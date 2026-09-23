"""The notes tier's mutation surface: edit, delete and count.

Moved out of :mod:`agents.memory.notes` when the ISSUE-0164 fix took that
module past 500 lines.  The three methods are one concern: each reaches
the same notes a recall does on the session, tenant and epoch axes — this
agent, the active session read at call time plus the ``legacy``
carve-out, and strict tenant and epoch equality — and that rule is
easiest to keep when they sit together.  Unlike the persona's recall they
do not filter by RFC 0037 protection level: an edit re-stamps upward
instead (§C), and a delete or a count ignores the level.  Same idiom as
:mod:`agents.memory.episodic_notes_api`: a mixin, not free functions, so
the methods keep their public call sites on
:class:`~agents.memory.notes.NoteStore`.  The read queries live in
:mod:`agents.memory._notes_recall`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ..clock import agent_now
from ._epoch_filter import epoch_eq_clause, resolve_active_epoch
from ._principal_filter import principal_eq_clause, resolve_active_principal
from ._session_filter import _resolve_session_list, session_in_clause
from .note_types import _check_note_content

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite


class _NoteStoreState(Protocol):
    """What the mixin reads from :class:`~agents.memory.notes.NoteStore`.

    Typing ``self`` with this protocol, rather than declaring the
    attributes on the mixin, keeps mypy checking that ``NoteStore`` really
    sets each one.
    """

    _db: aiosqlite.Connection
    _agent_id: str
    _active_session_id: str
    _active_principal_id: str
    _active_epoch_id: str


class _NoteMutationsMixin:
    """``update_note`` / ``delete_note`` / ``count_notes`` for
    :class:`~agents.memory.notes.NoteStore`.

    Reads the connection, the agent id and the three scope snapshots
    through :class:`_NoteStoreState`; private (leading underscore)
    because it is not a public extension point.
    """

    async def update_note(
        self: _NoteStoreState,
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
        construction snapshot — so a turn reaches the session, tenant and
        epoch its recall does (:func:`_mutation_scope_clause`).  It does
        not share recall's protection-level filter: a turn can edit a note
        above its acting level that its recall withholds, and the
        re-stamp below keeps that note's level.

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
        _check_note_content(content)
        now = agent_now()
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
        scope_sql, scope_params = _mutation_scope_clause(self)
        cursor = await self._db.execute(
            f"UPDATE notes SET content = ?, updated_at = ?{restamp_sql} "
            f"WHERE id = ? AND agent_id = ?{scope_sql}",
            (
                content, now, *restamp_params, note_id, self._agent_id,
                *scope_params,
            ),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def delete_note(self: _NoteStoreState, note_id: str) -> bool:
        """Delete a note by ID. Returns True if found.  Session-,
        principal- and epoch-scoped per :meth:`update_note`, and like it
        blind to protection level: RFC 0037 §C covers edits and says
        nothing about deletes, so a turn can delete a note above its
        acting level that its recall withholds."""
        scope_sql, scope_params = _mutation_scope_clause(self)
        cursor = await self._db.execute(
            f"DELETE FROM notes WHERE id = ? AND agent_id = ?{scope_sql}",
            (note_id, self._agent_id, *scope_params),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def count_notes(self: _NoteStoreState) -> int:
        """Number of notes in :meth:`update_note`'s scope — the active
        session plus ``legacy``, for this tenant and epoch — at every
        protection level."""
        scope_sql, scope_params = _mutation_scope_clause(self)
        async with self._db.execute(
            f"SELECT COUNT(*) FROM notes WHERE agent_id = ?{scope_sql}",
            (self._agent_id, *scope_params),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0


def _mutation_scope_clause(store: _NoteStoreState) -> tuple[str, list[str]]:
    """The ``" AND session_id IN (…) AND principal_id = ? AND epoch_id =
    ?"`` scope of the three methods above, built by the helpers
    :meth:`~agents.memory.notes.NoteStore.recall_notes` uses, so these
    three axes cannot drift from recall's.

    The session is read at call time (a bound ``session_scope``, else the
    store's snapshot) plus the ``legacy`` carve-out (ISSUE-0164).  Tenant
    and epoch are strict equality (ISSUE-0081 PR 3, ISSUE-0085 PR 3): a
    foreign principal, or a fresh epoch, cannot mutate a row even if it
    knows the UUID and shares the session or ``legacy`` scope.
    """
    session_sql, session_params = session_in_clause(
        _resolve_session_list(None, store._active_session_id),
        column="session_id",
    )
    principal_sql, principal_params = principal_eq_clause(
        resolve_active_principal(store._active_principal_id),
        column="principal_id",
    )
    epoch_sql, epoch_params = epoch_eq_clause(
        resolve_active_epoch(store._active_epoch_id), column="epoch_id",
    )
    return (
        session_sql + principal_sql + epoch_sql,
        [*session_params, *principal_params, *epoch_params],
    )
