"""Notes-API mixin for :class:`agents.memory.episodic.EpisodicMemory`.

Extracted from ``episodic.py`` to keep that file under the 500-line repo
cap.  Pure delegation to :class:`agents.memory.notes.NoteStore`; lives as
a mixin (not a free-function module) so the methods retain the same
public call sites on ``EpisodicMemory`` and share its lifecycle gate via
``_ensure_note_store``.

The mixin owns no state — all storage lives in the ``NoteStore`` opened
by :meth:`EpisodicMemory.initialize`.  ``recall_notes`` keeps its
``min_score`` validation at this layer (mirroring ``EpisodicMemory.recall``)
so a future ``NoteStore`` refactor that drops its own guard cannot
silently lose validation.  (PR 6 — RFC 0017 PR 3 review finding 1.)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..session_id import LEGACY_SESSION_ID
from ._migration_protection import PROTECTION_LEVEL_DEFAULT

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .notes import Note, NoteStore


class _EpisodicNotesAPIMixin:
    """Delegates note CRUD to the underlying :class:`NoteStore`.

    Expects ``_ensure_note_store(self) -> NoteStore`` to be provided by
    the concrete class (``EpisodicMemory``); the mixin is private (leading
    underscore) because it is not a public extension point — it exists
    solely as a file-size split.
    """

    if TYPE_CHECKING:
        def _ensure_note_store(self) -> NoteStore: ...

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

        ``session_id`` (RFC 0031 Phase 2 PR 1) is forwarded to the
        underlying :class:`NoteStore`; default
        :data:`agents.session_id.LEGACY_SESSION_ID` matches the
        operator-namespace carve-out used by the other persona-memory
        tiers' write paths.  Empty / whitespace-only values are
        normalised by :meth:`NoteStore.store_note` at the storage
        boundary — this layer is a pass-through.

        ``protection_level`` (RFC 0037 §C — PR 4) is forwarded verbatim;
        see :meth:`NoteStore.store_note` for the stamp-site contract.
        """
        return await self._ensure_note_store().store_note(
            topic, content, tags=tags, max_notes=max_notes,
            session_id=session_id, protection_level=protection_level,
        )

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
            normalised scores.  ``None`` → no filtering (current behaviour).
            LIKE-fallback path ignores this parameter per RFC 0017 Section C.
        sessions:
            RFC 0031 §D recall filter (Phase 2 PR 2) — forwarded
            verbatim to :meth:`NoteStore.recall_notes`.  See that
            method's docstring for the four-mode contract.
        allowed_protection_levels:
            RFC 0037 §D read-surface gating (PR 4) — forwarded verbatim;
            see :meth:`NoteStore.recall_notes` for the contract.  The
            persona read surfaces always pass it; ``None`` (default) is
            the ungated non-persona operator/CLI surface.
        """
        if min_score is not None and not 0.0 <= min_score <= 1.0:
            raise ValueError(
                f"min_score must be in [0.0, 1.0] or None, got {min_score}"
            )
        return await self._ensure_note_store().recall_notes(
            query, limit=limit, min_score=min_score, sessions=sessions,
            allowed_protection_levels=allowed_protection_levels,
        )

    async def update_note(
        self,
        note_id: str,
        content: str,
        *,
        restamp_protection_level: str | None = None,
        restamp_below: Sequence[str] = (),
    ) -> bool:
        """Update note content. Topic and tags preserved. Returns True if found.

        The RFC 0037 §C re-stamp kwargs (PR 4) are forwarded verbatim —
        see :meth:`NoteStore.update_note` for the raise-only contract.
        """
        return await self._ensure_note_store().update_note(
            note_id, content,
            restamp_protection_level=restamp_protection_level,
            restamp_below=restamp_below,
        )

    async def delete_note(self, note_id: str) -> bool:
        """Delete a note by ID (agent-scoped). Returns True if found."""
        return await self._ensure_note_store().delete_note(note_id)

    async def count_notes(self) -> int:
        """Return the number of notes for this agent."""
        return await self._ensure_note_store().count_notes()
