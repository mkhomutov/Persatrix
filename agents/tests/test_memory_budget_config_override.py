"""v0.3.16 PR K1 — the configured memory budget reaches the allocate-loop.

``_inject_memory_context`` constructs the per-turn :class:`MemoryBudget`
from the persona's resolved ``_memory_budget_tokens``: ``memory_budget.tokens``
from ``optimization.yaml`` when the operator set it, the
``MEMORY_BUDGET_TOKENS`` constant otherwise (``memory_knobs``).  The mixin
harness here never runs ``_LLMPersonaAgent.__init__``, so the class-level
default (the constant) is what keeps every older harness at the shipped
total, and a harness that wants a tighter budget sets the attribute on
the instance.  The last class builds a real persona from a config file
and drives injection, so the wiring is pinned end to end: the key in the
file is the total the allocate-loop runs at.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agents.optimization import reset_cache
from agents.persona_runtime.memory_budget import MEMORY_BUDGET_TOKENS
from agents.persona_runtime.memory_context import _MemoryContextMixin
from agents.persona_types import AgentEvent, EventType
from agents.tests._memory_context_helpers import (
    FakeEpisode,
    FakeNote,
    FakeRelSummary,
    episodic_tier,
    make_mixin,
)

__all__ = ["episodic_tier"]  # autouse fixture — see test_inject_memory_context.py


def _rich_inputs() -> dict[str, Any]:
    big_summary = "important context word " * 150
    return {
        "episodes": [FakeEpisode(summary=big_summary, id=f"ep-{i}") for i in range(5)],
        "notes": [FakeNote(topic="fact", content="detailed knowledge snippet " * 150)],
        "rel": FakeRelSummary(other_participant_id="alice", notes="rich notes " * 20),
        "sender_id": "alice",
    }


class TestMixinDefault:
    def test_class_default_is_the_constant(self) -> None:
        """No ``__init__`` → the shipped total applies (legacy harnesses)."""
        assert _MemoryContextMixin._memory_budget_tokens == MEMORY_BUDGET_TOKENS


class TestResolvedBudgetBoundsTheAllocateLoop:
    @pytest.mark.asyncio
    async def test_tighter_total_bounds_admission(self) -> None:
        mixin, event = make_mixin(**_rich_inputs())
        mixin._memory_budget_tokens = 100
        result = await mixin._inject_memory_context(event)
        assert 0 < result.memory_admitted_tokens <= 100

    @pytest.mark.asyncio
    async def test_zero_drops_every_recalled_tier(self) -> None:
        mixin, event = make_mixin(**_rich_inputs())
        mixin._memory_budget_tokens = 0
        result = await mixin._inject_memory_context(event)
        assert result.memory_admitted_tokens == 0
        names = {s.name for s in mixin._working_memory._sections}
        assert not {"episodic_recall", "recent_notes", "relationship_context"} & names


@pytest.fixture()
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "optimization.yaml"
    monkeypatch.setenv("PERSATRIX_OPTIMIZATION_CONFIG", str(path))
    reset_cache()
    yield path
    reset_cache()


async def _admitted_on_a_room_turn(agent: Any) -> int:
    """Seed three room episodes, inject for a stamped room turn, return admitted.

    A CHANNEL_MESSAGE stamped ``internal`` (the dispatch path's stamp) is
    the event shape under which the RFC 0037 §D gate admits the
    internal-by-default rows; a TICK floors to ``public`` and admits none,
    so it cannot tell a zero budget from the gate.
    """
    try:
        for i in range(3):
            await agent._episodic_memory.store_episode(
                summary=f"Turn {i}: hello from the planning room " * 3,
                context={}, importance=0.9, scope="group:planning",
            )
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE, channel_id="group:planning",
            sender_id="alice", payload={"content": "hello", "channel_type": "group"},
            metadata={"channel_classification": "internal"},
        )
        result = await agent._inject_memory_context(event, query="hello")
    finally:
        await agent.close_memory()
    return int(result.memory_admitted_tokens)


class TestPersonaStartReadsTheKey:
    @pytest.mark.asyncio
    async def test_agent_carries_the_configured_budget(self, config_path: Path) -> None:
        """Resolved once at persona start: ``__init__`` stores the value."""
        from agents.tests._persona_tick_helpers import make_agent

        config_path.write_text("memory_budget:\n  tokens: 321\n", encoding="utf-8")
        agent = await make_agent()
        try:
            assert agent._memory_budget_tokens == 321
        finally:
            await agent.close_memory()

    @pytest.mark.asyncio
    async def test_agent_without_the_key_runs_the_constant(self, config_path: Path) -> None:
        from agents.tests._persona_tick_helpers import make_agent

        config_path.write_text("schema_version: '0.3'\n", encoding="utf-8")
        agent = await make_agent()
        assert agent._memory_budget_tokens == MEMORY_BUDGET_TOKENS
        assert await _admitted_on_a_room_turn(agent) > 0

    @pytest.mark.asyncio
    async def test_zero_in_the_file_reaches_the_allocate_loop(self, config_path: Path) -> None:
        """The whole path: file → knob → ``__init__`` → the per-turn budget."""
        from agents.tests._persona_tick_helpers import make_agent

        config_path.write_text("memory_budget:\n  tokens: 0\n", encoding="utf-8")
        agent = await make_agent()
        assert agent._memory_budget_tokens == 0
        assert await _admitted_on_a_room_turn(agent) == 0
