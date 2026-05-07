"""
MemoryFacade — unified memory access surface for task agents (RFC 0008 §B).

Provides a stable API over the underlying memory tiers (episodic, notes,
working) so task agents can store observations, retrieve relevant context,
and compress entry sets without coupling to tier-specific schemas.  PR 2
shipped the facade-only delivery; PR 2a (this file's current revision)
adds the periodic episodic-tier eviction loop per RFC 0008 §G.  The
pinned API surface (``retrieve_relevant`` with ``tags`` filter, ``compress``
hook) is finalised here so [RFC 0011 PR plan](../../docs/rfcs/0011-pr-plan.md)
PR 5 and [RFC 0020 PR plan](../../docs/rfcs/0020-pr-plan.md) PR 4 can pin
against it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

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
from .working import estimate_tokens

logger = logging.getLogger(__name__)


# ─── Public dataclasses ─────────────────────────────────────────


@dataclass(frozen=True)
class MemoryEntry:
    """A facade-level memory entry returned by ``retrieve_relevant``.

    Tier-agnostic projection of an underlying episode (or, in future PRs,
    a note / shared-pool entry).  Callers must not depend on the underlying
    storage tier — the facade is the boundary.
    """

    id: str
    content: str
    importance: float
    tags: tuple[str, ...]
    created_at: float
    score: float
    """Relevance score in ``[0, 1]`` from the underlying tier (0 when unranked)."""
    scope: str | None = None


@dataclass(frozen=True)
class CompressedView:
    """Result of ``MemoryFacade.compress`` — the API hook required by RFC 0020 PR 4.

    Fields match RFC 0008 §B compress contract.  ``summary`` is the
    extractive concatenation of admitted entries (Phase 2); the abstractive
    path is wired in PR 5.
    """

    summary: str
    entries_dropped: int
    tokens_before: int
    tokens_after: int


@dataclass(frozen=True)
class Candidate:
    """Phase 2 stub: ``list_candidates`` returns ``[]``.  Populated in PR 5."""

    id: str
    content: str
    tokens: int
    importance: float


# ─── Errors ─────────────────────────────────────────────────────


class MemoryDisabledError(RuntimeError):
    """Raised when memory operations are attempted on an uninitialised facade.

    Per RFC 0008 PR plan PR 2 integration test: tool calls that would write
    memory must raise instead of silently no-op'ing, so the misconfiguration
    surfaces during agent startup integration testing.  Subclasses
    :class:`RuntimeError` for backward compatibility with the pre-PR-220
    facade error type.  PR 2a raises this from both write-side
    :meth:`MemoryFacade._require_initialised` and the read-side
    ``episodic`` property.
    """


# ─── MemoryFacade ────────────────────────────────────────────────


class MemoryFacade(ProceduralFacadeMixin, SharedPoolFacadeMixin):
    """Unified memory access for task agents (RFC 0008 §B).

    Per-process lifecycle (RFC 0008 Open Question 7): a single
    ``EpisodicMemory`` instance per task-agent process, shared across
    concurrent gRPC calls **and** the periodic eviction loop scheduled
    by :meth:`initialize`.  Both callers share one ``aiosqlite``
    connection; serialisation is provided by aiosqlite's worker-thread
    queue (no extra ``asyncio.Lock`` is introduced — the queue handles
    the dispatch/eviction interleave correctly).  ``initialize()`` opens
    the DB and starts the eviction loop; ``close()`` cancels the loop
    and closes the DB.  The lifecycle satisfies
    :class:`~agents.memory.MemoryLifecycle` structurally.
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

    @property
    def agent_id(self) -> str:
        return self._agent_id

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
                "MemoryFacade.initialize() called twice for agent %s — no-op",
                self._agent_id,
            )
            return
        await self._episodic.initialize()
        self._initialized = True
        db = self._episodic._ensure_db()  # noqa: SLF001 — PR 2a: schedule eviction loop
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
        logger.info("MemoryFacade initialised for agent %s (db=%s)", self._agent_id, self._db_path)

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
    ) -> list[MemoryEntry]:
        """Return relevant memory entries for *query* (RFC 0008 §B).

        Parameters
        ----------
        query:
            Free-text query passed through to the underlying episodic tier.
        limit:
            Maximum number of entries to return.  The facade may return
            fewer when the tag/scope filter trims the underlying recall
            results — it does not over-fetch in Phase 2.
        scope:
            Optional scope filter (e.g. ``"channel:slack-#dev"``).  Phase 2
            filters in Python after recall; SQL-side scope filtering is
            wired in PR 5.
        tags:
            Required-tag filter with **AND semantics** — an entry is admitted
            only when its tag set is a *superset* of the requested tags.
            This is the contract [RFC 0011 PR plan](../../docs/rfcs/0011-pr-plan.md)
            PR 5 pins against; do not change to OR without an RFC amendment.
        min_score:
            Optional relevance floor in ``[0, 1]`` applied to FTS5 BM25
            normalised scores.  ``None`` falls back to the facade's
            ``default_min_score`` (constructor arg, typically read from
            ``config/agents.yaml`` ``memory.min_score``).
        """
        self._require_initialised()
        effective_min_score = (
            min_score if min_score is not None else self._default_min_score
        )
        # The recall + scope/tags filter loop lives in
        # :func:`agents.memory.scope_recall.recall_with_scope_filter` so
        # the persona-runtime channel-history tier (RFC 0011 PR 5
        # follow-up) shares the same overfetch + AND-tag + scope-fallback
        # contract.  The facade is the ``MemoryEntry`` projection layer
        # on top of the shared helper.
        episodes = await recall_with_scope_filter(
            self._episodic,
            query,
            limit=limit,
            scope=scope,
            tags=tags,
            min_score=effective_min_score,
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
                    # facade returns 0.0 as the unranked sentinel and
                    # consumers must not branch on it.  PR 5 promotes the
                    # score onto Episode so this can pass through.
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
            Advisory TTL hint stored on the underlying episode.  Eviction
            does not consume it in this PR — the follow-on
            ``feature/v030-rfc0008-eviction`` PR adds enforcement.  Stored
            now so callers can set it without a future migration.
        tags:
            Caller-supplied tag set.  Mutable types accepted for
            ergonomic callers; the facade normalises to ``list[str]``
            before persistence.
        outcome:
            Optional caller-supplied outcome string persisted on the
            underlying episode (RFC 0008 §B); not ranked separately.
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
            # PR-220 review M2: raise MemoryDisabledError (a RuntimeError
            # subclass) so write-side callers can catch a memory-specific
            # error type per the contract advertised in the RFC 0008 PR
            # plan.  RuntimeError-matching tests remain green because
            # MemoryDisabledError IS-A RuntimeError.
            raise MemoryDisabledError(
                "MemoryFacade not initialised — call initialize() first",
            )


# ─── Budget translation helper ─────────────────────────────────


# RFC 0008 PR plan PR 2 §Key implementation details: agents translate the
# advisory ``budget_memory_tokens`` field from the orchestrator's
# ``_context_package`` into a ``retrieve_relevant(limit=...)`` call by
# dividing by an average per-entry token cost.  The constant is calibrated
# in PR 5; until then the value matches the PR plan integration test
# (``budget=500 → limit=5``).
DEFAULT_AVG_ENTRY_TOKENS = 100


def budget_to_limit(
    budget_memory_tokens: int,
    *,
    avg_entry_tokens: int = DEFAULT_AVG_ENTRY_TOKENS,
    fallback_limit: int = 5,
) -> int:
    """Translate the advisory orchestrator budget into a recall ``limit``.

    Returns ``fallback_limit`` when the orchestrator emits 0 (the PR 1
    placeholder value — RFC 0008 PR plan PR 2 keeps the orchestrator-side
    allocator out of scope under the facade-only split).  Always returns
    ``>= 1`` so a positive budget never collapses to a no-op recall.
    """
    if budget_memory_tokens <= 0:
        return max(1, fallback_limit)
    if avg_entry_tokens <= 0:
        raise ValueError(f"avg_entry_tokens must be positive, got {avg_entry_tokens}")
    return max(1, budget_memory_tokens // avg_entry_tokens)


__all__ = [
    "Candidate",
    "CompressedView",
    "DEFAULT_AVG_ENTRY_TOKENS",
    "MemoryDisabledError",
    "MemoryEntry",
    "MemoryFacade",
    "budget_to_limit",
]
