"""Unit tests for RFC 0023 PR 5 — persona TASK_ASSIGNED lease wiring (ISSUE-0063).

A workflow step dispatched to a persona agent (``PersonaAgent.handle``)
wraps the task as a ``TASK_ASSIGNED`` event and routes through the
persona action loop, not :meth:`BaseAgent._run_llm_loop`. RFC 0023 PR 3
retired the scheduler's post-hoc ``recordStepUsage`` counter feed on
the assumption that every workflow-step LLM call is leased — true for
:class:`TaskAgent` (the leased ``_run_llm_loop``), but **not** for the
persona action loop, which until this PR was un-leased. The gap is
latent (no shipped workflow targets a persona) but unguarded; one
operator-authored workflow whose step ``agent`` is a persona id (e.g.
``ember-owl``) reactivates it.

ISSUE-0063 closes here by mapping ``EventType.TASK_ASSIGNED`` to
``CAUSE_WORKFLOW_TASK`` in :func:`cause_for_event`. The persona
TASK_ASSIGNED LLM call now acquires a wallet lease tagged
``CAUSE_WORKFLOW_TASK`` against the persona's own ``agent_id``, so its
spend reaches the budget ``TokenCounter`` via the wallet's provisional
/ reconcile pair — the same recording authority the PR 3 retirement
relied on. The PR plan offered the alternative of a planner-side
guard; leasing is the route taken (one-line cause map, no new
constraint), recorded in ``docs/issues/ISSUE-0063-*.md`` as the
chosen resolution.
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
    "id": "task-persona",
    "type": "persona",
    "name": "Task Persona",
    "role": "ISSUE-0063 persona-as-workflow-step fixture",
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
        agent_id="task-persona",
        config=copy.deepcopy(_PERSONA_CONFIG),
        llm_client=client,
    )
    await agent.initialize_memory()
    return agent


def _nonzero_injection() -> MemoryInjectionResult:
    return MemoryInjectionResult(memory_admitted_tokens=10)


@pytest.mark.asyncio
async def test_persona_task_assigned_passes_cause_workflow_task() -> None:
    """A workflow step dispatched to a persona must acquire a
    ``CAUSE_WORKFLOW_TASK`` lease — same cause as :class:`TaskAgent`'s
    ``_run_llm_loop`` (PR 3) — so the wallet records the spend and
    the post-PR 3 budget counter stays consistent."""
    client = _make_client_with_recording_create()
    agent = await _make_agent(client)
    try:
        task = TaskInput(
            task_id="step-1",
            workflow_id="wf-test",
            payload="do the thing",
            config=TaskInputConfig(),
        )
        # The persona handle() wraps the TaskInput in event.payload, which
        # _store_event_episode would JSON-encode into the episode context.
        # The dataclass is not JSON-serialisable so we no-op the episode
        # write — episode shape is not under test here.
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

        calls: list[dict[str, Any]] = client._recorded_calls  # type: ignore[attr-defined]
        assert calls, "create_message must be invoked for a TASK_ASSIGNED event"
        first = calls[0]
        assert first.get("cause") == walletpb.CAUSE_WORKFLOW_TASK, (
            "PR 5 / ISSUE-0063: persona TASK_ASSIGNED must tag the lease with "
            "CAUSE_WORKFLOW_TASK so the wallet records the spend that the "
            "scheduler's recordStepUsage no longer writes "
            f"(got {first.get('cause')!r})"
        )
        assert first.get("agent_id") == "task-persona", (
            "PR 5 / ISSUE-0063: persona TASK_ASSIGNED lease must be acquired "
            "against the persona's own agent_id"
        )
    finally:
        await agent.close_memory()
