"""Unit tests for RFC 0023 PR 5 — sub-agent wallet-lease wiring.

A sub-agent spawn is dispatched through
:meth:`SubAgentSpawner.dispatch`, which calls ``child.handle(task)``.
The child's :meth:`BaseAgent._run_llm_loop` already brackets the LLM
call in a wallet lease (PR 3), but it tags the lease with
``CAUSE_WORKFLOW_TASK`` against the *child's* ``agent_id``. PR 5 must:

1. tag the lease with ``CAUSE_SUB_AGENT``, and
2. attribute the spend to the *parent's* ``agent_id`` so per-persona
   cost dashboards continue to bill the originating persona for work
   it delegated.

The spawner is the natural carrier of "this is a sub-agent
invocation": it already knows the parent id (constructor arg). PR 5
plumbs ``parent_agent_id`` into the dispatched
:class:`TaskInputConfig`; :meth:`BaseAgent._run_llm_loop` flips
``cause`` to ``CAUSE_SUB_AGENT`` and the lease's ``agent_id`` to the
parent whenever the field is non-empty. The active-lease cap stays
per-process per RFC 0023 Open Question §7 — attribution is the only
difference, not the concurrency ceiling.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.base import BaseAgent, TaskInput, TaskInputConfig, TaskOutput, TaskStatus
from agents.generated import wallet_pb2 as walletpb
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.sub_agents.delegation import (
    DELEGATION_RESULT_KEY,
    BudgetEnvelope,
    DelegationRequest,
    DelegationResult,
)
from agents.sub_agents.spawner import SubAgentSpawner

# ─── Fixtures ─────────────────────────────────────────────────────────────────


class _RecordingChild(BaseAgent):
    """A minimal :class:`BaseAgent` that records the dispatched task and
    emits a valid :class:`DelegationResult` so the spawner's merge step
    succeeds.  Does **not** invoke an LLM — we exercise the LLM-call
    path separately in :class:`TestRunLLMLoopSubAgentBranch`."""

    def __init__(self) -> None:
        super().__init__("child-agent", config={})
        self.received: TaskInput | None = None

    async def handle(self, task: TaskInput) -> TaskOutput:
        self.received = task
        result = DelegationResult(
            status="completed",
            summary="done",
            artifacts={},
            memory_writes=(),
        )
        return TaskOutput(
            status=TaskStatus.COMPLETED,
            result="done",
            metadata={DELEGATION_RESULT_KEY: result.to_json()},
        )


def _request() -> DelegationRequest:
    return DelegationRequest(
        objective="do a subtask",
        budget=BudgetEnvelope(max_llm_calls=1, tokens=128),
    )


# ─── Spawner plumbs parent_agent_id ──────────────────────────────────────────


class TestSpawnerPlumbsParentAgentId:
    """:meth:`SubAgentSpawner.dispatch` must mark the dispatched task
    as a sub-agent invocation by setting
    ``TaskInputConfig.sub_agent_parent_id``."""

    @pytest.mark.asyncio
    async def test_dispatch_sets_parent_agent_id_on_task_config(self) -> None:
        spawner = SubAgentSpawner(parent_agent_id="parent-persona")
        child = _RecordingChild()
        await spawner.dispatch(child, _request())

        assert child.received is not None, "child handle must have been invoked"
        assert child.received.config.sub_agent_parent_id == "parent-persona", (
            "PR 5: spawner must thread the parent agent_id through "
            "TaskInputConfig.sub_agent_parent_id so the child's leased LLM "
            "call attributes spend to the parent persona "
            f"(got {child.received.config.sub_agent_parent_id!r})"
        )


# ─── _run_llm_loop honours the sub-agent marker ──────────────────────────────


class _AlwaysCompleteAgent(BaseAgent):
    """Exposes :meth:`_run_llm_loop` directly so tests can pin the
    cause/agent_id passed to ``LLMClient.create_message``."""

    def __init__(self, llm_client: LLMClient) -> None:
        super().__init__("child-agent", config={"model": "claude-x"}, llm_client=llm_client)

    async def handle(self, task: TaskInput) -> TaskOutput:  # pragma: no cover — unused
        return await self._run_llm_loop(task, system_prompt="hi")


def _make_recording_llm() -> tuple[LLMClient, list[dict[str, Any]]]:
    provider = AsyncMock()
    provider.name = "anthropic"
    provider.create_message = AsyncMock(return_value=LLMResponse(
        text="ok", stop_reason=StopReason.END_TURN,
        usage=Usage(input_tokens=5, output_tokens=5),
    ))
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(return_value=[])
    client = LLMClient(provider)
    real_create_message = client.create_message
    calls: list[dict[str, Any]] = []

    async def _record(*args: Any, **kwargs: Any) -> LLMResponse:
        calls.append(dict(kwargs))
        return await real_create_message(*args, **kwargs)

    client.create_message = _record  # type: ignore[method-assign]
    return client, calls


class TestRunLLMLoopSubAgentBranch:
    """When ``TaskInputConfig.sub_agent_parent_id`` is non-empty, the
    lease must be tagged ``CAUSE_SUB_AGENT`` and attributed to the
    parent's ``agent_id``."""

    @pytest.mark.asyncio
    async def test_sub_agent_task_uses_cause_sub_agent_and_parent_id(self) -> None:
        client, calls = _make_recording_llm()
        agent = _AlwaysCompleteAgent(client)
        task = TaskInput(
            task_id="sub-1",
            workflow_id="delegation",
            payload="do a subtask",
            config=TaskInputConfig(sub_agent_parent_id="parent-persona"),
        )
        out = await agent.handle(task)
        assert out.status == TaskStatus.COMPLETED

        assert calls, "create_message must be invoked once"
        kwargs = calls[0]
        assert kwargs.get("cause") == walletpb.CAUSE_SUB_AGENT, (
            "PR 5: a task carrying sub_agent_parent_id must lease with "
            f"CAUSE_SUB_AGENT (got {kwargs.get('cause')!r})"
        )
        assert kwargs.get("agent_id") == "parent-persona", (
            "PR 5: sub-agent leases must be attributed to the parent persona, "
            f"not the child (got {kwargs.get('agent_id')!r})"
        )

    @pytest.mark.asyncio
    async def test_workflow_task_without_parent_id_unchanged(self) -> None:
        """Regression: PR 3's workflow-task path must still tag
        ``CAUSE_WORKFLOW_TASK`` against the child's own agent_id when
        ``sub_agent_parent_id`` is empty (the default)."""
        client, calls = _make_recording_llm()
        agent = _AlwaysCompleteAgent(client)
        task = TaskInput(
            task_id="wf-1",
            workflow_id="wf",
            payload="do the thing",
            config=TaskInputConfig(),
        )
        await agent.handle(task)
        kwargs = calls[0]
        assert kwargs.get("cause") == walletpb.CAUSE_WORKFLOW_TASK
        assert kwargs.get("agent_id") == "child-agent"
