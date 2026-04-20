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
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field

import aiosqlite

logger = logging.getLogger(__name__)


# ─── Data model ─────────────────────────────────────────────


@dataclass
class Note:
    """An agent-initiated note persisted via memory tools."""

    id: str
    agent_id: str
    topic: str
    content: str
    tags: list[str] = field(default_factory=list)
    access_count: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0


# FTS5 MATCH operator characters that cause parse errors when present in
# freeform (LLM-generated) queries. Colons are the most critical: FTS5
# interprets "word:phrase" as a column filter, failing with "no such column"
# when the word isn't a declared FTS5 column (e.g. "tick: scheduler").
_FTS5_SPECIAL = re.compile(r'[":*^()]+')

# Maximum content size for a single note (10 KB).
_MAX_NOTE_CONTENT_BYTES = 10_240

# Maximum number of notes returned by recall_notes() to prevent unbounded
# result sets and resource exhaustion.
_MAX_RECALL_LIMIT = 100

# Column list for SELECT queries on the notes table.
_NOTE_COLS = (
    "id", "agent_id", "topic", "content", "tags_json",
    "access_count", "created_at", "updated_at",
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
    ) -> None:
        self._agent_id = agent_id
        self._db = db
        self._fts5 = fts5

    # ─── CRUD ───────────────────────────────────────────────

    async def store_note(
        self,
        topic: str,
        content: str,
        tags: list[str] | None = None,
        max_notes: int = 500,
    ) -> str:
        """Store a new note. Prunes oldest low-access notes if over cap.

        Returns the generated note ID.
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

        # Prune oldest low-access notes if at capacity.
        await self._prune_notes(max_notes)

        note_id = str(uuid.uuid4())
        now = time.time()
        await self._db.execute(
            """
            INSERT INTO notes
                (id, agent_id, topic, content, tags_json,
                 access_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                note_id,
                self._agent_id,
                topic.strip(),
                content,
                json.dumps(tags or []),
                now,
                now,
            ),
        )
        await self._db.commit()
        return note_id

    async def recall_notes(
        self,
        query: str = "",
        *,
        limit: int = 10,
    ) -> list[Note]:
        """Retrieve notes matching query, ranked by relevance.

        Increments access_count on returned notes.
        """
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        limit = min(limit, _MAX_RECALL_LIMIT)

        if query and self._fts5:
            rows = await self._recall_notes_fts5(query, limit)
        elif query:
            rows = await self._recall_notes_like(query, limit)
        else:
            rows = await self._recall_notes_recency(limit)

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

    async def _prune_notes(self, max_notes: int) -> None:
        """Remove oldest low-access notes when count >= max_notes.

        Uses a single atomic DELETE with a subquery to avoid a TOCTOU race
        between SELECT count and DELETE that could occur when multiple
        EpisodicMemory instances share a DB file (F-3b-1).
        """
        await self._db.execute(
            """
            DELETE FROM notes WHERE agent_id = ? AND id IN (
                SELECT id FROM notes
                WHERE agent_id = ?
                ORDER BY access_count ASC, created_at ASC
                LIMIT MAX(0, (SELECT COUNT(*) FROM notes WHERE agent_id = ?) - ? + 1)
            )
            """,
            (self._agent_id, self._agent_id, self._agent_id, max_notes),
        )

    async def _recall_notes_fts5(
        self,
        query: str,
        limit: int,
    ) -> list[aiosqlite.Row]:
        """FTS5 search across topic, content, and tags."""
        safe_query = _FTS5_SPECIAL.sub(" ", query).strip()
        if not safe_query:
            return await self._recall_notes_like(query, limit)
        try:
            async with self._db.execute(
                f"""
                SELECT {", ".join(f"n.{c}" for c in _NOTE_COLS)}
                FROM notes_fts fts
                JOIN notes n ON n.rowid = fts.rowid
                WHERE notes_fts MATCH ?
                  AND n.agent_id = ?
                ORDER BY fts.rank * -1 DESC
                LIMIT ?
                """,
                (safe_query, self._agent_id, limit),
            ) as cursor:
                return list(await cursor.fetchall())
        except sqlite3.OperationalError as exc:
            logger.warning(
                "Notes FTS5 query failed for %r (sanitized: %r), falling back to LIKE: %s",
                query,
                safe_query,
                exc,
            )
            return await self._recall_notes_like(query, limit)

    async def _recall_notes_like(
        self,
        query: str,
        limit: int,
    ) -> list[aiosqlite.Row]:
        """LIKE fallback when FTS5 is unavailable."""
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
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (self._agent_id, pattern, pattern, pattern, limit),
        ) as cursor:
            return list(await cursor.fetchall())

    async def _recall_notes_recency(
        self,
        limit: int,
    ) -> list[aiosqlite.Row]:
        """No query — return most recently updated notes."""
        async with self._db.execute(
            f"""
            SELECT {_NOTE_SELECT}
            FROM notes
            WHERE agent_id = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (self._agent_id, limit),
        ) as cursor:
            return list(await cursor.fetchall())

    def _row_to_note(self, row: aiosqlite.Row) -> Note:
        """Convert a database row to a Note dataclass."""
        return Note(
            id=row[0],
            agent_id=row[1],
            topic=row[2],
            content=row[3],
            tags=json.loads(row[4]) if row[4] else [],
            access_count=row[5],
            created_at=row[6],
            updated_at=row[7],
        )
