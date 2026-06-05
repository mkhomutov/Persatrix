"""
Relationship memory — tracks per-agent-pair trust, interaction patterns, and history.

Shares the SQLite database and migration infrastructure with episodic memory.
Trust scores are bidirectionally decayed toward 0.5 (neutral) to prevent
permanent grudges in long-running simulations.

SQL query helpers live in :mod:`.relationship_queries`;
write/mutation helpers live in :mod:`.relationship_mutations`.
"""

from __future__ import annotations

import logging

import aiosqlite

from ..epoch_id import resolve_epoch_id_silent
from ..principal_id import resolve_principal_id_silent
from ..session_id import resolve_session_id_silent
from ._boundary import warn_external_construction
from ._epoch_filter import resolve_active_epoch
from ._principal_filter import resolve_active_principal
from ._salience import RELATIONSHIP_APPEND_SALIENCE, emit_for_tier
from ._session_filter import _resolve_session_list
from .migrations import _apply_migrations
from .relationship_mutations import (
    apply_decay as _apply_decay,
)
from .relationship_mutations import (
    record_interaction as _record_interaction,
)
from .relationship_mutations import (
    seed_trust as _seed_trust,
)
from .relationship_mutations import (
    update_trust as _update_trust,
)
from .relationship_mutations import (
    upsert_identity as _upsert_identity,
)
from .relationship_queries import (
    get_all_relationships as _get_all_relationships,
)
from .relationship_queries import (
    get_identity as _get_identity,
)
from .relationship_queries import (
    get_relationship_summary as _get_relationship_summary,
)
from .relationship_queries import (
    get_trust as _get_trust,
)
from .relationship_types import (
    _DEFAULT_TRUST,
    _MAX_RECENT_INTERACTIONS,
    _MAX_TRUST_DELTA,
    Interaction,
    RelationshipSummary,
)

logger = logging.getLogger(__name__)

__all__ = [
    "RelationshipMemory",
    "Interaction",
    "RelationshipSummary",
    "_DEFAULT_TRUST",
    "_MAX_RECENT_INTERACTIONS",
    "_MAX_TRUST_DELTA",
]


