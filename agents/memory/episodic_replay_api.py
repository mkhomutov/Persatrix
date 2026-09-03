"""The ISSUE-0130 (b) replay surface on :class:`EpisodicMemory`.

Split out of :mod:`agents.memory.episodic` (v0.3.15 PR B2 review) when
that module crossed the 500-line cap ``scripts/checks/file_size.py
--strict`` enforces, and composed the same way its two siblings are
(:class:`~agents.memory.episodic_notes_api._EpisodicNotesAPIMixin`,
:class:`~agents.memory.episodic_state_api._EpisodicStateAPIMixin`).

The seam is a real one rather than a line count: every method here exists
for the catch-up replay's WRITE path and for nothing else.  Two of them
are the re-derivation guard's read and its repair, which
:mod:`agents.memory._episodic_replay_dedup` documents must never grow a
recall-side caller; the third exposes the epoch a write would be stamped
with, so the replay span digest and the row it guards cannot disagree.
Keeping them together is what makes "this is the write path only"
checkable at a glance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._episodic_replay_dedup import (
    clear_failed_episode_for_interaction,
    episode_exists_for_interaction,
)
from ._epoch_filter import resolve_active_epoch

if TYPE_CHECKING:
    import aiosqlite

__all__ = ["_EpisodicReplayAPIMixin"]


class _EpisodicReplayAPIMixin:
    """Replay-path helpers — see module docstring for the shared rule.

    Expects ``_ensure_db(self) -> aiosqlite.Connection``, ``_agent_id``
    and ``_active_epoch_id`` from the concrete class
    (``EpisodicMemory``); private (leading underscore) because it is not
    a public extension point — it exists as a file-size split over a real
    seam.
    """

    if TYPE_CHECKING:
        _agent_id: str
        _active_epoch_id: str

        def _ensure_db(self) -> aiosqlite.Connection: ...

    def active_epoch_id(self) -> str:
        """The epoch a write from this call site would be stamped with.

        The same resolution :meth:`~agents.memory.episodic.EpisodicMemory
        .store_episode` applies (a per-request ``epoch_scope`` wins over
        the construction-time snapshot), exposed so the ISSUE-0130 (b)
        replay span identity can put it in its digest and be sure it
        matches the row the derivation goes on to write.
        """
        return resolve_active_epoch(self._active_epoch_id)

    async def has_episode_for_interaction(self, interaction_id: str) -> bool:
        """ISSUE-0130 (b): was this interaction already DERIVED into a row?

        The close path's re-derivation guard for replayed spans — see
        :mod:`agents.memory._episodic_replay_dedup` for why the lookup
        is not principal-filtered, why the janitor's failure tombstone
        does not count as derived, and why it must stay off the read path.
        """
        return await episode_exists_for_interaction(
            self._ensure_db(), self._agent_id, interaction_id)

    async def clear_failed_episode(self, interaction_id: str) -> int:
        """ISSUE-0130 (b): drop tombstones before re-deriving this span.

        Paired with :meth:`has_episode_for_interaction` — see
        :func:`agents.memory._episodic_replay_dedup
        .clear_failed_episode_for_interaction` for why a retry has to
        remove the row it is retrying.
        """
        return await clear_failed_episode_for_interaction(
            self._ensure_db(), self._agent_id, interaction_id)
