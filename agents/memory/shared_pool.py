"""SharedMemoryPool — config-driven cross-agent memory pools (RFC 0008 §H, PR 4).

Implements the *hybrid* memory model from `RFC 0008 §H`_ (see
``docs/rfcs/0008-agent-memory-context-optimization.md``):
each agent keeps an isolated ``MemoryFacade``, and curated entries are
*published* to a named shared pool whose ACL is declared in
``config/agents.yaml``.

Per `RFC 0008 PR plan PR 4
<../../docs/rfcs/0008-pr-plan.md#pr-4-feature-v030-rfc0008-shared-pools-acl---phase-4a-shared-pool-acl--provenance>`_:

* ACL enforcement lives in this Python layer (deny-by-default), matching
  the existing ``agents/tools/permissions.py`` pattern.
* Provenance fields (``source_agent``, ``created_at``, ``confidence``)
  are framework-injected on write — callers cannot spoof ``source_agent``.
* Consumer-side ``min_confidence`` filter on read; ``None`` admits all.
* Sensitive pools (``sensitive: true``) cannot be published into via
  :meth:`MemoryFacade.publish_to_pool` regardless of writer ACL — RFC 0008
  §H safety constraint #3.
* Eviction is FIFO on ``created_at`` (not §G hybrid score) because pool
  entries lack the per-agent ``access_count`` series required by §G.

The orchestrator does not need to know about pools; the gRPC contract is
unchanged.  Capability-token enforcement (RFC 0009) will *augment* — not
replace — the config ACL when it lands; the :class:`SharedMemoryPool`
interface stays stable.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .episodic import EpisodicMemory

logger = logging.getLogger(__name__)


# ─── Public errors ───────────────────────────────────────────────


class SharedMemoryPermissionError(PermissionError):
    """Raised when an agent attempts a read/write/publish it is not permitted.

    Subclasses :class:`PermissionError` so callers can catch the broader
    standard-library type.  The structured ``reason`` attribute identifies
    the deny path (``not_in_readers`` / ``not_in_writers`` /
    ``sensitive_pool_isolation`` / ``unknown_pool``).  PR #223 review S1
    dropped a previously-documented ``provenance_set`` reason that was
    never raised; RFC 0009 capability tokens may re-introduce a token-
    mismatch reason later.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# ─── Public dataclasses ──────────────────────────────────────────


@dataclass(frozen=True)
class SharedPoolEntry:
    """A single shared-pool entry returned by :meth:`SharedMemoryPool.read`.

    ``source_agent`` and ``created_at`` are framework-injected on write —
    callers cannot spoof them.  ``confidence`` is caller-supplied at write
    time and validated to ``[0.0, 1.0]``.
    """

    id: str
    content: str
    source_agent: str
    created_at: float
    confidence: float
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class SharedPoolConfig:
    """Operator-supplied ACL + retention settings for one named pool.

    Parsed from the top-level ``shared_memory_pools`` section of
    ``config/agents.yaml``.  The schema (``schemas/agent.schema.json``)
    enforces shape + duplicate-free ACL lists at validate time.
    """

    name: str
    readers: frozenset[str] = field(default_factory=frozenset)
    writers: frozenset[str] = field(default_factory=frozenset)
    max_entries: int = 2000
    required_confidence: float = 0.0
    sensitive: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("SharedPoolConfig.name must not be empty")
        if self.max_entries < 1:
            raise ValueError(
                f"max_entries must be >= 1, got {self.max_entries}",
            )
        if not 0.0 <= self.required_confidence <= 1.0:
            raise ValueError(
                "required_confidence must be in [0.0, 1.0], "
                f"got {self.required_confidence}",
            )

    @classmethod
    def from_mapping(cls, name: str, raw: Mapping[str, Any]) -> SharedPoolConfig:
        """Build a config object from a parsed-YAML mapping."""
        return cls(
            name=name,
            readers=frozenset(raw.get("readers", []) or []),
            writers=frozenset(raw.get("writers", []) or []),
            max_entries=int(raw.get("max_entries", 2000)),
            required_confidence=float(raw.get("required_confidence", 0.0)),
            sensitive=bool(raw.get("sensitive", False)),
        )


