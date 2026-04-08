"""
Tests for BaseAgent interface.

PR review: zero executable tests existed in the scaffold, meaning even import
errors would go undetected. These minimal tests prove the core interfaces are
importable, instantiable, and behave as documented.
"""

import pytest

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus


# ─── Concrete subclass for testing the ABC ──────────────────

class StubAgent(BaseAgent):
    """Minimal concrete agent to verify BaseAgent can be subclassed."""

    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(
            status=TaskStatus.COMPLETED,
            result=f"handled: {task.payload}",
        )

    @property
    def capabilities(self) -> list[str]:
        return ["testing"]


# ─── Tests ──────────────────────────────────────────────────

class TestTaskStatus:
    def test_enum_values(self):
        assert TaskStatus.COMPLETED.value == "completed"
        assert TaskStatus.FAILED.value == "failed"

    def test_enum_members(self):
        assert len(TaskStatus) == 2


class TestTaskInput:
    def test_required_fields(self):
        ti = TaskInput(task_id="t1", workflow_id="w1", payload="do stuff")
        assert ti.task_id == "t1"
        assert ti.workflow_id == "w1"
        assert ti.payload == "do stuff"

    def test_default_context(self):
        ti = TaskInput(task_id="t1", workflow_id="w1", payload="x")
        assert ti.context == {}


class TestTaskOutput:
    def test_construction(self):
        to = TaskOutput(status=TaskStatus.COMPLETED, result="ok")
        assert to.status == TaskStatus.COMPLETED
        assert to.result == "ok"
        assert to.metadata == {}


class TestBaseAgent:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            BaseAgent("test")  # type: ignore[abstract]

    def test_subclass_instantiation(self):
        agent = StubAgent("agent-1")
        assert agent.agent_id == "agent-1"
        assert agent.capabilities == ["testing"]

    def test_default_name_is_agent_id(self):
        agent = StubAgent("agent-1")
        assert agent.name == "agent-1"

    def test_name_from_config(self):
        agent = StubAgent("agent-1", config={"name": "Test Agent"})
        assert agent.name == "Test Agent"

    async def test_handle(self):
        agent = StubAgent("agent-1")
        task = TaskInput(task_id="t1", workflow_id="w1", payload="hello")
        result = await agent.handle(task)
        assert result.status == TaskStatus.COMPLETED
        assert result.result == "handled: hello"

    async def test_health_check_default(self):
        agent = StubAgent("agent-1")
        assert await agent.health_check() is True

    async def test_shutdown_does_not_raise(self):
        agent = StubAgent("agent-1")
        await agent.shutdown()  # should not raise
