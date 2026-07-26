"""
Agent-initiated note storage.

``NoteStore`` provides CRUD operations for structured notes that agents
persist via memory tools.  It operates on a shared ``aiosqlite``
connection managed by :class:`~agents.memory.episodic.EpisodicMemory`
and does not run its own migrations.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import TYPE_CHECKING

import aiosqlite

from ..epoch_id import DEFAULT_EPOCH_ID
from ..principal_id import DEFAULT_PRINCIPAL_ID
from ..session_id import LEGACY_SESSION_ID, normalize_session_id
from ._epoch_filter import resolve_active_epoch
from ._migration_protection import PROTECTION_LEVEL_DEFAULT
from ._notes_recall import (
    _FTS5_SPECIAL,
    _recall_notes_fts5,
    _recall_notes_like,
    _recall_notes_recency,
)
from ._principal_filter import resolve_active_principal
from ._salience import NOTES_APPEND_SALIENCE, emit_for_tier, emit_session_write
from ._session_filter import _resolve_session_list
from .note_types import _NOTE_COLS, _NOTE_SELECT, Note  # noqa: F401 — re-export

if TYPE_CHECKING:
    from collections.abc import Sequence

# ``_FTS5_SPECIAL`` is re-exported for backward compatibility with tests
# that import the regex from :mod:`agents.memory.notes` (the parent
# module is the documented entry point even though the helpers now live
# in :mod:`agents.memory._notes_recall`).  ``Note`` / the column constants
# moved to :mod:`agents.memory.note_types` (RFC 0037 PR 4 — 500-line cap)
# and are re-exported the same way.
__all__ = ["Note", "NoteStore", "_FTS5_SPECIAL"]

logger = logging.getLogger(__name__)


# Maximum content size for a single note (10 KB).
_MAX_NOTE_CONTENT_BYTES = 10_240

# Maximum number of notes returned by recall_notes() to prevent unbounded
# result sets and resource exhaustion.
_MAX_RECALL_LIMIT = 100


# ─── NoteStore ──────────────────────────────────────────────


class NoteStore:
    """Note CRUD backed by a shared ``aiosqlite`` connection.

    The caller (:class:`~agents.memory.episodic.EpisodicMemory`) is
    responsible for opening the connection, running migrations, and
    setting up FTS5 indexes.  ``NoteStore`` receives the live connection
    and the ``fts5`` availability flag.
    """

    def __init__(
        self,
        agent_id: str,
        db: aiosqlite.Connection,
        fts5: bool,
        *,
        active_session_id: str = LEGACY_SESSION_ID,
        active_principal_id: str = DEFAULT_PRINCIPAL_ID,
        active_epoch_id: str = DEFAULT_EPOCH_ID,
    ) -> None:
        self._agent_id = agent_id
        self._db = db
        self._fts5 = fts5
        # RFC 0031 Phase 2 PR 2: threaded from
        # :meth:`EpisodicMemory.initialize`'s ``resolve_session_id_silent``
        # so ``recall_notes(sessions=None)`` resolves without an env
        # read per call.  Defaults to legacy so a hand-built test
        # fixture does not have to opt in.
        self._active_session_id = active_session_id
        # ISSUE-0081 PR 3 — tenant snapshot, threaded the same way; the
        # call-time ``principal_scope`` wins via ``resolve_active_principal``.
        self._active_principal_id = active_principal_id
        # ISSUE-0085 PR 3 — epoch snapshot, threaded the same way; the
        # call-time ``epoch_scope`` wins via ``resolve_active_epoch``.
        self._active_epoch_id = active_epoch_id

    # ─── CRUD ───────────────────────────────────────────────

    async def store_note(
        self,
        topic: str,
        content: str,
        tags: list[str] | None = None,
        max_notes: int = 500,
        *,
        session_id: str = LEGACY_SESSION_ID,
        protection_level: str = PROTECTION_LEVEL_DEFAULT,
    ) -> str:
        """Store a new note. Prunes oldest low-access notes if over cap.

        Returns the generated note ID.

        ``session_id`` (RFC 0031 Phase 2 PR 1 — migration v9) tags the row
        with the operator-namespace active at write time; default
        :data:`agents.session_id.LEGACY_SESSION_ID` matches
        ``channels.DefaultSessionID`` so pre-RFC callers produce
        queryable rows.  Empty / whitespace-only values normalise to
        ``LEGACY_SESSION_ID`` to match the leaf-module contract at
        :func:`agents.session_id.resolve_session_id_silent` — without
        this, a direct caller passing ``session_id=""`` would persist an
        orphan row (NOT NULL accepts ``''``, but neither real-session
        nor legacy-carve-out filters match it).  PR 1 ships no
        recall-side filtering — that lands in a later Phase 2 PR.

        ``protection_level`` (RFC 0037 §C — v16, PR 4) persists VERBATIM:
        rule-(a) normalization is owned by the persona-side stamp site
        (the ``store_note`` tool via ``normalize_for_stamp``), which
        memory must not import.  Omitted (direct/test/operator callers)
        → the ``internal`` default; a mislabeled row fails closed at
        read time (§A rule (c)).  ``source_channel_id`` stays NULL for
        notes — a note is authored during a turn, not derived from one
        channel; its confidentiality rides the acting-level stamp alone
        (the §C synthesized-note shape).
        """
        # Validate max_notes: _prune_notes() computes
        # LIMIT MAX(0, count - max_notes + 1), so max_notes=0 would
        # delete ALL existing notes.  Reject at the public API boundary
        # even though create_memory_tools() always passes 500 (F-59-1).
        if max_notes < 1:
            raise ValueError(f"max_notes must be >= 1, got {max_notes}")
        if not topic or not topic.strip():
            raise ValueError("topic must not be empty")
        if not content or not content.strip():
            raise ValueError("content must not be empty")
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > _MAX_NOTE_CONTENT_BYTES:
            raise ValueError(
                f"content exceeds {_MAX_NOTE_CONTENT_BYTES} byte limit "
                f"({len(content_bytes)} bytes)"
            )
        # Normalise session_id at the storage boundary via the shared
        # helper (RFC 0031 Phase 2 PR 4, PR 1 F16 carry-forward — same
        # invariant now applied uniformly across the four persona-memory
        # tier write boundaries so a future fifth tier inherits it for
        # free).  Empty / whitespace-only / None → LEGACY_SESSION_ID.
        session_id = normalize_session_id(session_id)
        # ISSUE-0081 PR 3: resolve the active tenant once (scope override,
        # else construction snapshot) for both the prune scope and the row tag.
        principal_id = resolve_active_principal(self._active_principal_id)
        # ISSUE-0085 PR 3: resolve the active epoch the same way.
        epoch_id = resolve_active_epoch(self._active_epoch_id)

        # Prune scoped to ``(agent_id, session_id, principal_id, epoch_id)``
        # per PR 1 F1 carry-forward extended to the tenant + epoch axes: a
        # run-b / tenant-b / epoch-b write cannot evict a run-a row.
        # Trade-off is per-(session, principal, epoch) capacity; see
        # :meth:`_prune_notes`.
        await self._prune_notes(max_notes, session_id, principal_id, epoch_id)

        note_id = str(uuid.uuid4())
        now = time.time()
        await self._db.execute(
            """
            INSERT INTO notes
                (id, agent_id, topic, content, tags_json,
                 access_count, created_at, updated_at, session_id,
                 principal_id, epoch_id, protection_level)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                note_id,
                self._agent_id,
                topic.strip(),
                content,
                json.dumps(tags or []),
                now,
                now,
                session_id,
                principal_id,
                epoch_id,
                protection_level,
            ),
        )
        await self._db.commit()
        # RFC 0031 Phase 2 PR 1 review F2 — increment the per-session write
        # counter via the shared shim.  ``surface="note"`` so dashboards can
        # split the notes tier from the other persona-memory write surfaces
        # (see ``test_session_id_surface_granularity`` for the contract).
        emit_session_write(
            agent_id=self._agent_id, session_id=session_id, surface="note",
        )
        emit_for_tier(
            agent_id=self._agent_id,
            tier="notes",
            salience=NOTES_APPEND_SALIENCE,
        )
        return note_id

    async def recall_notes(
        self,
        query: str = "",
        *,
        limit: int = 10,
        min_score: float | None = None,
        sessions: list[str] | str | None = None,
        allowed_protection_levels: Sequence[str] | None = None,
    ) -> list[Note]:
        """Retrieve notes matching query, ranked by relevance.

        Increments access_count on returned notes.

        Parameters
        ----------
        min_score:
            Optional relevance floor in ``[0, 1]`` applied to FTS5 BM25
            normalised scores.  ``None`` → no filtering.
            LIKE-fallback path ignores this parameter per RFC 0017 Section C.
        sessions:
            RFC 0031 §D recall filter.  ``None`` (default) → active
            session plus the ``legacy`` carve-out; a non-empty list →
            those sessions plus the carve-out; ``"*"`` → all sessions
            (CLI / debug only); ``[]`` → :class:`ValueError`.  See
            :func:`agents.memory._session_filter.session_in_clause`
            for the SQL shape and the carve-out rationale.
        allowed_protection_levels:
            RFC 0037 §D read-surface gating (PR 4) — the entry protection
            levels injectable at the caller's acting classification, as
            resolved by ``persona_runtime.classification.injectable_levels``
            (the memory layer must not import the lattice, so it receives
            the resolved IN-list as plain data).  A stored label outside
            the set — including a corrupted one — is excluded, which is
            §A rule (c)'s withhold realised in SQL.  ``None`` (default)
            applies no clause: the non-persona operator/CLI surface.  The
            persona read surfaces (the injection tier and the
            ``recall_notes`` tool) always pass it; ``[]`` raises, because
            an empty allowlist means the caller resolved the acting level
            to *nothing* — a bug, not a filter.
        """
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        limit = min(limit, _MAX_RECALL_LIMIT)
        # Validate min_score range — RFC 0017 §C specifies [0.0, 1.0].
        # Mirrors the EpisodicMemory.recall() guard so misconfiguration
        # surfaces at the public boundary rather than silently no-op'ing
        # (negative) or filtering everything (>1.0). (PR #147 review.)
        if min_score is not None and not 0.0 <= min_score <= 1.0:
            raise ValueError(
                f"min_score must be in [0.0, 1.0] or None, got {min_score}"
            )
        # RFC 0037 PR 4: an explicit empty allowlist is a resolution bug
        # at the caller (``injectable_levels`` never returns fewer than
        # one level), not a legal "withhold everything" filter — raise at
        # the boundary like ``sessions=[]``.
        if allowed_protection_levels is not None and not allowed_protection_levels:
            raise ValueError("allowed_protection_levels must be None or non-empty")

        # Resolve §D session list once at the public boundary so
        # ``sessions=[]`` raises before any SQL runs; the recall helpers
        # build their own column-specific IN clauses.
        session_list = _resolve_session_list(
            sessions, self._active_session_id,
        )
        active_principal = resolve_active_principal(self._active_principal_id)
        active_epoch = resolve_active_epoch(self._active_epoch_id)

        if query and self._fts5:
            rows = await _recall_notes_fts5(
                self._db, agent_id=self._agent_id, query=query,
                limit=limit, min_score=min_score,
                sessions=session_list, note_cols=_NOTE_COLS,
                principal_id=active_principal, epoch_id=active_epoch,
                allowed_protection_levels=allowed_protection_levels,
            )
        elif query:
            rows = await _recall_notes_like(
                self._db, agent_id=self._agent_id, query=query,
                limit=limit, min_score=min_score,
                sessions=session_list, note_cols=_NOTE_COLS,
                principal_id=active_principal, epoch_id=active_epoch,
                allowed_protection_levels=allowed_protection_levels,
            )
        else:
            rows = await _recall_notes_recency(
                self._db, agent_id=self._agent_id, limit=limit,
                sessions=session_list, note_cols=_NOTE_COLS,
                principal_id=active_principal, epoch_id=active_epoch,
                allowed_protection_levels=allowed_protection_levels,
            )

        notes = [self._row_to_note(row) for row in rows]

        if notes:
            ids = [n.id for n in notes]
            placeholders = ",".join("?" for _ in ids)
            await self._db.execute(
                f"UPDATE notes SET access_count = access_count + 1 "
                f"WHERE id IN ({placeholders})",
                ids,
            )
            await self._db.commit()
            for note in notes:
                note.access_count += 1

        return notes

    async def update_note(
        self,
        note_id: str,
        content: str,
        *,
        restamp_protection_level: str | None = None,
        restamp_below: Sequence[str] = (),
    ) -> bool:
        """Update note content. Topic and tags preserved. Returns True if found.

        RFC 0031 Phase 2 PR 5 / `ISSUE-0077
        <../../docs/issues/ISSUE-0077-notes-mutation-not-session-scoped.md>`_:
        scoped to ``(agent_id, session_id IN (active, legacy))`` so a
        ``run-b`` caller cannot mutate a ``run-a`` row; the ``legacy``
        carve-out matches the recall surface (permissive policy).

        RFC 0037 §C re-stamp (PR 4): an edit re-stamps the row to
        ``max(existing protection_level, acting L)`` — never lowers.  The
        ``max`` arrives pre-resolved as data (memory must not import the
        lattice): ``restamp_protection_level`` is the acting level's
        rule-(a) stamp and ``restamp_below`` the levels ranking strictly
        below it (``persona_runtime.classification.levels_below_stamp``),
        so the raise-only rule is one SQL ``CASE`` — rows whose existing
        level is in ``restamp_below`` take the new stamp, every other row
        (equal, higher, or corrupted — the latter stays failing closed at
        read time per rule (c)) keeps its value.  Both omitted (the
        non-persona operator/CLI surface, and any pre-RFC caller) →
        content-only update, exactly the prior behaviour.
        """
        if not content or not content.strip():
            raise ValueError("content must not be empty")
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > _MAX_NOTE_CONTENT_BYTES:
            raise ValueError(
                f"content exceeds {_MAX_NOTE_CONTENT_BYTES} byte limit "
                f"({len(content_bytes)} bytes)"
            )
        now = time.time()
        # ISSUE-0081 PR 3: strict tenant equality in addition to the
        # session carve-out — a foreign principal cannot mutate this row
        # even if it knows the UUID and shares the session/legacy scope.
        # ISSUE-0085 PR 3: strict epoch equality, same reasoning — a fresh
        # epoch cannot mutate a prior run's row through the session carve-out.
        principal_id = resolve_active_principal(self._active_principal_id)
        epoch_id = resolve_active_epoch(self._active_epoch_id)
        restamp_sql = ""
        restamp_params: tuple[str, ...] = ()
        if restamp_protection_level is not None and restamp_below:
            # Raise-only: a ``public`` stamp has an empty ``restamp_below``
            # and skips the clause entirely (nothing ranks below the floor).
            placeholders = ",".join("?" for _ in restamp_below)
            restamp_sql = (
                f", protection_level = CASE WHEN protection_level "
                f"IN ({placeholders}) THEN ? ELSE protection_level END"
            )
            restamp_params = (*restamp_below, restamp_protection_level)
        cursor = await self._db.execute(
            f"UPDATE notes SET content = ?, updated_at = ?{restamp_sql} "
            "WHERE id = ? AND agent_id = ? "
            "AND session_id IN (?, ?) "
            "AND principal_id = ? "
            "AND epoch_id = ?",
            (
                content, now, *restamp_params, note_id, self._agent_id,
                self._active_session_id, LEGACY_SESSION_ID,
                principal_id, epoch_id,
            ),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def delete_note(self, note_id: str) -> bool:
        """Delete a note by ID. Returns True if found.  Session-,
        principal-, and epoch-scoped per :meth:`update_note`."""
        principal_id = resolve_active_principal(self._active_principal_id)
        epoch_id = resolve_active_epoch(self._active_epoch_id)
        cursor = await self._db.execute(
            "DELETE FROM notes "
            "WHERE id = ? AND agent_id = ? "
            "AND session_id IN (?, ?) "
            "AND principal_id = ? "
            "AND epoch_id = ?",
            (
                note_id, self._agent_id,
                self._active_session_id, LEGACY_SESSION_ID,
                principal_id, epoch_id,
            ),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def count_notes(self) -> int:
        """Number of notes visible to the active session + tenant + epoch
        (per :meth:`update_note`'s scope)."""
        principal_id = resolve_active_principal(self._active_principal_id)
        epoch_id = resolve_active_epoch(self._active_epoch_id)
        async with self._db.execute(
            "SELECT COUNT(*) FROM notes "
            "WHERE agent_id = ? "
            "AND session_id IN (?, ?) "
            "AND principal_id = ? "
            "AND epoch_id = ?",
            (
                self._agent_id,
                self._active_session_id, LEGACY_SESSION_ID,
                principal_id, epoch_id,
            ),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0

    # ─── Internal helpers ──────────────────────────────────

    async def _prune_notes(
        self, max_notes: int, session_id: str, principal_id: str,
        epoch_id: str,
    ) -> None:
        """Remove oldest low-access notes in the ``(session_id,
        principal_id, epoch_id)`` scope when count >= max_notes.

        Single atomic DELETE-with-subquery avoids a TOCTOU race between
        SELECT count and DELETE on shared-DB topologies (F-3b-1).
        RFC 0031 Phase 2 PR 2 scoped this to ``(agent_id, session_id)``;
        ISSUE-0081 PR 3 extended it to the tenant axis; ISSUE-0085 PR 3
        extends it again to the epoch axis so write-side isolation covers
        run/test isolation — a fresh-epoch write cannot evict a prior
        run's row.  Trade-off is per-(session, principal, epoch) capacity.
        """
        await self._db.execute(
            """
            DELETE FROM notes
            WHERE agent_id = ? AND session_id = ? AND principal_id = ?
              AND epoch_id = ?
              AND id IN (
                SELECT id FROM notes
                WHERE agent_id = ? AND session_id = ? AND principal_id = ?
                  AND epoch_id = ?
                ORDER BY access_count ASC, created_at ASC
                LIMIT MAX(
                    0,
                    (SELECT COUNT(*) FROM notes
                     WHERE agent_id = ? AND session_id = ?
                       AND principal_id = ? AND epoch_id = ?) - ? + 1
                )
            )
            """,
            (
                self._agent_id, session_id, principal_id, epoch_id,
                self._agent_id, session_id, principal_id, epoch_id,
                self._agent_id, session_id, principal_id, epoch_id,
                max_notes,
            ),
        )

    def _row_to_note(self, row: aiosqlite.Row) -> Note:
        """Convert a database row to a Note dataclass.

        Positional indices match :data:`_NOTE_COLS`; the projection
        contract pin forces this mapping, ``_NOTE_COLS``, and
        :class:`Note` to move together.
        """
        return Note(
            id=row[0],
            agent_id=row[1],
            topic=row[2],
            content=row[3],
            tags=json.loads(row[4]) if row[4] else [],
            access_count=row[5],
            created_at=row[6],
            updated_at=row[7],
            session_id=row[8],
            # RFC 0037 §C (migration v16): surfaced for the §D gate.
            protection_level=row[9],
            source_channel_id=row[10],
        )
