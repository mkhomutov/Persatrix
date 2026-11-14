"""
Shared memory pools of the agent server: load, start, stop.

RFC 0008 PR 4.  A pool is declared under ``shared_memory_pools`` in the
agents config and keeps its rows in the hosted agent's own memory
database file, through a connection of its own.

- ``load_shared_pools`` / ``setup_shared_pools``: build the registry from
  the config, at process start.
- ``start_shared_pools``: open each pool, called from
  ``initialize_persona_agents`` before the persona's own memory opens.
- ``stop_shared_pools``: close them, called from ``AgentServer.stop()``.

Split out of ``server_persona.py``, which had reached the size at which
the file-size check warns.
"""

from __future__ import annotations

import logging

import yaml

from .base import BaseAgent

# The name these functions have always logged under.
logger = logging.getLogger("Persatrix.agent.server_persona")

__all__ = [
    "load_shared_pools",
    "setup_shared_pools",
    "start_shared_pools",
    "stop_shared_pools",
]


def load_shared_pools(
    config_path: str,
    *,
    db_path: str = "data/memory.db",
):  # type: ignore[no-untyped-def]
    """Build a :class:`SharedPoolRegistry` from the top-level ``shared_memory_pools``.

    Returns an empty registry when the section is absent or empty —
    deny-by-default for processes that do not declare any pools.  Errors
    in the YAML structure raise :class:`SystemExit` (mirrors
    :func:`load_agent`'s startup-failure contract).
    """
    from .memory.shared_pool import build_registry_from_config

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        raise SystemExit(f"Agent config not found: {config_path}")
    except yaml.YAMLError as exc:
        raise SystemExit(f"Invalid YAML in {config_path}: {exc}")
    raw = (config or {}).get("shared_memory_pools") or {}
    if not isinstance(raw, dict):
        raise SystemExit(
            f"'shared_memory_pools' in {config_path} must be a mapping, "
            f"got {type(raw).__name__}",
        )
    try:
        return build_registry_from_config(raw, db_path=db_path)
    except (TypeError, ValueError) as exc:
        raise SystemExit(
            f"Invalid shared_memory_pools entry in {config_path}: {exc}",
        )


async def start_shared_pools(registry) -> None:  # type: ignore[no-untyped-def]
    """Initialize each pool in *registry*; per-pool failures are logged
    and the failing pool is removed from the registry so subsequent
    ``read``/``write`` raise the documented
    :class:`SharedMemoryPermissionError` (``unknown_pool``) instead of a
    bare ``RuntimeError("not initialised")`` from a half-built pool.

    PR #223 deep-review S2: an earlier draft swallowed the exception
    and left the failing pool in the registry, so ``registry.get(name)``
    succeeded but the next ``pool.read``/``pool.write`` raised
    ``RuntimeError("... not initialised")`` — which an operator could
    not distinguish from a programming error.  Evicting on failure makes
    ``unknown_pool`` semantics honest until process restart.

    The failing pool is closed before it is dropped, so nothing it opened
    is left behind.
    """
    if registry is None:
        return
    for pool_name in registry.names():
        pool = registry.get(pool_name)
        try:
            await pool.initialize()
        except Exception:
            logger.exception(
                "Failed to initialise shared pool %s — dropping from "
                "registry; subsequent calls will raise unknown_pool",
                pool_name,
            )
            # Close what did open.  A pool keeps its rows in the persona's
            # own database file, through a connection of its own, and a
            # connection that was made runs a thread that is not a daemon.
            # Dropped while open, the pool is out of reach of every later
            # close, and the agent process can then never end: not on a
            # stop, and not on the exit a persona without memory takes
            # (ISSUE-0205).
            try:
                await pool.close()
            except Exception:
                logger.exception("Failed to close shared pool %s", pool_name)
            registry.drop(pool_name)


async def stop_shared_pools(registry) -> None:  # type: ignore[no-untyped-def]
    """Close every pool in *registry*; safe when *registry* is ``None``."""
    if registry is not None:
        await registry.close_all()


def setup_shared_pools(server, config_path: str, agent: BaseAgent) -> None:
    """Load the shared-pool registry and assign it to ``server._shared_pools``.

    RFC 0008 PR 4 wiring helper used by :func:`agents.server.main`.
    """
    db_path = ((agent.config or {}).get("memory") or {}).get("db_path", "data/memory.db")
    server._shared_pools = load_shared_pools(config_path, db_path=db_path)
