"""
Agent loading and persona lifecycle helpers for AgentServer.

- ``_resolve_agent_type`` / ``load_agent`` — agent config parsing and
  instantiation (task and persona agents).
- ``initialize_persona_agents`` — persona memory, dispatcher, and tick
  scheduler startup, called from ``AgentServer.start()``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .base import BaseAgent
from .llm_client import LLMClient, create_provider
from .persona import create_persona_agent
from .persona_runtime import _LLMPersonaAgent
from .prompt_loader import PromptLoadError, resolve_instructions
from .task_agent import TaskAgent
from .tick import TickScheduler
from .tools import builtin
from .tools.permissions import PermissionGate
from .tools.sandbox import PathValidator

if TYPE_CHECKING:
    from .dispatch import EventDispatcher

logger = logging.getLogger("Persatrix.agent.server_persona")

# _resolve_agent_type is intentionally excluded: it is a private helper
# used only within load_agent; tests that import it directly are accessing
# an implementation detail, not part of this module's public contract.
__all__ = ["load_agent", "initialize_persona_agents", "default_grpc_target"]


def default_grpc_target(orchestrator_url: str) -> str:
    """Derive the default gRPC target from the orchestrator REST URL.

    Strips the URL scheme + path and replaces the (REST, default 8080)
    port with the canonical orchestrator gRPC port (9090). Matches the
    docker-compose service layout where the same host serves both
    REST and gRPC, so a single ``--orchestrator-url`` argument is
    sufficient for the common case. Operators with a non-standard
    layout pass ``--orchestrator-grpc=<host:port>`` explicitly.
    """
    from urllib.parse import urlparse

    parsed = urlparse(orchestrator_url)
    host = parsed.hostname or "127.0.0.1"
    return f"{host}:9090"

# Agent IDs must match the cross-component contract shared with the Go
# orchestrator registry.  Validated at load time to prevent routing mismatches.
_AGENT_ID_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


# ─── Agent type resolution ───────────────────────────────────


def _resolve_agent_type(agent_config: dict[str, Any]) -> str:
    """Resolve agent type string from the ``type`` field in agent config.

    Supported types:
    - ``task`` (default) — data-driven TaskAgent with YAML instructions
    - ``persona`` — LLM-powered PersonaAgent with memory and autonomy

    Agents without a ``type`` field default to ``task`` for backward
    compatibility with v0.1 configs.
    """
    agent_type = agent_config.get("type", "task")

    match agent_type:
        case "task":
            return "task"
        case "persona":
            return "persona"
        case _:
            raise SystemExit(
                f"Unknown agent type {agent_type!r} for agent "
                f"{agent_config['id']!r}. Supported types: task, persona"
            )


# ─── Agent loading ───────────────────────────────────────────


def load_agent(
    agent_id: str,
    config_path: str,
    workspace: str,
    repo_root: str | Path | None = None,
) -> BaseAgent:
    """Load an agent by ID from YAML config.

    Returns a fully-initialized BaseAgent with LLM client, tools, and
    permission configuration wired.

    ``repo_root`` is the anchor used to resolve ``instructions_file``
    references against the ``prompts/`` subtree.  When omitted, it
    defaults to this package's parent directory (``Path(__file__).parent.parent``),
    which matches the source-tree convention used by sibling tooling
    (e.g. ``scripts/checks/doc_links.py``).  Operators can override
    explicitly when running from a non-default layout, and tests inject
    a fixtured root.
    """
    if not _AGENT_ID_PATTERN.match(agent_id):
        raise SystemExit(
            f"Invalid agent ID {agent_id!r}: "
            f"must match {_AGENT_ID_PATTERN.pattern}"
        )

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        raise SystemExit(f"Agent config not found: {config_path}")
    except yaml.YAMLError as exc:
        raise SystemExit(f"Invalid YAML in {config_path}: {exc}")

    # A malformed config like ``agents: "string"`` would otherwise fail
    # with an unclear TypeError during enumeration.
    agents_list = config.get("agents", [])
    if not isinstance(agents_list, list):
        raise SystemExit(
            f"'agents' key in {config_path} must be a list, "
            f"got {type(agents_list).__name__}"
        )

    # Validate 'id' field presence before the dict comprehension to
    # surface a clear SystemExit instead of a raw KeyError.
    for i, a in enumerate(agents_list):
        if "id" not in a:
            raise SystemExit(
                f"Agent config entry {i} missing required 'id' field"
            )
    # Dict comprehension silently takes the last duplicate ID;
    # detect and reject duplicates before building the map.
    seen_ids: set[str] = set()
    for a in agents_list:
        aid = a["id"]
        if aid in seen_ids:
            raise SystemExit(
                f"Duplicate agent ID {aid!r} in {config_path}"
            )
        seen_ids.add(aid)
    agent_configs = {a["id"]: a for a in agents_list}
    if agent_id not in agent_configs:
        raise SystemExit(f"Agent {agent_id!r} not found in {config_path}")

    agent_config = agent_configs[agent_id]
    agent_type = _resolve_agent_type(agent_config)

    # Validate required 'model' field at startup so operators see a
    # clear message instead of a raw KeyError from create_provider().
    if "model" not in agent_config:
        raise SystemExit(
            f"Agent {agent_id!r} missing required 'model' field in config"
        )

    # Resolve instructions_file (if present) into the inline instructions
    # field before constructing the agent.  This keeps TaskAgent runtime
    # purely data-driven — it never touches the filesystem.
    if agent_type == "task" and "instructions_file" in agent_config:
        # Default the anchor to this package's parent (the repo root in the
        # source tree) rather than deriving it from ``config_path``.  The
        # config-path-relative derivation silently mis-resolved the prompts/
        # subtree whenever an operator pointed --config at a non-default
        # layout (e.g. /etc/persatrix/agents.yaml).  The kwarg above allows
        # an explicit override for tests and unusual layouts.
        resolved_root: Path = (
            Path(repo_root) if repo_root is not None
            else Path(__file__).resolve().parent.parent
        )
        try:
            resolved = resolve_instructions(agent_config, resolved_root)
        except PromptLoadError as exc:
            raise SystemExit(str(exc))
        # Replace the file reference with the resolved text on a copy so
        # the original config dict (potentially shared across agents) is
        # not mutated.
        agent_config = {
            k: v for k, v in agent_config.items() if k != "instructions_file"
        }
        if resolved is not None:
            agent_config["instructions"] = resolved

    # Create LLM client
    provider = create_provider(agent_config)
    llm_client = LLMClient(provider)

    # Create agent based on type
    agent: BaseAgent
    if agent_type == "persona":
        agent = create_persona_agent(
            agent_id=agent_id,
            config=agent_config,
            llm_client=llm_client,
        )
    else:
        agent = TaskAgent(
            agent_id=agent_id,
            config=agent_config,
            llm_client=llm_client,
        )

    # Wire built-in tool dependencies
    permissions = agent_config.get("permissions", {})
    builtin.permission_gate = PermissionGate(permissions)
    fs = permissions.get("filesystem", {})
    builtin.path_validator = PathValidator(
        allow_read=fs.get("read", []),
        allow_write=fs.get("write", []),
        deny=fs.get("deny", []),
    )
    builtin.workspace_root = Path(workspace).resolve()

    return agent


# ─── Shared memory pools (RFC 0008 PR plan PR 4) ─────────────


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
    """
    if registry is None:
        return
    for pool_name in registry.names():
        try:
            await registry.get(pool_name).initialize()
        except Exception:
            logger.exception(
                "Failed to initialise shared pool %s — dropping from "
                "registry; subsequent calls will raise unknown_pool",
                pool_name,
            )
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


