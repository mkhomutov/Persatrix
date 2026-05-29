"""MemoryStore — the single, frozen entry point for agent memory (RFC 0029).

RFC 0029 Phase 1 promotes the RFC 0008 ``MemoryFacade`` to ``MemoryStore``:
one Python class that owns every memory-backend connection and exposes a
typed API.  Personal-tier methods (episodes, notes, facts, bonds-self,
commitments) hit per-agent SQLite — behaviour identical to the legacy
facade.  Society-tier methods (shared pools, inbound trust) hit Postgres
when a ``society_dsn`` is configured; Phase 1 ships single-agent mode
only, so they raise the :class:`SocietyBackendUnavailable` hierarchy.

The promotion was a **pure refactor**: ``MemoryStore`` kept the legacy
``MemoryFacade`` construction signature, and :mod:`agents.memory.facade`
carried a thin ``MemoryFacade`` alias of this class for one minor version
so downstream callers migrated incrementally.  That alias was removed in
v0.3.3 — all call sites import ``MemoryStore`` directly.  The frozen
facade is the v0.4.0 prerequisite — RFC 0028 pins its ``DecisionRecord``
schema against this surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..principal_id import resolve_principal_id_silent
from ..session_id import current_session_id, resolve_session_id_silent
from .decay import (
    DEFAULT_C_MIN,
    DEFAULT_LAMBDA_PER_DAY,
    DEFAULT_STALE_CONFIDENCE_ALERT_THRESHOLD,
)
from .episodic import EpisodicMemory
from .eviction import eviction_loop
from .facade_procedural import (
    ProceduralFacadeMixin,
    validate_decay_params,
)
from .scope_recall import recall_with_scope_filter
from .shared_pool import SharedPoolRegistry
from .shared_pool_facade import SharedPoolFacadeMixin
from .society_facade import (
    SocietyBackendUnavailable,
    SocietyDisabled,
    SocietyFacadeMixin,
    SocietyTransientError,
)
from .store_types import (
    Candidate,
    CompressedView,
    MemoryDisabledError,
    MemoryEntry,
)
from .working import estimate_tokens

logger = logging.getLogger(__name__)


# ─── Construction contract ──────────────────────────────────────


@dataclass(frozen=True)
class StoreConfig:
    """Construction contract for :class:`MemoryStore` (RFC 0029 §C).

    Names the agent and *optionally* the society backend.  Frozen so the
    v0.4.0 boundary is stable: RFC 0029 Phases 2–6 extend this dataclass,
    they do not re-version it.  ``society_dsn=None`` selects single-agent
    mode.  ``capability_token`` is reserved for Phase 2 cross-agent reads.
    """

    agent_id: str
    personal_db_path: str = "data/memory.db"
    society_dsn: str | None = None
    capability_token: bytes | None = None


# ─── MemoryStore ─────────────────────────────────────────────────


class MemoryStore(ProceduralFacadeMixin, SharedPoolFacadeMixin, SocietyFacadeMixin):
    """Unified memory access for agents (RFC 0008 §B, promoted by RFC 0029 §C).

    Per-process lifecycle (RFC 0008 Open Question 7): a single
    ``EpisodicMemory`` instance per agent process, shared across
    concurrent gRPC calls **and** the periodic eviction loop scheduled
    by :meth:`initialize`.  Both callers share one ``aiosqlite``
    connection; serialisation is provided by aiosqlite's worker-thread
    queue (no extra ``asyncio.Lock`` is introduced — the queue handles
    the dispatch/eviction interleave correctly).  ``initialize()`` opens
    the DB and starts the eviction loop; ``close()`` cancels the loop
    and closes the DB.  The lifecycle satisfies
    :class:`~agents.memory.MemoryLifecycle` structurally.

    Society-tier methods (:meth:`read_pool`, :meth:`query_inbound_trust`)
    come from :class:`SocietyFacadeMixin` and raise
    :class:`SocietyBackendUnavailable` until the RFC 0029 Phase 3
    Postgres backend lands — Phase 1 ships single-agent mode only.
    """

    def __init__(
        self,
        agent_id: str,
        *,
        db_path: str = "data/memory.db",
        default_min_score: float | None = None,
        episodic_cap: int = 1000,
        ttl_low_importance_days: int = 30,
        eviction_cadence_seconds: int = 3600,
        shared_pools: SharedPoolRegistry | None = None,
        lambda_per_day: float = DEFAULT_LAMBDA_PER_DAY,
        c_min: float = DEFAULT_C_MIN,
        stale_confidence_alert_threshold: float = (
            DEFAULT_STALE_CONFIDENCE_ALERT_THRESHOLD
        ),
        society_dsn: str | None = None,
        capability_token: bytes | None = None,
    ) -> None:
        if episodic_cap < 1:
            raise ValueError(f"episodic_cap must be >= 1, got {episodic_cap}")
        if ttl_low_importance_days < 1:
            raise ValueError(f"ttl_low_importance_days must be >= 1, got {ttl_low_importance_days}")
        if eviction_cadence_seconds <= 0:
            raise ValueError(
                f"eviction_cadence_seconds must be positive, got {eviction_cadence_seconds}"
            )
        # PR 5: validate procedural-tier decay knobs at the boundary.
        validate_decay_params(
            lambda_per_day=lambda_per_day,
            c_min=c_min,
            stale_confidence_alert_threshold=stale_confidence_alert_threshold,
        )
        self._agent_id = agent_id
        self._db_path = db_path
        self._default_min_score = default_min_score
        self._episodic_cap = episodic_cap
        self._ttl_low_importance_days = ttl_low_importance_days
        self._eviction_cadence_seconds = eviction_cadence_seconds
        self._lambda_per_day = lambda_per_day
        self._c_min = c_min
        self._stale_alert_threshold = stale_confidence_alert_threshold
        self._episodic = EpisodicMemory(agent_id=agent_id, db_path=db_path)
        self._initialized = False
        self._eviction_task: asyncio.Task[None] | None = None
        self._shared_pools = shared_pools
        # RFC 0029 Phase 1: society backend is configured (DSN) but never
        # consumed — single-agent mode is the only mode.  Stored so the
        # SocietyFacadeMixin can raise SocietyDisabled vs SocietyTransientError.
        self._society_dsn = society_dsn
        self._capability_token = capability_token
        # RFC 0031 Phase 1: resolve PERSATRIX_SESSION_ID once at
        # construction so the task-agent / sub-agent path inherits the
        # operator-namespace tag without an explicit kwarg at every write
        # site.  Silent by design — the canonical INFO/WARN lines come from
        # ``PersonaAgent.__init__`` and the Go orchestrator's
        # ``resolveSessionID``.  Delegates to the leaf module
        # ``agents.session_id`` so the env-var name + legacy carve-out
        # cannot drift against ``agents.persona_runtime.session_id``.
        self._session_id = resolve_session_id_silent()
        # ISSUE-0081 PR 3: resolve the tenant/principal once at
        # construction, same rationale as the session snapshot above.  The
        # procedural recall path (the only facade-level read that builds
        # its own scope predicate) threads this; the per-tier objects own
        # their own ``_active_principal_id`` snapshots for the
        # persona-direct path.
        self._principal_id = resolve_principal_id_silent()

    @classmethod
    def from_config(cls, config: StoreConfig) -> MemoryStore:
        """Build a :class:`MemoryStore` from a :class:`StoreConfig`.

        The RFC 0029 §C construction path.  Eviction / decay knobs keep
        their defaults — operators tune those through the legacy keyword
        constructor or ``config/agents.yaml``.

        ``StoreConfig`` carries no ``shared_pools``: the RFC 0008
        in-process :class:`~agents.memory.shared_pool.SharedPoolRegistry`
        is a live runtime object, not config, so a store that needs
        cross-agent pools must still be built via the keyword constructor
        (as :meth:`agents.base.BaseAgent.initialize_memory` does).  The
        omission is deliberate — the registry is subsumed by the society
        tier in Phase 3 — so ``from_config`` is not a drop-in replacement
        for the keyword constructor when shared pools are in play.
        """
        return cls(
            agent_id=config.agent_id,
            db_path=config.personal_db_path,
            society_dsn=config.society_dsn,
            capability_token=config.capability_token,
        )

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def session_id(self) -> str:
        """Construction-time ``PERSATRIX_SESSION_ID`` snapshot (RFC 0031
        Phase 1).  Public read-only contract; tests must use this
        rather than the private ``_session_id``.
        """
        return self._session_id

    @property
    def episodic(self) -> EpisodicMemory:
        """Read-only access to the underlying episodic tier.

        Exposed so RFC 0020's ``InteractionTracker`` can keep its current
        direct-write code path; the facade gains full ownership in PR 5.
        Raises :class:`MemoryDisabledError` if not initialised.
        """
        self._require_initialised()
        return self._episodic

    # ─── Lifecycle ───────────────────────────────────────────────

    async def initialize(self) -> None:
        """Open the underlying SQLite connection.

        Idempotent: a second call is a no-op + warning, matching the
        :class:`~agents.memory.MemoryLifecycle` protocol contract used
        elsewhere in the runtime.
        """
        if self._initialized:
            logger.warning(
                "MemoryStore.initialize() called twice for agent %s — no-op",
                self._agent_id,
            )
            return
        await self._episodic.initialize()
        self._initialized = True
        db = self._episodic._ensure_db()  # noqa: SLF001 — schedule eviction loop
        self._eviction_task = asyncio.create_task(
            eviction_loop(
                self._agent_id, db, episodic_cap=self._episodic_cap,
                ttl_low_importance_days=self._ttl_low_importance_days,
                cadence_seconds=self._eviction_cadence_seconds,
                lambda_per_day=self._lambda_per_day,
                c_min=self._c_min,
            ),
            name=f"memory-eviction-{self._agent_id}",
        )
        logger.info("MemoryStore initialised for agent %s (db=%s)", self._agent_id, self._db_path)

    async def close(self) -> None:
        """Close the underlying SQLite connection.

        Safe to call multiple times — second call is a no-op.
        """
        if not self._initialized:
            return
        if self._eviction_task is not None:
            self._eviction_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._eviction_task
            self._eviction_task = None
        await self._episodic.close()
        self._initialized = False

    # ─── Read API ────────────────────────────────────────────────

    async def retrieve_relevant(
        self,
        query: str,
        *,
        limit: int = 10,
        scope: str | None = None,
        tags: Iterable[str] | None = None,
        min_score: float | None = None,
        sessions: list[str] | str | None = None,
    ) -> list[MemoryEntry]:
        """Return relevant memory entries for *query* (RFC 0008 §B).

        ``scope`` filters in Python after recall (SQL-side lands in PR 5).
        ``tags`` is AND-semantic — entry tags must be a superset of the
        requested set (RFC 0011 PR 5 contract; do not change to OR).
        ``min_score`` is the FTS5 BM25 relevance floor in ``[0, 1]``;
        ``None`` falls back to the facade's ``default_min_score``.
        ``sessions`` (RFC 0031 §D / PR 4 — OQ #4 back-compat): ``None``
        → the tier's ``_active_session_id`` + ``legacy`` carve-out;
        list → those sessions + carve-out; ``"*"`` → no filter
        (CLI/debug); ``[]`` → :class:`ValueError`.  PR 451 review M2
        carry-forward: pass-through to the tier so
        :func:`agents.memory._session_filter._resolve_session_list` is
        the single source of truth for the §D default; the facade's own
        ``_session_id`` snapshot is read from the same env var as the
        tier's at construction so the two are equal by construction.
        Matches ``channel_history.recall_channel_episodes`` which has
        always been pass-through.
        """
        self._require_initialised()
        effective_min_score = (
            min_score if min_score is not None else self._default_min_score
        )
        # Pass ``sessions`` through unchanged; the tier resolves it.
        episodes = await recall_with_scope_filter(
            self._episodic,
            query,
            limit=limit,
            scope=scope,
            tags=tags,
            min_score=effective_min_score,
            sessions=sessions,
        )
        out: list[MemoryEntry] = []
        for ep in episodes:
            entry_scope = ep.scope
            if entry_scope is None and isinstance(ep.context, dict):
                entry_scope = ep.context.get("scope")
            out.append(
                MemoryEntry(
                    id=ep.id,
                    content=ep.summary,
                    importance=ep.importance,
                    tags=tuple(ep.tags or ()),
                    created_at=ep.created_at,
                    # EpisodicMemory.recall does not surface the per-row
                    # normalised score on the Episode dataclass today; the
                    # facade returns 0.0 as the unranked sentinel — PR 5
                    # promotes the score onto Episode so it can pass through.
                    score=0.0,
                    scope=entry_scope,
                )
            )
        return out

    async def list_candidates(self, task_context: dict[str, Any]) -> list[Candidate]:
        """Return facade-level candidates for context-package admission (Phase 2 stub).

        PR 5 wires this into the orchestrator-side packaging pipeline.
        Returns ``[]`` in Phase 2 — the orchestrator builds the package
        from upstream step outputs without consulting the agent.
        """
        self._require_initialised()
        return []

    # ─── Write API ───────────────────────────────────────────────

    async def store_observation(
        self,
        content: str,
        *,
        scope: str | None = None,
        importance: float = 0.5,
        ttl_seconds: float | None = None,
        tags: Iterable[str] = (),
        outcome: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """Persist *content* as an episodic observation.

        Returns the generated entry ID.

        Parameters
        ----------
        scope:
            Optional scope tag (e.g. ``"channel:slack-#dev"``) stored on
            the underlying episode's ``context`` so future scope-filtered
            recall can find it.
        ttl_seconds:
            Advisory TTL hint stored on the underlying episode.  Stored so
            callers can set it without a future migration.
        tags:
            Caller-supplied tag set.  Mutable types accepted for
            ergonomic callers; the facade normalises to ``list[str]``
            before persistence.
        outcome:
            Optional caller-supplied outcome string persisted on the
            underlying episode (RFC 0008 §B); not ranked separately.
        session_id:
            RFC 0031 Phase 1 operator-namespace tag.  ``None`` falls
            back to the facade's construction-time default (resolved
            from ``PERSATRIX_SESSION_ID``); pass an explicit value to
            override on a per-call basis.
        """
        self._require_initialised()
        if not content or not content.strip():
            raise ValueError("content must not be empty")
        tag_list = list(tags)
        context: dict[str, Any] = {}
        if scope is not None:
            context["scope"] = scope
        if ttl_seconds is not None:
            if ttl_seconds <= 0:
                raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
            context["ttl_seconds"] = ttl_seconds
        return await self._episodic.store_episode(
            summary=content,
            context=context,
            outcome=outcome,
            importance=importance,
            tags=tag_list,
            scope=scope,
            # ISSUE-0081: resolve the default at *call* time — a
            # per-request ``session_scope`` (task-local) wins over the
            # construction-time snapshot, which stays the fallback seed.
            session_id=(
                session_id
                if session_id is not None
                else (current_session_id() or self._session_id)
            ),
            surface="observation",  # RFC 0031 PR 4 F2: counter dimension
        )

    async def record_action(
        self,
        action: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Append an action to the per-agent provenance log (RFC 0029 §C).

        Reserved for the SA-7 / RFC 0028 audit path.  Phase 1 does **not**
        choose a backend (per-agent JSONL vs. SQLite append-only table is
        OQ §3 / SA-7's call), so the method raises rather than leaving an
        ambiguous no-op body — callers see an explicit "backend not chosen
        yet" error instead of silently losing audit writes.  The method
        name is reserved now so SA-7 extends the personal-tier API without
        re-versioning the facade.
        """
        raise NotImplementedError(
            "record_action backend chosen by SA-7 (RFC 0028 spawn); not in Phase 1",
        )

    # ─── Procedural tier (RFC 0008 PR 5) ─────────────────────
    # ``store_procedure`` and ``retrieve_procedures`` come from
    # :class:`ProceduralFacadeMixin` to keep this file under the
    # repo's 500-line cap.

    @staticmethod
    def compress(
        entries: Iterable[MemoryEntry],
        *,
        target_tokens: int,
    ) -> CompressedView:
        """Extractively compress *entries* into a view of ≤ ``target_tokens``.

        RFC 0020 PR 4 contract.  Phase 2 implementation: highest-
        importance first, in-order until the running token count would
        exceed ``target_tokens``.  Idempotent.  Entries individually
        larger than ``target_tokens`` are silently skipped (knapsack-
        suboptimal but acceptable for Phase 2 — the extractive path is a
        stop-gap until PR 5's abstractive path) and count toward
        ``entries_dropped``.  :func:`staticmethod` so RFC 0020 PR 4's
        persona-runtime call site can invoke it without a facade instance.
        Pinned by [RFC 0020 PR plan](../../docs/rfcs/0020-pr-plan.md) PR 4.
        """
        if target_tokens < 0:
            raise ValueError(f"target_tokens must be >= 0, got {target_tokens}")
        entry_list = list(entries)
        # Stable-sort by importance descending so equal-importance entries
        # retain their input order (deterministic for tests).
        ordered = sorted(entry_list, key=lambda e: -e.importance)
        admitted_chunks: list[str] = []
        admitted_tokens = 0
        admitted_count = 0
        # tokens_before is summed over the original input set.
        tokens_before = sum(estimate_tokens(e.content) for e in entry_list)
        for entry in ordered:
            entry_tokens = estimate_tokens(entry.content)
            if admitted_tokens + entry_tokens > target_tokens:
                continue
            admitted_chunks.append(entry.content)
            admitted_tokens += entry_tokens
            admitted_count += 1
        return CompressedView(
            summary="\n\n".join(admitted_chunks),
            entries_dropped=len(entry_list) - admitted_count,
            tokens_before=tokens_before,
            tokens_after=admitted_tokens,
        )

    # ─── Internals ───────────────────────────────────────────────

    def _require_initialised(self) -> None:
        if not self._initialized:
            # Raise MemoryDisabledError (a RuntimeError subclass) so
            # write-side callers can catch a memory-specific error type.
            # RuntimeError-matching tests remain green because
            # MemoryDisabledError IS-A RuntimeError.
            raise MemoryDisabledError(
                "MemoryStore not initialised — call initialize() first",
            )


__all__ = [
    "Candidate",
    "CompressedView",
    "MemoryDisabledError",
    "MemoryEntry",
    "MemoryStore",
    "SocietyBackendUnavailable",
    "SocietyDisabled",
    "SocietyTransientError",
    "StoreConfig",
]
