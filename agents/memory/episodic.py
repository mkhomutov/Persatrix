"""
Episodic memory — long-term storage of past interactions.

Stores summaries of conversations, decisions, and outcomes in SQLite
with FTS5 full-text search for relevance-ranked retrieval.

Also provides agent-initiated note storage (migration v2) for
structured knowledge the agent chooses to persist, delegated to
:class:`~agents.memory.notes.NoteStore`.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import TYPE_CHECKING, Any

import aiosqlite
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from ..epoch_id import resolve_epoch_id_silent
from ..observability.spans import (
    EPISODIC_RECALL_SPAN,
    EPISODIC_REMEMBER_SPAN,
)
from ..principal_id import resolve_principal_id_silent
from ..session_id import current_session_id, normalize_session_id, resolve_session_id_silent
from ._boundary import warn_external_construction
from ._epoch_filter import resolve_active_epoch
from ._principal_filter import resolve_active_principal
from ._salience import EPISODIC_APPEND_SALIENCE, emit_for_tier, emit_session_write
from ._session_filter import _resolve_session_list
from .episodic_crud import (
    count_episodes as _count_episodes,
)
from .episodic_crud import (
    delete_episode as _delete_episode,
)
from .episodic_crud import (
    get_episode as _get_episode,
)
from .episodic_notes_api import _EpisodicNotesAPIMixin
from .episodic_queries import (
    MAX_RECALL_LIMIT,
    Episode,
    get_interaction_count,
    increment_interaction_count,
    insert_episode,
    load_agent_state,
    persist_agent_state,
    recall_fts5,
    recall_like,
    recall_recency,
    reset_interaction_count,
    row_to_episode,
)
from .episodic_queries import (
    update_episode_summary as _update_episode_summary,
)
from .episodic_retention import (
    delete_old_episodes as _delete_old_episodes,
)
from .episodic_retention import (
    summarize_old_episodes as _summarize_old_episodes,
)
from .interactions import SUMMARY_PENDING_TEXT
from .migrations import (
    _FTS5_DDL,
    _NOTES_FTS5_DDL,
    _apply_migrations,
    _fts5_available,
)
from .notes import NoteStore

_tracer = trace.get_tracer(__name__)

if TYPE_CHECKING:
    from ..llm_client import LLMClient

logger = logging.getLogger(__name__)


# ─── Per-tier min_score defaults (RFC 0017 §C) ────────────────────────────────
# Calibrated against a representative FTS5 BM25 score distribution:
# queries with a clear topic match produce |rank| ≈ 1.5–4.0 (normalised ≈ 0.20–0.40);
# low-signal queries ("hi", empty TICK boilerplate) produce |rank| ≥ 5 or no rows
# (normalised ≤ 0.17).  Thresholds are conservative to avoid over-filtering;
# operators can tighten them via caller overrides without API changes.
#
# Public API (PR 6 — RFC 0017 PR 4 review finding 1): the persona runtime is
# already a consumer and RFC 0008's vector tier will be the third.  Public
# names avoid ``ruff PLC2701`` (``import-private-name``) at consumer sites
# and signal the cross-module contract.
DEFAULT_EPISODIC_MIN_SCORE: float = 0.20
DEFAULT_NOTES_MIN_SCORE: float = 0.20


# ─── EpisodicMemory ────────────────────────────────────────


class EpisodicMemory(_EpisodicNotesAPIMixin):
    """Long-term memory store using SQLite with FTS5 search.

    The notes-tier delegation methods (``store_note`` / ``recall_notes`` /
    ``update_note`` / ``delete_note`` / ``count_notes``) live in
    :class:`agents.memory.episodic_notes_api._EpisodicNotesAPIMixin` to
    keep this file under the 500-line repo cap; the public surface is
    unchanged.
    """

    def __init__(self, agent_id: str, db_path: str = "data/memory.db") -> None:
        warn_external_construction("EpisodicMemory")  # RFC 0029 — facade-only tier
        self._agent_id = agent_id
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._fts5: bool = False
        self._note_store: NoteStore | None = None
        # RFC 0031 Phase 2 PR 2 — tier-owned (see ``recall`` docstring).
        self._active_session_id = resolve_session_id_silent()
        # ISSUE-0081 PR 3 — tenant snapshot (call-time scope wins on use).
        self._active_principal_id = resolve_principal_id_silent()
        # ISSUE-0085 PR 3 — epoch (run/test) snapshot, same shape.
        self._active_epoch_id = resolve_epoch_id_silent()

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def has_fts5(self) -> bool:
        """Return True when the underlying SQLite build provides FTS5.

        Public, read-only view of the internal ``_fts5`` flag set during
        :meth:`initialize`.  Tests and operators that need to gate on FTS5
        availability should use this property instead of reaching into the
        private attribute.  (PR 6 — RFC 0017 PR 4 review finding 3.)
        """
        return self._fts5

    async def initialize(self) -> None:
        """Open database, run migrations, set up FTS5 if available."""
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")

        await _apply_migrations(self._db)

        # Verify that ln() is available — the scoring formula in _SCORE_EXPR
        # uses SQLite's ln() function, which requires SQLITE_ENABLE_MATH_FUNCTIONS
        # at compile time.  Python's bundled SQLite typically includes this, but
        # custom builds or Alpine musl-based Docker images may not.  Failing
        # here with a clear message is better than a cryptic error at recall time
        # (PR #59 review: ln() startup diagnostic).
        try:
            async with self._db.execute("SELECT ln(1)") as cursor:
                await cursor.fetchone()
        except sqlite3.OperationalError:
            raise RuntimeError(
                "SQLite ln() function not available — required by recall scoring. "
                "Ensure Python is built with SQLITE_ENABLE_MATH_FUNCTIONS."
            )

        self._fts5 = await _fts5_available(self._db)
        if self._fts5:
            await self._db.executescript(_FTS5_DDL)
            await self._db.executescript(_NOTES_FTS5_DDL)
            await self._db.commit()
            logger.info("FTS5 enabled for episodic memory")
        else:
            logger.warning(
                "FTS5 not available — falling back to LIKE-based queries. "
                "Performance degrades beyond ~1000 episodes per agent. "
                "Install a Python build with FTS5 support for production use."
            )

        self._note_store = NoteStore(
            agent_id=self._agent_id, db=self._db, fts5=self._fts5,
            active_session_id=self._active_session_id,
            active_principal_id=self._active_principal_id,
            active_epoch_id=self._active_epoch_id,
        )

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None
            self._note_store = None

    def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("EpisodicMemory not initialized — call initialize() first")
        return self._db

    def _ensure_note_store(self) -> NoteStore:
        if self._note_store is None:
            raise RuntimeError("EpisodicMemory not initialized — call initialize() first")
        return self._note_store

    # ─── Episode CRUD ──────────────────────────────────────

    async def store_episode(
        self,
        summary: str,
        context: dict[str, Any],
        outcome: str | None = None,
        importance: float = 0.5,
        tags: list[str] | None = None,
        *,
        interaction_id: str | None = None,
        started_at: float | None = None,
        closed_at: float | None = None,
        turn_count: int | None = None,
        scope: str | None = None,
        governance_interaction_id: str | None = None,
        session_id: str = "legacy",
        surface: str = "episode",
    ) -> str:
        """Store a new episode. Returns the generated episode ID.

        The keyword-only ``interaction_id`` / ``started_at`` / ``closed_at`` /
        ``turn_count`` / ``scope`` populate the RFC 0020 §D columns (v5), and
        ``governance_interaction_id`` the RFC 0030 governance id the episode
        opened under (ISSUE-0102 PR 2 — v15).  Pre-RFC callers omit them and the
        row keeps ``NULL`` — recall treats those as legacy single-turn episodes
        per RFC 0020 §I.

        ``session_id`` (RFC 0031 Phase 1 — migration v7) tags the row with the
        operator-namespace active at write time; default ``"legacy"`` matches
        ``channels.DefaultSessionID``.  Phase 1 ships no recall-side filtering.

        ``surface`` (PR 4 F2) tags ``sessions.writes`` only — not persisted.
        """
        with _tracer.start_as_current_span(
            EPISODIC_REMEMBER_SPAN,
            attributes={
                "agent.id": self._agent_id,
                "episode.kind": "episode",
            },
        ) as span:
            # Mirror the LLM/tool span error contract: validation /
            # persistence failures must mark the span ERROR before
            # propagating the exception, otherwise operators searching
            # traces for failed remembers see them as "successful".
            try:
                db = self._ensure_db()
                if not summary or not summary.strip():
                    raise ValueError("summary must not be empty")
                # Clamp importance to [0.0, 1.0] — the scoring formula assumes
                # non-negative values; negative importance would invert ranking.
                if not 0.0 <= importance <= 1.0:
                    logger.warning("importance=%.4f out of [0.0, 1.0] range, clamping", importance)
                    importance = max(0.0, min(1.0, importance))
                session_id = normalize_session_id(session_id)  # PR 4 / F16
                # ISSUE-0081 PR 3: tag the row with the active tenant.
                principal_id = resolve_active_principal(self._active_principal_id)
                # ISSUE-0085 PR 3: tag the row with the active epoch.
                epoch_id = resolve_active_epoch(self._active_epoch_id)
                episode_id = await insert_episode(
                    db, self._agent_id,
                    summary=summary, context=context, outcome=outcome,
                    importance=importance, tags=tags,
                    interaction_id=interaction_id, started_at=started_at,
                    closed_at=closed_at, turn_count=turn_count,
                    session_id=session_id, scope=scope,
                    governance_interaction_id=governance_interaction_id,
                    principal_id=principal_id, epoch_id=epoch_id,
                )
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            # RFC 0031 Phase 1 sessions.writes (PR #337 M1) — the shim
            # suppresses OTEL errors so a metric failure is not a write failure.
            emit_session_write(
                agent_id=self._agent_id, session_id=session_id, surface=surface,
            )
        # Emit *after* the EPISODIC_REMEMBER_SPAN closes so ``source_span_id``
        # captures the parent span (the LLM-call span when the write
        # originates inside the action loop) rather than the inner episodic
        # span — PR 3b's loop-back guard input (RFC 0024 §F).
        emit_for_tier(
            agent_id=self._agent_id,
            tier="episodic",
            salience=EPISODIC_APPEND_SALIENCE,
        )
        return episode_id

    async def update_episode_summary(self, interaction_id: str, summary: str) -> bool:
        return await _update_episode_summary(
            self._ensure_db(), self._agent_id, interaction_id, summary)

    async def recall(
        self,
        query: str = "",
        *,
        limit: int = 10,
        min_importance: float = 0.0,
        min_score: float | None = None,
        sessions: list[str] | str | None = None,
    ) -> list[Episode]:
        """Retrieve relevant episodes ranked by composite score.

        Uses FTS5 BM25 when available, falls back to LIKE.
        Increments access_count on returned entries.

        Parameters
        ----------
        min_score:
            Optional relevance floor in ``[0, 1]`` applied to FTS5 BM25
            normalised scores.  ``None`` → no filtering (current behaviour).
            LIKE-fallback path ignores this parameter (all LIKE matches score
            ``1.0`` per RFC 0017 Section C).
        sessions:
            RFC 0031 §D recall filter — see ``_resolve_session_list``.
        """
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        if limit > MAX_RECALL_LIMIT:
            logger.warning(
                "limit=%d exceeds maximum (%d), capping",
                limit, MAX_RECALL_LIMIT,
            )
            limit = MAX_RECALL_LIMIT
        # Validate min_score range — RFC 0017 §C specifies [0.0, 1.0].
        # Out-of-range values silently no-op (negative) or filter everything
        # (>1.0), making misconfiguration hard to debug in production.
        # Mirrors the existing `limit` guard above (PR #147 review).
        if min_score is not None and not 0.0 <= min_score <= 1.0:
            raise ValueError(
                f"min_score must be in [0.0, 1.0] or None, got {min_score}"
            )
        session_list = _resolve_session_list(sessions, self._active_session_id)
        active_principal = resolve_active_principal(self._active_principal_id)
        active_epoch = resolve_active_epoch(self._active_epoch_id)
        with _tracer.start_as_current_span(
            EPISODIC_RECALL_SPAN,
            attributes={
                "agent.id": self._agent_id,
                "query.kind": "recall",
                "query.empty": not query,
                # OQ #7/0081: active session (scope over snapshot), not the filter shape.
                "session_id": current_session_id() or self._active_session_id,
                "principal_id": active_principal,  # ISSUE-0081 PR 3
                "epoch_id": active_epoch,  # ISSUE-0085 PR 3
            },
        ) as span:
            # Mirror the LLM/tool/store_episode span error contract: a SQLite
            # failure inside the recall path must mark the span ERROR before
            # propagating, otherwise operators searching traces for failed
            # recalls see them as "successful" (PR #167 review Should-Fix).
            try:
                # Emit ``min_score`` only when the caller set one — overloading
                # the numeric range with a ``-1.0`` sentinel made the attribute
                # ambiguous to dashboards and to operators reading raw spans.
                if min_score is not None:
                    span.set_attribute("min_score", min_score)
                db = self._ensure_db()

                if query and self._fts5:
                    rows = await recall_fts5(
                        db, self._agent_id, query, limit, min_importance,
                        min_score, sessions=session_list,
                        principal_id=active_principal, epoch_id=active_epoch,
                    )
                elif query:
                    rows = await recall_like(
                        db, self._agent_id, query, limit, min_importance,
                        min_score, sessions=session_list,
                        principal_id=active_principal, epoch_id=active_epoch,
                    )
                else:
                    rows = await recall_recency(
                        db, self._agent_id, limit, min_importance,
                        sessions=session_list, principal_id=active_principal,
                        epoch_id=active_epoch,
                    )

                # RFC 0020 PR 5 / PR-262 review M1: drop unfinalised
                # closing rows at this chokepoint so the facade, persona
                # prompt assembly, and shared-pool callers are all
                # covered by one filter (the persona path bypasses the
                # facade and reads ``recall`` directly). The janitor's
                # ``SUMMARY_UNAVAILABLE_TEXT`` fallback stays visible —
                # it is the operator's signal that summarisation failed.
                # Full rationale + race-window analysis lives in the
                # test docstring at
                # tests/unit/python/test_episodic_memory_pending_filter.py.
                episodes = [
                    ep
                    for ep in (row_to_episode(row) for row in rows)
                    if ep.summary != SUMMARY_PENDING_TEXT
                ]
                span.set_attribute("result.count", len(episodes))

                # Increment access_count and update last_accessed_at
                if episodes:
                    now = time.time()
                    ids = [e.id for e in episodes]
                    placeholders = ",".join("?" for _ in ids)
                    await db.execute(
                        f"UPDATE episodes SET access_count = access_count + 1, "
                        f"last_accessed_at = ? WHERE id IN ({placeholders})",
                        [now, *ids],
                    )
                    await db.commit()
                    # Update in-memory objects to reflect the increment
                    for ep in episodes:
                        ep.access_count += 1
                        ep.last_accessed_at = now

                return episodes
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise

    async def get_episode(self, episode_id: str) -> Episode | None:
        """Retrieve a single episode by ID (agent-scoped)."""
        return await _get_episode(self._ensure_db(), self._agent_id, episode_id)

    async def count_episodes(self) -> int:
        """Return the number of episodes for this agent."""
        return await _count_episodes(self._ensure_db(), self._agent_id)

    # ─── Episode summarization & retention ──────────────────

    async def summarize_old_episodes(
        self,
        older_than_days: float,
        llm_client: LLMClient,
        *,
        compression_model: str = "summarizer",
        batch_size: int = 50,
    ) -> int:
        """Summarize raw episodes older than *older_than_days*.

        See :func:`~agents.memory.episodic_retention.summarize_old_episodes`
        for full documentation.
        """
        return await _summarize_old_episodes(
            self._ensure_db(), self._agent_id, older_than_days, llm_client,
            compression_model=compression_model, batch_size=batch_size,
        )

    async def delete_old_episodes(self, older_than_days: float) -> int:
        """Delete compressed episodes older than *older_than_days*.

        See :func:`~agents.memory.episodic_retention.delete_old_episodes`
        for full documentation.
        """
        return await _delete_old_episodes(
            self._ensure_db(), self._agent_id, older_than_days,
        )

    async def delete_episode(self, episode_id: str) -> bool:
        """Delete a single episode by ID, agent-scoped. RFC 0008 PR 3a / N5."""
        return await _delete_episode(self._ensure_db(), self._agent_id, episode_id)

    # ─── Notes ─────────────────────────────────────────────
    # ``store_note`` / ``recall_notes`` / … — see :class:`_EpisodicNotesAPIMixin`.

    # ─── Interaction counter ─────────────────────────────────

    async def get_interaction_count(self) -> int:
        """Get the current interaction count for this agent."""
        return await get_interaction_count(self._ensure_db(), self._agent_id)

    async def increment_interaction_count(self) -> int:
        """Increment and return the new interaction count (upsert).

        Uses RETURNING to get the post-upsert count in a single round-trip,
        eliminating a read-after-write race.  Requires SQLite >= 3.35
        (Python 3.11+ ships >= 3.39).
        """
        return await increment_interaction_count(self._ensure_db(), self._agent_id)

    async def reset_interaction_count(self) -> None:
        """Reset the interaction counter to zero."""
        await reset_interaction_count(self._ensure_db(), self._agent_id)

    # ─── Persona state persistence ──────────────────────────

    async def persist_agent_state(
        self, agent_id: str, state_json: str,
    ) -> None:
        """Persist opaque agent state JSON to the agent_state table (upsert).

        Preserves interaction_count: only persona_state_json and updated_at
        are overwritten by the upsert, so call-count tracking is not reset.
        """
        await persist_agent_state(self._ensure_db(), agent_id, state_json)

    async def load_agent_state(self, agent_id: str) -> str | None:
        """Load opaque agent state JSON from the agent_state table.

        Returns ``None`` if no state has been persisted for this agent.
        """
        return await load_agent_state(self._ensure_db(), agent_id)
