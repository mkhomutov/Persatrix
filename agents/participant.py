"""
Participant abstraction — symmetrical identity for agents and human users.

The ``Participant`` Protocol defines the minimal interface shared by all
actors in the agent society. ``BaseAgent`` satisfies it via properties
added in this PR. ``UserParticipant`` is the concrete human-user type.
``UserStore`` provides SQLite-persisted CRUD for user identities.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import aiosqlite

# Valid participant types — validated at the UserStore write boundary.
VALID_PARTICIPANT_TYPES: frozenset[str] = frozenset({"agent", "user"})

# Agent/participant ID pattern from project conventions.
_PARTICIPANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")


@runtime_checkable
class Participant(Protocol):
    """Minimal identity contract for all society actors."""

    @property
    def participant_id(self) -> str: ...

    @property
    def participant_type(self) -> str: ...

    @property
    def display_name(self) -> str: ...


def validate_participant_id(participant_id: str) -> None:
    """Raise ``ValueError`` if *participant_id* is not a valid identifier."""
    if not _PARTICIPANT_ID_RE.match(participant_id):
        raise ValueError(
            f"Invalid participant_id {participant_id!r}: "
            f"must match {_PARTICIPANT_ID_RE.pattern}"
        )


def validate_participant_type(participant_type: str) -> None:
    """Raise ``ValueError`` if *participant_type* is not in ``VALID_PARTICIPANT_TYPES``."""
    if participant_type not in VALID_PARTICIPANT_TYPES:
        raise ValueError(
            f"Invalid participant_type {participant_type!r}: "
            f"must be one of {sorted(VALID_PARTICIPANT_TYPES)}"
        )


@dataclass
class UserParticipant:
    """A human user participating in the agent society."""

    participant_id: str
    display_name: str
    participant_type: str = "user"
    created_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)


class UserStore:
    """SQLite-persisted CRUD for ``UserParticipant`` records."""

    def __init__(self, db_path: str = "data/memory.db") -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Open the database and ensure the ``users`` table exists."""
        if self._db is not None:
            await self._db.close()

        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                participant_id   TEXT PRIMARY KEY,
                display_name     TEXT NOT NULL,
                participant_type TEXT NOT NULL DEFAULT 'user',
                created_at       REAL NOT NULL,
                last_seen_at     REAL NOT NULL
            )
            """
        )
        await self._db.commit()

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("UserStore not initialized — call initialize() first")
        return self._db

    async def get_or_create(
        self,
        participant_id: str,
        display_name: str | None = None,
        participant_type: str = "user",
    ) -> UserParticipant:
        """Return existing user or create a new one.

        Validates *participant_id* and *participant_type* at the write boundary.
        """
        validate_participant_id(participant_id)
        validate_participant_type(participant_type)

        db = self._ensure_db()
        row = await db.execute_fetchall(
            "SELECT participant_id, display_name, participant_type, "
            "created_at, last_seen_at FROM users WHERE participant_id = ?",
            (participant_id,),
        )
        if row:
            return UserParticipant(
                participant_id=row[0][0],
                display_name=row[0][1],
                participant_type=row[0][2],
                created_at=row[0][3],
                last_seen_at=row[0][4],
            )

        now = time.time()
        name = display_name or participant_id
        await db.execute(
            "INSERT INTO users (participant_id, display_name, participant_type, "
            "created_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
            (participant_id, name, participant_type, now, now),
        )
        await db.commit()
        return UserParticipant(
            participant_id=participant_id,
            display_name=name,
            participant_type=participant_type,
            created_at=now,
            last_seen_at=now,
        )

    async def update_last_seen(self, participant_id: str) -> None:
        """Update ``last_seen_at`` without modifying other fields."""
        db = self._ensure_db()
        await db.execute(
            "UPDATE users SET last_seen_at = ? WHERE participant_id = ?",
            (time.time(), participant_id),
        )
        await db.commit()

    async def get(self, participant_id: str) -> UserParticipant | None:
        """Return the user or ``None`` if not found."""
        db = self._ensure_db()
        row = await db.execute_fetchall(
            "SELECT participant_id, display_name, participant_type, "
            "created_at, last_seen_at FROM users WHERE participant_id = ?",
            (participant_id,),
        )
        if not row:
            return None
        return UserParticipant(
            participant_id=row[0][0],
            display_name=row[0][1],
            participant_type=row[0][2],
            created_at=row[0][3],
            last_seen_at=row[0][4],
        )
