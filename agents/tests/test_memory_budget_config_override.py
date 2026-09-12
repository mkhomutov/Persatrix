"""v0.3.16 PR K1 — the configured memory budget reaches the allocate-loop.

``_inject_memory_context`` constructs the per-turn :class:`MemoryBudget`
from the persona's resolved ``_memory_budget_tokens`` when the operator
set ``memory_budget.tokens`` in ``optimization.yaml``, and from the
``MEMORY_BUDGET_TOKENS`` constant otherwise.  The mixin harness here
never runs ``_LLMPersonaAgent.__init__``, so the class-level default
(``None`` → the constant) is what keeps every older harness green; the
last class builds a real persona to pin the ``__init__`` wiring.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agents.optimization import reset_cache
from agents.persona_runtime.memory_budget import MEMORY_BUDGET_TOKENS
from agents.persona_runtime.memory_context import _MemoryContextMixin
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
    def test_class_default_is_none(self) -> None:
        """No override → the module constant applies (legacy harnesses)."""
        assert _MemoryContextMixin._memory_budget_tokens is None


class TestConfiguredBudgetOverridesTheConstant:
    @pytest.mark.asyncio
    async def test_override_bounds_admission_below_the_constant(self) -> None:
        mixin, event = make_mixin(**_rich_inputs())
        mixin._memory_budget_tokens = 100
        result = await mixin._inject_memory_context(event)
        assert 0 < result.memory_admitted_tokens <= 100
        assert MEMORY_BUDGET_TOKENS == 1500  # the constant did not move

    @pytest.mark.asyncio
    async def test_override_zero_drops_every_tier(self) -> None:
        mixin, event = make_mixin(**_rich_inputs())
        mixin._memory_budget_tokens = 0
        result = await mixin._inject_memory_context(event)
        assert result.memory_admitted_tokens == 0
        names = {s.name for s in mixin._working_memory._sections}
        assert not {"episodic_recall", "recent_notes", "relationship_context"} & names

    @pytest.mark.asyncio
    async def test_none_falls_through_to_the_patched_constant(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The zero-budget harness still monkeypatches the constant."""
        from agents.persona_runtime import memory_context as mc

        monkeypatch.setattr(mc, "MEMORY_BUDGET_TOKENS", 0)
        mixin, event = make_mixin(**_rich_inputs())
        mixin._memory_budget_tokens = None
        result = await mixin._inject_memory_context(event)
        assert result.memory_admitted_tokens == 0

    @pytest.mark.asyncio
    async def test_override_wins_over_the_patched_constant(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from agents.persona_runtime import memory_context as mc

        monkeypatch.setattr(mc, "MEMORY_BUDGET_TOKENS", 0)
        mixin, event = make_mixin(**_rich_inputs())
        mixin._memory_budget_tokens = 100
        result = await mixin._inject_memory_context(event)
        assert 0 < result.memory_admitted_tokens <= 100


@pytest.fixture()
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "optimization.yaml"
    monkeypatch.setenv("PERSATRIX_OPTIMIZATION_CONFIG", str(path))
    reset_cache()
    yield path
    reset_cache()


class TestPersonaStartReadsTheKey:
    @pytest.mark.asyncio
    async def test_agent_carries_the_configured_budget(self, config_path: Path) -> None:
        """Read once at persona start: ``__init__`` stores the resolved value."""
        from agents.tests._persona_tick_helpers import make_agent

        config_path.write_text("memory_budget:\n  tokens: 321\n", encoding="utf-8")
        agent = await make_agent()
        assert agent._memory_budget_tokens == 321

    @pytest.mark.asyncio
    async def test_agent_without_the_key_carries_none(self, config_path: Path) -> None:
        from agents.tests._persona_tick_helpers import make_agent

        config_path.write_text("schema_version: '0.3'\n", encoding="utf-8")
        agent = await make_agent()
        assert agent._memory_budget_tokens is None