# Shared pools live in the same SQLite file as the per-agent episodic
# tier; the synthetic agent_id below namespaces the rows so EpisodicMemory
# recall queries (which are agent-scoped) cannot cross-read between pools
# or between a pool and an isolated agent.
#
# Pattern matches ``schemas/agent.schema.json`` ``id`` so EpisodicMemory's
# eviction/ retention helpers (which assume a canonical agent_id) operate
# correctly.  The ``pool-`` prefix is reserved — no real agent may use it.
_POOL_AGENT_PREFIX = "pool-"
_POOL_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")

# PR #223 review S3 (mirrors PR-220 M3 / facade.py constant of same name):
# over-fetch when a Python-side ``min_confidence`` trust filter follows a
# fixed FTS5 ``recall(limit=N)`` so the post-filter result honours
# ``limit``.  Duplicated here (not imported from facade.py) to avoid an
# import cycle.  Drop once SQL-side ``importance`` pre-filter lands (PR 5+).
_MIN_CONFIDENCE_OVERFETCH_FACTOR = 3


def _pool_agent_id(name: str) -> str:
    if not _POOL_NAME_PATTERN.match(name):
        raise ValueError(
            f"shared pool name {name!r} must match {_POOL_NAME_PATTERN.pattern}",
        )
    return f"{_POOL_AGENT_PREFIX}{name}"


# ─── SharedMemoryPool ────────────────────────────────────────────