# ─── Persona lifecycle ───────────────────────────────────────


async def initialize_persona_agents(
    agents: dict[str, BaseAgent],
    dispatcher: EventDispatcher,
    tick_schedulers: dict[str, TickScheduler],
    *,
    shared_pools=None,  # type: ignore[no-untyped-def]
) -> None:
    """Initialize memory, dispatcher, and tick schedulers for persona agents.

    Called from ``AgentServer.start()`` after the gRPC server is listening,
    so memory failures cannot prevent non-persona agents from serving.

    Memory failure for a given agent is logged and that agent is skipped
    (it will NOT receive dispatched events or tick scheduling).

    ``tick_schedulers`` is mutated in-place: started TickScheduler instances
    are inserted under their agent ID.  Non-persona agents are left untouched.

    ``shared_pools`` (RFC 0008 PR plan PR 4) is started here (idempotent)
    and forwarded to :meth:`BaseAgent.initialize_memory`.
    """
    await start_shared_pools(shared_pools)
    for agent_id, agent in agents.items():
        if not isinstance(agent, _LLMPersonaAgent):
            continue

        # Memory initialization — must succeed before dispatch/tick.
        try:
            await agent.initialize_memory(shared_pools=shared_pools)
            logger.info("Initialized memory for persona agent %s", agent_id)
        except Exception:
            logger.exception(
                "Failed to initialize memory for agent %s — "
                "agent will NOT receive dispatched events or tick scheduling",
                agent_id,
            )
            continue

        # Register with event dispatcher.
        dispatcher.register_agent(agent_id, agent)

        # Start tick scheduler for autonomous agents.
        autonomy = agent.config.get("autonomy", {})
        level = autonomy.get("level", "reactive")
        if level in ("semi-autonomous", "autonomous"):
            interval = autonomy.get("tick_interval_seconds", 60)
            max_actions = autonomy.get("max_actions_per_tick", 3)
            idle_after = autonomy.get("idle_after_ticks", 10)
            scheduler = TickScheduler(
                agent,
                interval=float(interval),
                max_actions_per_tick=max_actions,
                idle_after_ticks=idle_after,
                executor=dispatcher.executor,
            )
            tick_schedulers[agent_id] = scheduler
            dispatcher.register_tick_scheduler(agent_id, scheduler)
            # Cost-safety notice — emitted *before* scheduler.start() so the
            # warning is guaranteed to reach the log before any LLM spend can
            # begin.  Placing it after start() would invert the semantic order
            # the PR description promised ("at the exact moment spend can begin")
            # and mislead future maintainers.
            # See README.md "Cost Warning" and SECURITY.md "Responsible Use".
            max_llm_calls = agent.config.get("max_llm_calls", "unset")
            logger.warning(
                "COST: persona '%s' is about to start an autonomous tick loop "
                "and will consume LLM tokens continuously.",
                agent_id,
            )
            logger.warning(
                "COST: tick_interval=%ss, max_actions_per_tick=%s, "
                "idle_after_ticks=%s, max_llm_calls=%s.",
                interval,
                max_actions,
                idle_after,
                max_llm_calls,
            )
            logger.warning(
                "COST: stop agent '%s' explicitly when done — do not rely "
                "on idle detection alone. Confirm hard spending limits are "
                "set at your LLM provider's billing page.",
                agent_id,
            )
            scheduler.start()
            logger.info(
                "Started tick scheduler for %s (interval=%ds)",
                agent_id,
                interval,
            )
