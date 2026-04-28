"""Integration tests for the task-agent MemoryFacade wiring (RFC 0008 PR 2).

Covers the end-to-end opt-in flow described in
``docs/rfcs/0008-pr-plan.md`` PR 2:

* When ``memory.enabled: true`` is set in the agent's config, the
  agent-server lifecycle opens a :class:`MemoryFacade` on startup,
  ``BaseAgent._inject_memories`` injects retrieved entries into the
  system prompt, and the budget knob carried via the
  ``_context_package`` clamps the recall ``limit``.
* When ``memory.enabled`` is unset / ``false`` (deny-by-default),
  ``agent.memory`` is ``None`` and ``_inject_memories`` is a no-op so
  task agents that don't opt in pay zero memory cost.

The persona-runtime memory path is exercised separately by
``tests/integration/test_persona_e2e_scheduling_memory.py`` and the
RFC 0017 budget tests; this file is scoped to the task-agent surface
only.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from agents.base import CONTEXT_PACKAGE_KEY, BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.memory import MemoryFacade


class _TaskAgentStub(BaseAgent):
    """Minimal concrete BaseAgent for exercising memory wiring."""

    async def handle(self, task: TaskInput) -> TaskOutput:  # pragma: no cover - unused
        return TaskOutput(status=TaskStatus.COMPLETED, result="ok")


@pytest.fixture
async def enabled_agent(tmp_path: Any) -> AsyncGenerator[_TaskAgentStub, None]:
    """Task agent with memory enabled and an isolated SQLite DB."""

    agent = _TaskAgentStub(
        agent_id="memo-bot",
        config={
            "model": "test",
            "memory": {
                "enabled": True,
                "db_path": str(tmp_path / "memory.db"),
            },
        },
    )
    await agent.initialize_memory()
    try:
        yield agent
    finally:
        await agent.close_memory()


@pytest.fixture
async def disabled_agent() -> AsyncGenerator[_TaskAgentStub, None]:
    """Task agent with memory disabled (deny-by-default path)."""

    agent = _TaskAgentStub(agent_id="no-mem", config={"model": "test"})
    await agent.initialize_memory()
    try:
        yield agent
    finally:
        await agent.close_memory()


class TestMemoryEnabledTaskAgent:
    async def test_facade_opened_on_initialize(
        self, enabled_agent: _TaskAgentStub,
    ) -> None:
        assert isinstance(enabled_agent.memory, MemoryFacade)

    async def test_store_then_retrieve_round_trip(
        self, enabled_agent: _TaskAgentStub,
    ) -> None:
        facade = enabled_agent.memory
        assert facade is not None
        await facade.store_observation(
            "the user prefers Python type hints",
            scope="prefs",
            importance=0.8,
        )
        results = await facade.retrieve_relevant("python type hints", limit=5)
        assert len(results) == 1
        assert "type hints" in results[0].content

    async def test_inject_memories_appends_preamble(
        self, enabled_agent: _TaskAgentStub,
    ) -> None:
        facade = enabled_agent.memory
        assert facade is not None
        await facade.store_observation(
            "the user prefers Python type hints",
            scope="prefs",
            importance=0.8,
        )
        task = TaskInput(
            task_id="t1",
            workflow_id="wf-test",
            payload="python type hints",
            context={},
        )
        prompt = await enabled_agent._inject_memories("SYSTEM", task)
        assert "Relevant memories" in prompt
        assert "type hints" in prompt

    async def test_inject_memories_respects_budget(
        self, enabled_agent: _TaskAgentStub,
    ) -> None:
        """A small ``budget_memory_tokens`` clamps the recall limit."""

        facade = enabled_agent.memory
        assert facade is not None
        for i in range(3):
            await facade.store_observation(
                f"widgets observation number {i}",
                scope="ops",
                importance=0.5,
            )
        package = json.dumps({"budget_memory_tokens": 200})
        task = TaskInput(
            task_id="t2",
            workflow_id="wf-test",
            payload="widgets observation",
            context={CONTEXT_PACKAGE_KEY: package},
        )
        prompt = await enabled_agent._inject_memories("SYSTEM", task)
        # Budget 200 / DEFAULT_AVG_ENTRY_TOKENS (100) → limit 2.
        # Three observations stored → preamble must show ≤ 2 bullet lines.
        bullet_lines = [
            line for line in prompt.splitlines() if line.startswith("- ")
        ]
        assert len(bullet_lines) <= 2


class TestMemoryDisabledTaskAgent:
    async def test_memory_is_none_by_default(
        self, disabled_agent: _TaskAgentStub,
    ) -> None:
        assert disabled_agent.memory is None

    async def test_inject_memories_is_noop(
        self, disabled_agent: _TaskAgentStub,
    ) -> None:
        task = TaskInput(task_id="t3", workflow_id="wf-test", payload="anything", context={})
        prompt = await disabled_agent._inject_memories("SYSTEM", task)
        assert prompt == "SYSTEM"

    async def test_close_is_safe_when_never_opened(
        self, disabled_agent: _TaskAgentStub,
    ) -> None:
        # Already closed once via fixture teardown is implicit; explicit
        # second call must not raise either.
        await disabled_agent.close_memory()
        await disabled_agent.close_memory()