class SharedMemoryPool:
    """One named cross-agent memory pool with ACL + provenance enforcement.

    Wraps an :class:`EpisodicMemory` instance (one per pool, namespaced by
    ``pool-{name}``) so the FTS5 index, scoring, and SQLite WAL guarantees
    are reused.  The ACL gate is the only authority — callers who hold a
    ``SharedMemoryPool`` reference but are not in ``readers`` / ``writers``
    are still rejected by every public method.
    """

    def __init__(self, config: SharedPoolConfig, *, db_path: str) -> None:
        self._config = config
        self._db_path = db_path
        self._episodic = EpisodicMemory(
            agent_id=_pool_agent_id(config.name), db_path=db_path,
        )
        self._initialized = False

    # ─── Lifecycle ───────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def config(self) -> SharedPoolConfig:
        return self._config

    async def initialize(self) -> None:
        if self._initialized:
            return
        await self._episodic.initialize()
        self._initialized = True
        logger.info(
            "SharedMemoryPool %s initialised (db=%s, max_entries=%d, sensitive=%s)",
            self._config.name, self._db_path, self._config.max_entries,
            self._config.sensitive,
        )

    async def close(self) -> None:
        if not self._initialized:
            return
        await self._episodic.close()
        self._initialized = False

    # ─── ACL helpers ─────────────────────────────────────────────

    def _require_reader(self, agent_id: str) -> None:
        if agent_id not in self._config.readers:
            _record_denied(self._config.name, agent_id, operation="read")
            raise SharedMemoryPermissionError(
                f"agent {agent_id!r} is not a reader of pool "
                f"{self._config.name!r}",
                reason="not_in_readers",
            )

    def _require_writer(self, agent_id: str) -> None:
        if agent_id not in self._config.writers:
            _record_denied(self._config.name, agent_id, operation="write")
            raise SharedMemoryPermissionError(
                f"agent {agent_id!r} is not a writer of pool "
                f"{self._config.name!r}",
                reason="not_in_writers",
            )

    # ─── Read API ────────────────────────────────────────────────

    async def read(
        self,
        agent_id: str,
        query: str,
        *,
        limit: int = 10,
        min_confidence: float | None = None,
    ) -> list[SharedPoolEntry]:
        """Return entries matching *query*, filtered by ``min_confidence``.

        Raises :class:`SharedMemoryPermissionError` when ``agent_id`` is
        not a reader of this pool.  ``min_confidence=None`` admits all
        entries (the default — explicit operator opt-in for trust filters).
        """
        if not self._initialized:
            raise RuntimeError(
                f"SharedMemoryPool {self._config.name!r} not initialised",
            )
        self._require_reader(agent_id)
        if min_confidence is not None and not 0.0 <= min_confidence <= 1.0:
            raise ValueError(
                f"min_confidence must be in [0.0, 1.0], got {min_confidence}",
            )
        # PR #223 deep-review S3: over-fetch when a trust filter is
        # active so the post-filter result honours ``limit``.  No-op when
        # ``min_confidence is None`` (no culling will happen).
        recall_limit = (
            limit * _MIN_CONFIDENCE_OVERFETCH_FACTOR
            if min_confidence is not None
            else limit
        )
        # Note: ``recall`` increments ``access_count`` for every BM25 hit,
        # including rows the post-filter drops. Harmless (FIFO ignores it);
        # PR #223 pass-3 NTH-1.
        episodes = await self._episodic.recall(query, limit=recall_limit)
        out: list[SharedPoolEntry] = []
        for ep in episodes:
            ctx = ep.context if isinstance(ep.context, dict) else {}
            source = str(ctx.get("source_agent", ""))
            confidence = float(ctx.get("confidence", ep.importance))
            if min_confidence is not None and confidence < min_confidence:
                continue
            out.append(
                SharedPoolEntry(
                    id=ep.id,
                    content=ep.summary,
                    source_agent=source,
                    created_at=ep.created_at,
                    confidence=confidence,
                    tags=tuple(ep.tags or ()),
                ),
            )
            if len(out) >= limit:
                # Truncate at the caller-requested ``limit`` so the
                # over-fetch is invisible to the consumer.
                break
        _record_read(self._config.name, agent_id, len(out))
        return out

    # ─── Write API ───────────────────────────────────────────────

    async def write(
        self,
        agent_id: str,
        content: str,
        *,
        confidence: float,
        tags: Iterable[str] = (),
    ) -> str:
        """Persist *content* into the pool with framework-injected provenance.

        Raises :class:`SharedMemoryPermissionError` when ``agent_id`` is
        not a writer.  ``source_agent`` is bound 1-for-1 to *agent_id* —
        no override knob, so an in-process caller cannot spoof it without
        also impersonating the writer ACL.  (PR #223 review S1 removed an
        earlier ``source_agent_override`` kwarg the facade always set to
        ``agent_id`` anyway; RFC 0009 capability tokens may add a token-
        bound override later.)  Returns the new entry ID; FIFO eviction
        on ``created_at`` runs before return when count > ``max_entries``.
        Note: ``sensitive: true`` isolation (RFC §H safety #3) is enforced
        at the **facade** (``publish_via_facade``), not here — a direct
        ``pool.write()`` bypasses it. PR #223 pass-3.
        """
        if not self._initialized:
            raise RuntimeError(
                f"SharedMemoryPool {self._config.name!r} not initialised",
            )
        self._require_writer(agent_id)
        if not content or not content.strip():
            raise ValueError("content must not be empty")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(
                f"confidence must be in [0.0, 1.0], got {confidence}",
            )
        if confidence < self._config.required_confidence:
            raise ValueError(
                f"confidence {confidence} below required_confidence "
                f"{self._config.required_confidence} for pool "
                f"{self._config.name!r}",
            )
        # Provenance is bound 1-for-1 to ``agent_id`` (PR #223 review S1
        # — see method docstring for the trust-boundary rationale).
        ctx: dict[str, Any] = {
            "source_agent": agent_id,
            "confidence": confidence,
            "pool": self._config.name,
        }
        entry_id = await self._episodic.store_episode(
            summary=content,
            context=ctx,
            importance=confidence,
            tags=list(tags),
        )
        await self._enforce_fifo_cap()
        _record_write(self._config.name, agent_id)
        return entry_id

    async def _enforce_fifo_cap(self) -> None:
        """Drop oldest rows when the pool exceeds ``max_entries`` (FIFO).

        Single atomic ``DELETE ... WHERE id NOT IN (SELECT ... LIMIT
        max_entries)`` per PR #223 review N2 — eliminates the count→
        delete race a concurrent writer could open.  Reaches into
        ``EpisodicMemory._ensure_db()`` (review S5 / PR-2 M3); the
        canonical ``connection`` property is deferred to PR 5+.
        """
        db = self._episodic._ensure_db()  # noqa: SLF001 — RFC 0008 PR 5
        pool_agent = _pool_agent_id(self._config.name)
        cursor = await db.execute(
            """
            DELETE FROM episodes
            WHERE agent_id = ?
              AND id NOT IN (
                SELECT id FROM episodes
                WHERE agent_id = ?
                ORDER BY created_at DESC
                LIMIT ?
              )
            """,
            (pool_agent, pool_agent, self._config.max_entries),
        )
        evicted = cursor.rowcount or 0
        await db.commit()
        if evicted > 0:
            _record_evictions(self._config.name, evicted)


