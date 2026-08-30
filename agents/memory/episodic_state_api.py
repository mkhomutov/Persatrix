"""Agent-state API mixin for :class:`agents.memory.episodic.EpisodicMemory`.

Extracted from ``episodic.py`` (PR #849 review) when that file sat at
exactly the 500-line repo cap (``scripts/checks/file_size.py --strict``)
and the ISSUE-0131 ``speaker_id`` docstring paragraph could only be
added by re-wrapping prose — the same at-the-cap trap that produced
``interaction_key.py``, ``_facts_write.py`` and ``close_entries.py`` on
this branch.  Same idiom as :mod:`agents.memory.episodic_notes_api`:
pure delegation, moved as a MIXIN (not free functions) so the methods
keep their public call sites on ``EpisodicMemory`` and share its
lifecycle gate via ``_ensure_db``.

The two sections here are one concern — the ``agent_state`` table: the
interaction counter and the opaque persona-state JSON live on the same
row, and the upsert contract (``persist_agent_state`` must not reset
``interaction_count``) is only visible when the two APIs sit together.
The SQL bodies stay in :mod:`agents.memory.episodic_queries`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .episodic_queries import (
    get_interaction_count,
    increment_interaction_count,
    load_agent_state,
    persist_agent_state,
    reset_interaction_count,
)

if TYPE_CHECKING:
    import aiosqlite


class _EpisodicStateAPIMixin:
    """Delegates ``agent_state``-table access to :mod:`.episodic_queries`.

    Expects ``_ensure_db(self) -> aiosqlite.Connection`` and
    ``_agent_id`` from the concrete class (``EpisodicMemory``); private
    (leading underscore) because it is not a public extension point — it
    exists solely as a file-size split.
    """

    if TYPE_CHECKING:
        _agent_id: str

        def _ensure_db(self) -> aiosqlite.Connection: ...

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
