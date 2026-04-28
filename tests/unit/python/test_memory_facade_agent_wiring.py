"""Unit tests for BaseAgent ↔ MemoryFacade wiring (RFC 0008 PR plan PR 2).

Verifies that:
- ``BaseAgent.initialize_memory`` creates a ``MemoryFacade`` only when
  ``memory.enabled=true`` (deny-by-default).
- ``BaseAgent.close_memory`` releases the facade idempotently.
- ``_inject_memories`` augments the system prompt when relevant entries exist
  and is a no-op when memory is disabled or the recall is empty.
- The reserved ``_context_package`` key's ``budget_memory_tokens`` field is
  honoured (translation is via :func:`agents.memory.budget_to_limit`).
"""

from __future__ import annotations

import json

import pytest

from agents.base import CONTEXT_PACKAGE_KEY, BaseAgent, TaskInput, TaskOutput, TaskStatus


class _Stub(BaseAgent):
    async def handle(self, task: TaskInput) -> TaskOutput:  # pragma: no cover - unused
        return TaskOutput(status=TaskStatus.COMPLETED, result="ok")


# ─── Lifecycle ────────────────────────────────────────────────


async def test_initialize_memory_no_op_when_disabled() -> None:
    agent = _Stub("disabled", config={})
    await agent.initialize_memory()
    assert agent.memory is None


async def test_initialize_memory_no_op_when_enabled_false() -> None:
    agent = _Stub("disabled", config={"memory": {"enabled": False}})
    await agent.initialize_memory()
    assert agent.memory is None


async def test_initialize_memory_opens_facade_when_enabled() -> None:
    agent = _Stub(
        "enabled",
        config={"memory": {"enabled": True, "db_path": ":memory:"}},
    )
    try:
        await agent.initialize_memory()
        assert agent.memory is not None
        assert agent.memory.agent_id == "enabled"
    finally:
        await agent.close_memory()


async def test_initialize_memory_is_idempotent() -> None:
    agent = _Stub(
        "enabled",
        config={"memory": {"enabled": True, "db_path": ":memory:"}},
    )
    try:
        await agent.initialize_memory()
        first = agent.memory
        await agent.initialize_memory()
        assert agent.memory is first
    finally:
        await agent.close_memory()


async def test_close_memory_is_idempotent() -> None:
    agent = _Stub(
        "enabled",
        config={"memory": {"enabled": True, "db_path": ":memory:"}},
    )
    await agent.initialize_memory()
    await agent.close_memory()
    # Second close is a no-op.
    await agent.close_memory()
    assert agent.memory is None


# ─── _inject_memories ─────────────────────────────────────────


async def test_inject_memories_no_op_when_memory_disabled() -> None:
    agent = _Stub("disabled", config={})
    task = TaskInput(task_id="t", workflow_id="w", payload="any query")
    out = await agent._inject_memories("SYSTEM", task)
    assert out == "SYSTEM"


async def test_inject_memories_no_op_when_recall_empty() -> None:
    agent = _Stub(
        "enabled",
        config={"memory": {"enabled": True, "db_path": ":memory:"}},
    )
    try:
        await agent.initialize_memory()
        task = TaskInput(
            task_id="t", workflow_id="w", payload="never-stored query",
        )
        out = await agent._inject_memories("SYSTEM", task)
        assert out == "SYSTEM"
    finally:
        await agent.close_memory()


async def test_inject_memories_appends_preamble_when_match_exists() -> None:
    agent = _Stub(
        "enabled",
        config={"memory": {"enabled": True, "db_path": ":memory:"}},
    )
    try:
        await agent.initialize_memory()
        assert agent.memory is not None
        await agent.memory.store_observation(
            "the user prefers Python type hints",
            importance=0.9,
            tags=("preferences",),
        )
        task = TaskInput(
            task_id="t",
            workflow_id="w",
            payload="python type hints",
        )
        out = await agent._inject_memories("SYSTEM", task)
        assert "SYSTEM" in out
        assert "Relevant memories" in out
        assert "type hints" in out
    finally:
        await agent.close_memory()


async def test_inject_memories_tolerates_malformed_context_package() -> None:
    agent = _Stub(
        "enabled",
        config={"memory": {"enabled": True, "db_path": ":memory:"}},
    )
    try:
        await agent.initialize_memory()
        assert agent.memory is not None
        await agent.memory.store_observation(
            "the user uses tabs", importance=0.9,
        )
        task = TaskInput(
            task_id="t",
            workflow_id="w",
            payload="tabs",
            context={CONTEXT_PACKAGE_KEY: "not-a-json-payload"},
        )
        # Must not raise; preamble still appended from the recall.
        out = await agent._inject_memories("SYSTEM", task)
        assert "Relevant memories" in out
    finally:
        await agent.close_memory()


async def test_inject_memories_reads_budget_from_context_package() -> None:
    agent = _Stub(
        "enabled",
        config={"memory": {"enabled": True, "db_path": ":memory:"}},
    )
    try:
        await agent.initialize_memory()
        assert agent.memory is not None
        for i in range(3):
            await agent.memory.store_observation(
                f"observation number {i} about widgets",
                importance=0.9,
            )
        package = {
            "version": 1,
            "pinned_sections": [],
            "step_outputs": [],
            "metrics": {
                "tokens_before": 0, "tokens_after": 0,
                "compression_ratio": 1.0, "candidates_dropped": 0,
            },
            "budget_memory_tokens": 200,
        }
        task = TaskInput(
            task_id="t",
            workflow_id="w",
            payload="widgets observation",
            context={CONTEXT_PACKAGE_KEY: json.dumps(package)},
        )
        out = await agent._inject_memories("SYSTEM", task)
        # 200 tokens / 100 avg-per-entry == limit=2 → at most two memory lines.
        memory_lines = [
            line for line in out.splitlines()
            if line.startswith("- ")
        ]
        assert len(memory_lines) <= 2
    finally:
        await agent.close_memory()


# ─── Re-exports ──────────────────────────────────────────────


def test_context_package_key_constant_exposed() -> None:
    """Stable consumer-facing constant — pinned by tests/unit so a rename trips CI."""
    assert CONTEXT_PACKAGE_KEY == "_context_package"


@pytest.mark.skip(reason="Phase 2 stub — full integration covered by RFC 0008 PR 2 e2e")
async def test_run_llm_loop_reads_memory_in_e2e() -> None:  # pragma: no cover
    pass
