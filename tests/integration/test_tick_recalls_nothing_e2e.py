"""A tick admits no memory, even beside rows that share its words (ISSUE-0159).

RFC 0017 §F skips the model call on a tick whose memory injection admitted
nothing. Recall now matches any word of a message, and a tick's sentence
("review your goals and decide on next actions") shares words with ordinary
episodes and notes, so in a public room a tick could admit them and pay for
a call it used to skip. The FTS5 query builder searches nothing for the tick
sentence; this drives a TICK through the persona's real injection path
against public rows that share its words and checks nothing is admitted and
nothing is counted as used.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.clock import WallClock
from agents.memory._fts5_query import TICK_TEXT
from agents.memory.episodic import EpisodicMemory
from agents.memory.working import WorkingMemory
from agents.persona_runtime.memory_context import _MemoryContextMixin
from agents.persona_types import EventType


class _Persona(_MemoryContextMixin):
    """The memory mixin with the attributes ``_LLMPersonaAgent`` would set,
    the wiring ``test_memory_budget_e2e.py`` uses."""

    def __init__(self, episodic: EpisodicMemory) -> None:
        super().__init__()
        self._clock = WallClock()
        self._timezone = "UTC"
        self.agent_id = "test-agent-tick-e2e"
        self._working_memory = WorkingMemory(max_tokens=8192)
        self._episodic_memory = episodic
        self._relationship_memory = AsyncMock()
        self._relationship_memory.get_relationship_summary.return_value = None

    def _format_event(self, event: Any) -> str:  # type: ignore[override]
        return TICK_TEXT if event.event_type is EventType.TICK else ""


def _tick() -> Any:
    """A TICK carries no channel stamp, so it acts at the public floor."""
    event = MagicMock()
    event.event_type = EventType.TICK
    event.sender_id = None
    event.metadata = {}
    event.payload = {}
    return event


@pytest.fixture
async def public_rows_sharing_tick_words() -> AsyncGenerator[EpisodicMemory, None]:
    mem = EpisodicMemory("test-agent-tick-e2e", db_path=":memory:")
    await mem.initialize()
    await mem.store_episode(
        "Event: tick -> Actions: ['do_nothing']",
        context={"actions": ["do_nothing"]},
        protection_level="public",
    )
    await mem.store_episode(
        "Weekly review of the goals; we decide next actions on Friday.",
        context={"event": "chat"},
        protection_level="public",
    )
    await mem.store_note(
        "goals", "Review the quarterly goals and decide next actions.",
        protection_level="public",
    )
    yield mem
    await mem.close()


async def _total_access_count(mem: EpisodicMemory) -> int:
    assert mem._db is not None
    async with mem._db.execute("SELECT SUM(access_count) FROM episodes") as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0] or 0)


@pytest.mark.asyncio
async def test_a_tick_admits_nothing_and_counts_no_use(
    public_rows_sharing_tick_words: EpisodicMemory,
) -> None:
    mem = public_rows_sharing_tick_words
    # Positive control: the same rows are recalled for an ordinary message.
    assert await mem.recall("What are our goals this week?")
    before = await _total_access_count(mem)

    result = await _Persona(mem)._inject_memory_context(_tick())

    assert result.memory_admitted_tokens == 0
    assert await _total_access_count(mem) == before
