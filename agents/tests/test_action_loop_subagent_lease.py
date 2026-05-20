"""Unit tests for ISSUE-0064 — persona-as-sub-agent attribution.

RFC 0023 PR 5 wired ``CAUSE_SUB_AGENT`` + parent-agent attribution in
:meth:`BaseAgent._run_llm_loop`. A ``PersonaAgent`` dispatched as a
sub-agent child does **not** use that path — :meth:`PersonaAgent.handle`
wraps the task as a ``TASK_ASSIGNED`` event and routes through the
persona action loop, whose `cause_for_event` returns
``CAUSE_WORKFLOW_TASK`` and whose lease is acquired against
``self.agent_id`` (the child). The ``sub_agent_parent_id`` field the
spawner threads on the dispatched ``TaskInputConfig`` was silently
ignored on the persona path, so a persona sub-agent's spend billed the
*child* rather than the *delegating parent*.

ISSUE-0064 closes here by teaching the persona action loop to honour
``task.config.sub_agent_parent_id`` on the TASK_ASSIGNED path — exact
twin of the override PR 5 added to ``BaseAgent._run_llm_loop``. When
the marker is non-empty:

1. the lease cause is overridden to ``CAUSE_SUB_AGENT``, and
2. the lease ``agent_id`` is overridden to the parent's id so per-persona
   cost dashboards bill the delegating persona.

The marker is empty by default, so a workflow-step dispatched to a
persona (the ISSUE-0063 path) keeps ``CAUSE_WORKFLOW_TASK`` against the
persona's own ``agent_id``.

The issue is **latent today** — ``SPAWN_SUB_AGENT`` returns
``{"status": "not_implemented"}`` in ``agents/action_executor.py`` and
no production caller routes a persona through ``SubAgentSpawner``.
Closing it now keeps the persona action loop symmetric with
``_run_llm_loop`` so future RFC 0023 work that adds a new cause does
not have to re-discover this fork.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.base import TaskInput, TaskInputConfig
from agents.generated import wallet_pb2 as walletpb
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_runtime.memory_context import MemoryInjectionResult

_PERSONA_CONFIG: dict[str, Any] = {
    "id": "child-persona",
    "type": "persona",
    "name": "Child Persona",
    "role": "ISSUE-0064 persona-as-sub-agent fixture",
    "model": "claude-sonnet-4-6",
    "temperature": 0.3,
    "max_llm_calls": 5,
    "max_tokens": 256,
    "persona": {"background": "Test fixture.", "behavior": {}},
    "permissions": {"memory": {"read": True, "write": True}},
    "memory": {"db_path": ":memory:"},
}


def _llm_response() -> LLMResponse:
    return LLMResponse(
        text='[{"action_type": "complete_task", "payload": {"result": "done"}}]',
        stop_reason=StopReason.END_TURN,
        usage=Usage(input_tokens=12, output_tokens=8),
    )


def _make_client_with_recording_create() -> LLMClient:
    provider = AsyncMock()
    provider.name = "anthropic"
    provider.create_message = AsyncMock(return_value=_llm_response())
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(return_value=[])
    client = LLMClient(provider)
    real_create_message = client.create_message
    calls: list[dict[str, Any]] = []

    async def _record(*args: Any, **kwargs: Any) -> LLMResponse:
        calls.append(dict(kwargs))
        return await real_create_message(*args, **kwargs)

    client.create_message = _record  # type: ignore[method-assign]
    client._recorded_calls = calls  # type: ignore[attr-defined]
    return client


async def _make_agent(client: LLMClient) -> _LLMPersonaAgent:
    agent = create_persona_agent(
        agent_id="child-persona",
        config=copy.deepcopy(_PERSONA_CONFIG),
        llm_client=client,
    )
    await agent.initialize_memory()
    return agent


def _nonzero_injection() -> MemoryInjectionResult:
    return MemoryInjectionResult(memory_admitted_tokens=10)


async def _drive_persona_task(
    agent: _LLMPersonaAgent, task: TaskInput,
) -> None:
    """Run a persona ``handle(task)`` past the LLM call with episode /
    state persistence stubbed out (the dataclass payload is not
    JSON-serialisable; the episode shape is not under test here)."""

    async def _noop_episode(*args: Any, **kwargs: Any) -> None:
        return None

    async def _noop_state() -> None:
        return None

    with patch.object(
        agent, "_inject_memory_context", return_value=_nonzero_injection(),
    ), patch.object(
        agent, "_store_event_episode", side_effect=_noop_episode,
    ), patch.object(
        agent, "_persist_persona_state", side_effect=_noop_state,
    ):
        await agent.handle(task)


# ─── Persona-as-sub-agent attribution override ───────────────────────────────


class TestPersonaSubAgentAttribution:
    """The persona action loop must honour ``sub_agent_parent_id``.

    Twin of :class:`tests.unit.python.test_subagent_lease.TestRunLLMLoopSubAgentBranch`
    — same override, on the parallel persona LLM-call origin.
    """

    @pytest.mark.asyncio
    async def test_persona_sub_agent_task_uses_cause_sub_agent_and_parent_id(self) -> None:
        client = _make_client_with_recording_create()
        agent = await _make_agent(client)
        try:
            task = TaskInput(
                task_id="sub-1",
                workflow_id="delegation",
                payload="do a subtask as a persona",
                config=TaskInputConfig(sub_agent_parent_id="parent-persona"),
            )
            await _drive_persona_task(agent, task)

            calls: list[dict[str, Any]] = client._recorded_calls  # type: ignore[attr-defined]
            assert calls, "create_message must be invoked for a TASK_ASSIGNED event"
            first = calls[0]
            assert first.get("cause") == walletpb.CAUSE_SUB_AGENT, (
                "ISSUE-0064: a persona TASK_ASSIGNED carrying "
                "sub_agent_parent_id must lease with CAUSE_SUB_AGENT — "
                "symmetric with BaseAgent._run_llm_loop's PR 5 override "
                f"(got {first.get('cause')!r})"
            )
            assert first.get("agent_id") == "parent-persona", (
                "ISSUE-0064: persona-as-sub-agent leases must be attributed "
                "to the parent persona, not the child "
                f"(got {first.get('agent_id')!r})"
            )
        finally:
            await agent.close_memory()

    @pytest.mark.asyncio
    async def test_persona_workflow_task_without_parent_id_unchanged(self) -> None:
        """Regression: a workflow-step dispatched to a persona (the
        ISSUE-0063 path, no ``sub_agent_parent_id``) must still tag
        ``CAUSE_WORKFLOW_TASK`` against the persona's own ``agent_id``.
        ISSUE-0064's override is gated on a non-empty marker — the
        default-empty case must not regress."""
        client = _make_client_with_recording_create()
        agent = await _make_agent(client)
        try:
            task = TaskInput(
                task_id="wf-1",
                workflow_id="wf",
                payload="do the thing",
                config=TaskInputConfig(),
            )
            await _drive_persona_task(agent, task)

            calls: list[dict[str, Any]] = client._recorded_calls  # type: ignore[attr-defined]
            assert calls
            first = calls[0]
            assert first.get("cause") == walletpb.CAUSE_WORKFLOW_TASK, (
                "ISSUE-0063 invariant: a persona TASK_ASSIGNED without "
                "sub_agent_parent_id must still lease with CAUSE_WORKFLOW_TASK "
                f"(got {first.get('cause')!r})"
            )
            assert first.get("agent_id") == "child-persona", (
                "ISSUE-0063 invariant: a persona TASK_ASSIGNED without "
                "sub_agent_parent_id must lease against the persona's own id "
                f"(got {first.get('agent_id')!r})"
            )
        finally:
            await agent.close_memory()

    @pytest.mark.asyncio
    async def test_persona_sub_agent_empty_string_parent_id_unchanged(self) -> None:
        """Defense in depth: an explicit empty-string ``sub_agent_parent_id``
        (the dataclass default) must not trip the override — only a
        *non-empty* parent id flips the cause / attribution."""
        client = _make_client_with_recording_create()
        agent = await _make_agent(client)
        try:
            task = TaskInput(
                task_id="wf-2",
                workflow_id="wf",
                payload="do the thing",
                config=TaskInputConfig(sub_agent_parent_id=""),
            )
            await _drive_persona_task(agent, task)

            calls: list[dict[str, Any]] = client._recorded_calls  # type: ignore[attr-defined]
            first = calls[0]
            assert first.get("cause") == walletpb.CAUSE_WORKFLOW_TASK
            assert first.get("agent_id") == "child-persona"
        finally:
            await agent.close_memory()