class RelationshipMemory:
    """Per-participant-pair trust and interaction tracking.

    Stores trust scores and interaction history in SQLite, sharing the
    database file with ``EpisodicMemory``. Trust is scoped to
    ``(participant_id, participant_type, other_participant_id,
    other_participant_type)`` tuples.

    All methods accept optional ``participant_type`` and
    ``other_participant_type`` keyword arguments (default ``"agent"``).
    Existing callers are unaffected.
    """

    def __init__(self, agent_id: str, db_path: str = "data/memory.db") -> None:
        warn_external_construction("RelationshipMemory")  # RFC 0029 — facade-only tier
        self._agent_id = agent_id
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        # RFC 0031 Phase 2 PR 3 — tier-owned active session (mirrors
        # EpisodicMemory).  The persona-direct read path bypasses the
        # MemoryStore facade, so the tier must resolve its own active
        # session for ``sessions=None`` recall to be correct there too.
        self._active_session_id = resolve_session_id_silent()
        # ISSUE-0081 PR 3 — tenant snapshot; call-time ``principal_scope``
        # wins via ``resolve_active_principal`` on recall + write paths.
        self._active_principal_id = resolve_principal_id_silent()
        # ISSUE-0085 PR 3 — epoch snapshot; call-time ``epoch_scope`` wins
        # via ``resolve_active_epoch`` on recall + write paths.
        self._active_epoch_id = resolve_epoch_id_silent()

    @property
    def agent_id(self) -> str:
        return self._agent_id

    async def initialize(
        self,
        config_relationships: list[dict[str, object]] | None = None,
        *,
        session_id: str = "legacy",
    ) -> None:
        """Open database, run migrations, seed trust from config.

        *config_relationships* is the ``relationships`` list from the
        agent's YAML config (e.g. ``[{"agent_id": "mike", "trust_level": 0.9}]``).
        Existing trust scores are never overwritten — config only seeds
        the initial state.

        ``session_id`` (RFC 0031 Phase 1; default ``"legacy"``) tags any
        newly-inserted seed rows.  Persona-runtime threads the resolved
        ``PERSATRIX_SESSION_ID`` here so a peer pre-declared in YAML
        config takes the active session's tag rather than the column
        default (RFC 0031 PR plan PR 4 finding #2).
        """
        # Guard against double-initialize: close any existing connection
        # to prevent file descriptor and SQLite connection leaks.
        if self._db is not None:
            await self._db.close()

        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await _apply_migrations(self._db)

        if config_relationships:
            await _seed_trust(
                self._db, self._agent_id, config_relationships,
                session_id=session_id,
                principal_id=resolve_active_principal(self._active_principal_id),
                epoch_id=resolve_active_epoch(self._active_epoch_id),
            )

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError(
                "RelationshipMemory not initialized — call initialize() first"
            )
        return self._db

    # ─── Trust CRUD ─────────────────────────────────────────

    async def get_trust(
        self,
        other_id: str,
        *,
        participant_type: str = "agent",
        other_participant_type: str = "agent",
        sessions: list[str] | str | None = None,
    ) -> float:
        """Get current trust score for another participant (0.0–1.0).

        Returns the default (0.5) if no relationship exists.

        ``sessions`` (RFC 0031 Phase 2 PR 3) — see
        :func:`agents.memory._session_filter._resolve_session_list` for
        the four-mode contract.  Default ``None`` resolves to the
        tier's active session plus the always-visible ``legacy``
        carve-out; a row in another session yields the neutral default
        so a foreign-session trust value cannot leak into the prompt.
        """
        session_list = _resolve_session_list(
            sessions, self._active_session_id,
        )
        return await _get_trust(
            self._ensure_db(), self._agent_id, other_id,
            participant_type=participant_type,
            other_participant_type=other_participant_type,
            sessions=session_list,
            principal_id=resolve_active_principal(self._active_principal_id),
            epoch_id=resolve_active_epoch(self._active_epoch_id),
        )

    async def update_trust(
        self,
        other_id: str,
        delta: float,
        reason: str,
        *,
        participant_type: str = "agent",
        other_participant_type: str = "agent",
    ) -> float:
        """Update trust score. Returns new value (clamped to [0.0, 1.0]).

        *delta* is clamped to ±0.2 to prevent single interactions from
        swinging trust dramatically.

        .. note::

           The *reason* overwrites the previous ``notes`` value on the
           relationship row — only the most recent trust-change reason
           is retained.
        """
        return await _update_trust(
            self._ensure_db(), self._agent_id, other_id, delta, reason,
            participant_type=participant_type,
            other_participant_type=other_participant_type,
            principal_id=resolve_active_principal(self._active_principal_id),
            epoch_id=resolve_active_epoch(self._active_epoch_id),
        )

    # ─── Person identity (cross-room) ───────────────────────

    async def upsert_identity(
        self,
        other_id: str,
        fields: dict[str, object],
        *,
        participant_type: str = "agent",
        other_participant_type: str = "agent",
    ) -> dict[str, object]:
        """Merge person-identity ``fields`` onto the relationship record.

        RFC 0031 amendment (F-7 Option D, ISSUE-0093) — the cross-room
        write for person identity (name / role / stable preferences).
        Non-destructive merge (scalar last-writer-wins; ``prefs`` union);
        creates the row at neutral trust if absent; **never touches the
        trust ``notes`` column**.  Identity lives on the relationship row
        (PK omits ``session_id``), so it is cross-room by construction.
        Returns the merged identity that was persisted.
        """
        return await _upsert_identity(
            self._ensure_db(), self._agent_id, other_id, fields,
            participant_type=participant_type,
            other_participant_type=other_participant_type,
            principal_id=resolve_active_principal(self._active_principal_id),
            epoch_id=resolve_active_epoch(self._active_epoch_id),
        )

    async def get_identity(
        self,
        other_id: str,
        *,
        participant_type: str = "agent",
        other_participant_type: str = "agent",
    ) -> dict[str, object] | None:
        """Read the structured person identity off the relationship record.

        RFC 0031 amendment (F-7 Option D, ISSUE-0093) — the cross-room
        read.  Applies principal/epoch strict equality but **no session
        filter** (see :func:`agents.memory.relationship_queries.get_identity`)
        so identity stated in one room surfaces in every room for the same
        ``(principal, epoch)``.  Returns ``None`` when no identity is
        recorded for the pair.
        """
        return await _get_identity(
            self._ensure_db(), self._agent_id, other_id,
            participant_type=participant_type,
            other_participant_type=other_participant_type,
            principal_id=resolve_active_principal(self._active_principal_id),
            epoch_id=resolve_active_epoch(self._active_epoch_id),
        )

    async def apply_decay(
        self,
        decay_rate: float = 0.01,
        *,
        participant_type: str = "agent",
    ) -> int:
        """Decay all trust scores toward 0.5 (neutral).

        Formula: ``new = old + decay_rate * (0.5 - old)``.
        Rows within 0.001 of neutral are skipped.  Decays all
        ``other_participant_type`` values uniformly (PR #120 F-5).

        Returns the number of relationships updated.
        """
        return await _apply_decay(
            self._ensure_db(), self._agent_id, decay_rate,
            participant_type=participant_type,
            principal_id=resolve_active_principal(self._active_principal_id),
            epoch_id=resolve_active_epoch(self._active_epoch_id),
        )

    # ─── Interaction recording ──────────────────────────────

    async def record_interaction(
        self,
        other_id: str,
        interaction_type: str,
        outcome: str | None = None,
        sentiment: float = 0.0,
        *,
        participant_type: str = "agent",
        other_participant_type: str = "agent",
        session_id: str = "legacy",
    ) -> str:
        """Record an interaction with another participant.

        Inserts into the ``interactions`` table and increments the
        ``interaction_count`` on the relationship. Creates the
        relationship row if it doesn't exist.

        ``session_id`` (RFC 0031 Phase 1; default ``"legacy"``) tags the
        relationship row on first-seen INSERT.  See
        :func:`agents.memory.relationship_mutations.record_interaction`
        for the per-row tagging contract.

        Returns the generated interaction ID.
        """
        interaction_id = await _record_interaction(
            self._ensure_db(), self._agent_id, other_id, interaction_type,
            outcome, sentiment,
            participant_type=participant_type,
            other_participant_type=other_participant_type,
            session_id=session_id,
            principal_id=resolve_active_principal(self._active_principal_id),
            epoch_id=resolve_active_epoch(self._active_epoch_id),
        )
        emit_for_tier(
            agent_id=self._agent_id,
            tier="relationship",
            salience=RELATIONSHIP_APPEND_SALIENCE,
        )
        return interaction_id

    # ─── Queries ────────────────────────────────────────────

    async def get_relationship_summary(
        self,
        other_id: str,
        *,
        participant_type: str = "agent",
        other_participant_type: str = "agent",
        sessions: list[str] | str | None = None,
    ) -> RelationshipSummary:
        """Get full relationship context for injection into LLM prompt.

        ``sessions`` (RFC 0031 Phase 2 PR 3) — see :meth:`get_trust`.
        A row in a non-active non-legacy session yields the "no
        relationship" summary, matching :meth:`get_trust`.
        """
        session_list = _resolve_session_list(
            sessions, self._active_session_id,
        )
        return await _get_relationship_summary(
            self._ensure_db(), self._agent_id, other_id,
            participant_type=participant_type,
            other_participant_type=other_participant_type,
            sessions=session_list,
            principal_id=resolve_active_principal(self._active_principal_id),
            epoch_id=resolve_active_epoch(self._active_epoch_id),
        )

    async def get_all_relationships(
        self,
        *,
        participant_type: str = "agent",
        sessions: list[str] | str | None = None,
    ) -> list[RelationshipSummary]:
        """Get summaries for all known relationships of this agent.

        .. note::

           ``recent_interactions`` is not populated in returned summaries
           (defaults to ``[]``) to avoid N+1 queries. Use
           ``get_relationship_summary()`` for individual relationships
           with full interaction history.

        ``sessions`` (RFC 0031 Phase 2 PR 3) — see :meth:`get_trust`.
        """
        session_list = _resolve_session_list(
            sessions, self._active_session_id,
        )
        return await _get_all_relationships(
            self._ensure_db(), self._agent_id,
            participant_type=participant_type,
            sessions=session_list,
            principal_id=resolve_active_principal(self._active_principal_id),
            epoch_id=resolve_active_epoch(self._active_epoch_id),
        )