# ─── SharedPoolRegistry ──────────────────────────────────────────


class SharedPoolRegistry:
    """Lifecycle owner for the named pools declared in ``agents.yaml``.

    Built once per agent process from the parsed ``shared_memory_pools``
    section, then handed to :class:`MemoryFacade` so per-agent
    ``publish_to_pool`` / ``read_from_pool`` calls resolve by pool name.
    """

    def __init__(self, pools: Mapping[str, SharedMemoryPool]) -> None:
        self._pools = dict(pools)

    def __contains__(self, name: object) -> bool:
        return name in self._pools

    def get(self, name: str) -> SharedMemoryPool:
        try:
            return self._pools[name]
        except KeyError:
            raise SharedMemoryPermissionError(
                f"shared pool {name!r} is not configured",
                reason="unknown_pool",
            ) from None

    def names(self) -> tuple[str, ...]:
        return tuple(self._pools)

    def drop(self, name: str) -> None:
        """Remove *name* (idempotent).

        PR #223 review S2: ``start_shared_pools`` evicts pools whose
        ``initialize()`` raised so callers see the documented
        ``unknown_pool`` deny instead of a half-built pool's
        ``RuntimeError("not initialised")``.
        """
        self._pools.pop(name, None)

    async def close_all(self) -> None:
        for pool in self._pools.values():
            try:
                await pool.close()
            except Exception:  # pragma: no cover - best-effort teardown
                logger.exception("failed to close shared pool %s", pool.name)


def build_registry_from_config(
    raw: Mapping[str, Mapping[str, Any]] | None,
    *,
    db_path: str,
) -> SharedPoolRegistry:
    """Construct (but do *not* initialise) a registry from parsed YAML.

    The caller is expected to ``await initialize()`` each pool — typically
    once at agent-server startup.  An empty / missing section yields an
    empty registry, preserving deny-by-default for processes that never
    declare any pools.
    """
    pools: dict[str, SharedMemoryPool] = {}
    for name, cfg_raw in (raw or {}).items():
        cfg = SharedPoolConfig.from_mapping(name, cfg_raw)
        pools[name] = SharedMemoryPool(cfg, db_path=db_path)
    return SharedPoolRegistry(pools)


# ─── Observability hooks ─────────────────────────────────────────
#
# Metrics live under ``agent.shared_pool.*`` in the OTEL inventory
# (registered in ``agents/observability/metrics.py``).  Recording is
# nil-safe so unit tests that do not init OTEL still pass.


def _record_read(pool: str, agent: str, count: int) -> None:
    # PR #223 review N1: do NOT emit ``count`` as a counter attribute —
    # high cardinality.  Use a Histogram instead if per-call distribution
    # becomes interesting.  Param kept for caller-shape stability.
    del count
    inst = _try_instruments()
    if inst is None:
        return
    inst.shared_pool_reads.add(
        1, attributes={"pool": pool, "agent.id": agent},
    )


def _record_write(pool: str, agent: str) -> None:
    inst = _try_instruments()
    if inst is None:
        return
    inst.shared_pool_writes.add(1, attributes={"pool": pool, "agent.id": agent})


def _record_denied(pool: str, agent: str, *, operation: str) -> None:
    inst = _try_instruments()
    if inst is None:
        return
    inst.shared_pool_denied.add(
        1,
        attributes={"pool": pool, "agent.id": agent, "operation": operation},
    )


def _record_evictions(pool: str, count: int) -> None:
    inst = _try_instruments()
    if inst is None or count <= 0:
        return
    inst.shared_pool_evictions.add(count, attributes={"pool": pool})


def _try_instruments() -> Any:
    try:
        from ..observability.metrics import try_get_instruments
    except Exception:  # pragma: no cover - import-time isolation
        return None
    return try_get_instruments()


# ─── Facade helpers ──────────────────────────────────────────────
#
# ``publish_via_facade`` / ``read_via_facade`` live in
# ``agents/memory/shared_pool_facade.py`` to keep this module under
# the repo line cap.


__all__ = [
    "SharedMemoryPermissionError",
    "SharedMemoryPool",
    "SharedPoolConfig",
    "SharedPoolEntry",
    "SharedPoolRegistry",
    "build_registry_from_config",
]
