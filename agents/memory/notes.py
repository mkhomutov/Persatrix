"""
Agent-initiated note storage.

``NoteStore`` provides CRUD operations for structured notes that agents
persist via memory tools.  It operates on a shared ``aiosqlite``
connection managed by :class:`~agents.memory.episodic.EpisodicMemory`
and does not run its own migrations.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field

import aiosqlite

from ..observability.metrics import try_get_instruments
from ..session_id import LEGACY_SESSION_ID
from ._salience import NOTES_APPEND_SALIENCE, emit_for_tier
from ._session_filter import _resolve_session_list, session_in_clause
from .episodic_queries import resolve_min_score

logger = logging.getLogger(__name__)


# ─── Data model ─────────────────────────────────────────────


@dataclass
class Note:
    """An agent-initiated note persisted via memory tools.

    ``session_id`` (RFC 0031 Phase 2 PR 2) is on the recall projection;
    defaults to :data:`agents.session_id.LEGACY_SESSION_ID` so a hand-
    constructed test fixture round-trips without opting in.
    """

    id: str
    agent_id: str
    topic: str
    content: str
    tags: list[str] = field(default_factory=list)
    access_count: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    session_id: str = LEGACY_SESSION_ID


# FTS5 MATCH operator characters that cause parse errors when present in
# freeform (LLM-generated or user-generated) queries.  Rather than
# enumerate every problematic character (colons, commas, periods, angle
# brackets, pipes, etc.), strip all non-alphanumeric characters except
# spaces.  This keeps meaningful search tokens while preventing FTS5
# syntax errors that force repeated LIKE fallbacks.
_FTS5_SPECIAL = re.compile(r'[^a-zA-Z0-9\s]+')

# Maximum content size for a single note (10 KB).
_MAX_NOTE_CONTENT_BYTES = 10_240

# Maximum number of notes returned by recall_notes() to prevent unbounded
# result sets and resource exhaustion.
_MAX_RECALL_LIMIT = 100

# Column list for SELECT queries on the notes table.  RFC 0031 Phase 2
# PR 2: ``session_id`` joined the projection — the dataclass, the
# projection, and ``_row_to_note`` MUST move together (contract pin at
# ``test_session_id_notes_migration.TestNotesProjectionContract``).
_NOTE_COLS = (
    "id", "agent_id", "topic", "content", "tags_json",
    "access_count", "created_at", "updated_at",
    "session_id",
)
_NOTE_SELECT = ", ".join(_NOTE_COLS)


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

    # ─── CRUD ───────────────────────────────────────────────

    async def store_note(
        self,
        topic: str,
        content: str,
        tags: list[str] | None = None,
        max_notes: int = 500,
        *,
        session_id: str = LEGACY_SESSION_ID,
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
        # Normalise session_id at the storage boundary to mirror
        # agents.session_id.resolve_session_id_silent's contract: empty
        # / whitespace-only collapses to LEGACY_SESSION_ID.  Prevents a
        # direct programmatic caller (or test fixture) from persisting
        # an orphan row that escapes both real-session and legacy filters
        # once Phase 2 recall lands.  (PR 1 review F4.)
        session_id = (session_id or "").strip() or LEGACY_SESSION_ID

        # Prune scoped to ``(agent_id, session_id)`` per PR 1 F1 carry-
        # forward: a run-b write cannot evict a run-a row.  Trade-off is
        # per-session capacity; see :meth:`_prune_notes` for details.
        await self._prune_notes(max_notes, session_id)

        note_id = str(uuid.uuid4())
        now = time.time()
        await self._db.execute(
            """
            INSERT INTO notes
                (id, agent_id, topic, content, tags_json,
                 access_count, created_at, updated_at, session_id)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
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
            ),
        )
        await self._db.commit()
        # RFC 0031 Phase 2 PR 1 review F2 — increment the per-session
        # write counter, matching the episodes / relationships / facts
        # tiers' emit shape.  ``surface="note"`` so dashboards can split
        # the notes tier from the other persona-memory write surfaces
        # (see ``test_session_id_surface_granularity`` for the contract).
        # Wrapped in ``contextlib.suppress`` so a metric-backend failure
        # cannot mark the write failed after ``db.commit()`` — same
        # failure-isolation contract as ``store_episode`` (PR #337 M1).
        with contextlib.suppress(Exception):
            inst = try_get_instruments()
            if inst is not None:
                inst.sessions_writes.add(
                    1,
                    attributes={
                        "session_id": session_id,
                        "agent.id": self._agent_id,
                        "surface": "note",
                    },
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

        # Resolve §D session list once at the public boundary so
        # ``sessions=[]`` raises before any SQL runs; the recall helpers
        # build their own column-specific IN clauses.
        session_list = _resolve_session_list(
            sessions, self._active_session_id,
        )

        if query and self._fts5:
            rows = await self._recall_notes_fts5(
                query, limit, min_score, session_list,
            )
        elif query:
            rows = await self._recall_notes_like(
                query, limit, min_score, session_list,
            )
        else:
            rows = await self._recall_notes_recency(limit, session_list)

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

    async def update_note(self, note_id: str, content: str) -> bool:
        """Update note content. Topic and tags preserved. Returns True if found."""
        if not content or not content.strip():
            raise ValueError("content must not be empty")
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > _MAX_NOTE_CONTENT_BYTES:
            raise ValueError(
                f"content exceeds {_MAX_NOTE_CONTENT_BYTES} byte limit "
                f"({len(content_bytes)} bytes)"
            )
        now = time.time()
        cursor = await self._db.execute(
            "UPDATE notes SET content = ?, updated_at = ? "
            "WHERE id = ? AND agent_id = ?",
            (content, now, note_id, self._agent_id),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def delete_note(self, note_id: str) -> bool:
        """Delete a note by ID (agent-scoped). Returns True if found."""
        cursor = await self._db.execute(
            "DELETE FROM notes WHERE id = ? AND agent_id = ?",
            (note_id, self._agent_id),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def count_notes(self) -> int:
        """Return the number of notes for this agent."""
        async with self._db.execute(
            "SELECT COUNT(*) FROM notes WHERE agent_id = ?",
            (self._agent_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0

    # ─── Internal helpers ──────────────────────────────────

    async def _prune_notes(self, max_notes: int, session_id: str) -> None:
        """Remove oldest low-access notes in ``session_id`` when count >= max_notes.

        Single atomic DELETE-with-subquery avoids a TOCTOU race between
        SELECT count and DELETE on shared-DB topologies (F-3b-1).
        RFC 0031 Phase 2 PR 2: scoped to ``(agent_id, session_id)`` so
        write-side isolation extends to the lifecycle path — per-session
        capacity instead of per-agent (PR 1 review F1 carry-forward).
        """
        await self._db.execute(
            """
            DELETE FROM notes WHERE agent_id = ? AND session_id = ? AND id IN (
                SELECT id FROM notes
                WHERE agent_id = ? AND session_id = ?
                ORDER BY access_count ASC, created_at ASC
                LIMIT MAX(
                    0,
                    (SELECT COUNT(*) FROM notes
                     WHERE agent_id = ? AND session_id = ?) - ? + 1
                )
            )
            """,
            (
                self._agent_id, session_id,
                self._agent_id, session_id,
                self._agent_id, session_id,
                max_notes,
            ),
        )

    async def _recall_notes_fts5(
        self,
        query: str,
        limit: int,
        min_score: float | None,
        sessions: list[str] | None,
    ) -> list[aiosqlite.Row]:
        """FTS5 search across topic, content, and tags.

        ``sessions`` (RFC 0031 Phase 2 PR 2) is the resolved session
        list from :func:`agents.memory._session_filter._resolve_session_list`;
        ``None`` is the ``"*"`` no-filter mode.
        """
        sess_clause, sess_params = session_in_clause(
            sessions, column="n.session_id",
        )
        safe_query = _FTS5_SPECIAL.sub(" ", query).strip()
        if not safe_query:
            return await self._recall_notes_like(
                query, limit, min_score, sessions,
            )
        effective_min_score = resolve_min_score(min_score)
        try:
            async with self._db.execute(
                f"""
                SELECT {", ".join(f"n.{c}" for c in _NOTE_COLS)}
                FROM notes_fts fts
                JOIN notes n ON n.rowid = fts.rowid
                WHERE notes_fts MATCH ?
                  AND n.agent_id = ?
                  AND (1.0 / (1.0 + ABS(fts.rank))) >= ?
                  {sess_clause}
                ORDER BY fts.rank * -1 DESC
                LIMIT ?
                """,
                (
                    safe_query, self._agent_id, effective_min_score,
                    *sess_params, limit,
                ),
            ) as cursor:
                return list(await cursor.fetchall())
        except sqlite3.OperationalError as exc:
            logger.warning(
                "Notes FTS5 query failed for %r (sanitized: %r), falling back to LIKE: %s",
                query,
                safe_query,
                exc,
            )
            return await self._recall_notes_like(
                query, limit, min_score, sessions,
            )

    async def _recall_notes_like(
        self,
        query: str,
        limit: int,
        min_score: float | None,  # noqa: ARG002 — LIKE matches score 1.0
        sessions: list[str] | None,
    ) -> list[aiosqlite.Row]:
        """LIKE fallback when FTS5 is unavailable.

        ``min_score`` is accepted for signature compatibility but is not
        applied: LIKE matching is binary, so every match scores ``1.0``
        per RFC 0017 Section C.  ``sessions`` — see :meth:`_recall_notes_fts5`.
        """
        sess_clause, sess_params = session_in_clause(
            sessions, column="session_id",
        )
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        async with self._db.execute(
            f"""
            SELECT {_NOTE_SELECT}
            FROM notes
            WHERE agent_id = ?
              AND (topic LIKE ? ESCAPE '\\'
                   OR content LIKE ? ESCAPE '\\'
                   OR tags_json LIKE ? ESCAPE '\\')
              {sess_clause}
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (
                self._agent_id, pattern, pattern, pattern,
                *sess_params, limit,
            ),
        ) as cursor:
            return list(await cursor.fetchall())

    async def _recall_notes_recency(
        self,
        limit: int,
        sessions: list[str] | None,
    ) -> list[aiosqlite.Row]:
        """No query — return most recently updated notes.

        ``sessions`` — see :meth:`_recall_notes_fts5`.
        """
        sess_clause, sess_params = session_in_clause(
            sessions, column="session_id",
        )
        async with self._db.execute(
            f"""
            SELECT {_NOTE_SELECT}
            FROM notes
            WHERE agent_id = ?
              {sess_clause}
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (self._agent_id, *sess_params, limit),
        ) as cursor:
            return list(await cursor.fetchall())

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
        )
