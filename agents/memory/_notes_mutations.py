"""The notes tier's mutation surface: edit, delete and count.

Moved out of :mod:`agents.memory.notes` when the ISSUE-0164 fix took that
module past 500 lines.  The three methods are one concern: each reaches
exactly the notes a default recall returns in the same turn — this agent,
the active session read at call time plus the ``legacy`` carve-out, and
strict tenant and epoch equality — and that rule is easiest to keep when
they sit together.  Same idiom as :mod:`agents.memory.episodic_notes_api`:
a mixin, not free functions, so the methods keep their public call sites
on :class:`~agents.memory.notes.NoteStore`.  The read queries live in
:mod:`agents.memory._notes_recall`.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ._epoch_filter import resolve_active_epoch
from ._principal_filter import resolve_active_principal
from ._session_filter import _resolve_session_list, session_in_clause
from .note_types import _MAX_NOTE_CONTENT_BYTES

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite


class _NoteMutationsMixin:
    """``update_note`` / ``delete_note`` / ``count_notes`` for
    :class:`~agents.memory.notes.NoteStore`.

    Expects the connection, the agent id and the three scope snapshots
    from the concrete class; private (leading underscore) because it is
    not a public extension point.
    """

    if TYPE_CHECKING:
        _db: aiosqlite.Connection
        _agent_id: str
        _active_session_id: str
        _active_principal_id: str
        _active_epoch_id: str

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
        `ISSUE-0164
        <../../docs/issues/ISSUE-0164-persona-cannot-edit-or-delete-its-channel-notes.md>`_:
        "active" is read at call time, the way
        :meth:`~agents.memory.notes.NoteStore.recall_notes` reads it — the
        channel session the runtime bound for this event, else the
        construction snapshot — so a turn can edit exactly the notes it
        can recall.

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
        sess_clause, sess_params = self._mutation_session_clause()
        cursor = await self._db.execute(
            f"UPDATE notes SET content = ?, updated_at = ?{restamp_sql} "
            f"WHERE id = ? AND agent_id = ?{sess_clause} "
            "AND principal_id = ? "
            "AND epoch_id = ?",
            (
                content, now, *restamp_params, note_id, self._agent_id,
                *sess_params,
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
        sess_clause, sess_params = self._mutation_session_clause()
        cursor = await self._db.execute(
            "DELETE FROM notes "
            f"WHERE id = ? AND agent_id = ?{sess_clause} "
            "AND principal_id = ? "
            "AND epoch_id = ?",
            (
                note_id, self._agent_id,
                *sess_params,
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
        sess_clause, sess_params = self._mutation_session_clause()
        async with self._db.execute(
            "SELECT COUNT(*) FROM notes "
            f"WHERE agent_id = ?{sess_clause} "
            "AND principal_id = ? "
            "AND epoch_id = ?",
            (
                self._agent_id,
                *sess_params,
                principal_id, epoch_id,
            ),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0

    def _mutation_session_clause(self) -> tuple[str, list[str]]:
        """The ``" AND session_id IN (…)"`` clause of the three methods
        above, built by the same helpers
        :meth:`~agents.memory.notes.NoteStore.recall_notes` uses: the
        active session read at call time (a bound ``session_scope``, else
        this store's snapshot) plus the ``legacy`` carve-out.  Sharing
        them keeps what a turn can change equal to what it can recall
        (ISSUE-0164).
        """
        return session_in_clause(
            _resolve_session_list(None, self._active_session_id),
            column="session_id",
        )
