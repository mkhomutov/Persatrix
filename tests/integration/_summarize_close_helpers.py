"""Shared helpers for the RFC 0020 PR 4 summarisation-on-close integration tests.

Extracted from ``test_summarize_on_close.py`` so the original module +
the new ``test_summarize_on_close_phases.py`` (PR #229 review Must-Fix
#1 + Should-Fix #3 / #4 coverage) can share the persona config, mock
LLM clients, and episode-state probes without either file blowing
through the 500-line size cap enforced by
``scripts/checks/file_size.py --strict``.

The leading underscore prevents pytest from collecting this module as
a test file.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_runtime.summarize_close import SUMMARIZATION_MAX_OUTPUT_TOKENS
from agents.persona_types import AgentEvent, EventType

LLM_SUMMARY_TEXT = (
    "Persona discussed weekend plans with the user across ten turns; "
    "agreed to meet Saturday morning."
)


PERSONA_CONFIG: dict = {
    "id": "summary-on-close-persona",
    "model": "test-model",
    "role": "PR 4 summary-on-close test persona",
    "type": "persona",
    "max_llm_calls": 5,
    # Persona event-loop output cap.  Deliberately kept distinct from
    # ``SUMMARIZATION_MAX_OUTPUT_TOKENS`` so the mock LLM clients below
    # can route the summariser call apart from the persona-loop call by
    # ``max_tokens`` alone — the two calls would otherwise be
    # indistinguishable to the mock.
    "max_tokens": 4096,
    "tools": [],
    "persona": {
        "name": "Summary Agent",
        "background": "RFC 0020 PR 4 summarisation-on-close test persona.",
        "behavior": {
            "directness": "balanced",
            "formality": "professional",
            "risk_tolerance": "moderate",
        },
    },
    "autonomy": {
        "level": "semi-autonomous",
        "tick_interval_seconds": 1,
        "max_actions_per_tick": 3,
        "idle_after_ticks": 5,
    },
    "memory": {
        "db_path": ":memory:",
        "working": {"max_tokens": 50000},
        "interaction_idle_timeout_sec": 5.0,
        # Activate the auto-reflect counter so increment_interaction_count
        # actually fires (RFC 0020 §H — gated on auto_reflect_after > 0).
        "notes": {"auto_reflect_after": 3},
    },
    "relationships": [],
}


def make_summary_client(text: str = LLM_SUMMARY_TEXT) -> LLMClient:
    """Build a mock LLM client that branches on ``max_tokens``.

    The summariser pins ``max_tokens=SUMMARIZATION_MAX_OUTPUT_TOKENS``
    while the persona event loop uses the persona config's
    ``max_tokens``, so we route the summarisation call to the prose
    ``text`` and the persona call to a JSON action list.
    """
    mock_provider = AsyncMock()

    async def _route(*, model, messages, system, tools, max_tokens, temperature):
        if max_tokens == SUMMARIZATION_MAX_OUTPUT_TOKENS:  # summarisation call
            return LLMResponse(
                text=text,
                stop_reason=StopReason.END_TURN,
                usage=Usage(120, 30),
            )
        return LLMResponse(  # persona event-loop call
            text='```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
            stop_reason=StopReason.END_TURN,
            usage=Usage(10, 5),
        )

    mock_provider.create_message = AsyncMock(side_effect=_route)
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: msgs,
    )
    return LLMClient(mock_provider)


@dataclass
class GatedSummaryClient:
    """Triple returned by :func:`make_gated_summary_client`.

    ``started`` is set by the mock the first time the summariser call
    enters; ``gate`` is awaited until the test releases it.  Tests use
    ``await started.wait()`` instead of ``await asyncio.sleep(0)`` to
    deterministically observe Phase-2 mid-flight (PR 6 review #30).
    """
    client: LLMClient
    started: asyncio.Event
    gate: asyncio.Event


def make_gated_summary_client(text: str = LLM_SUMMARY_TEXT) -> GatedSummaryClient:
    """Build a summary client whose summariser call parks on a gate.

    Used by the Phase-1 sentinel-visible test (PR 6 review #30) and
    the Phase-2 ↔ janitor race regression test (PR 6 review #20/#26)
    to deterministically interleave the close path with the janitor.
    """
    started = asyncio.Event()
    gate = asyncio.Event()
    mock_provider = AsyncMock()

    async def _route(*, model, messages, system, tools, max_tokens, temperature):
        if max_tokens == SUMMARIZATION_MAX_OUTPUT_TOKENS:
            started.set()
            await gate.wait()
            return LLMResponse(
                text=text, stop_reason=StopReason.END_TURN,
                usage=Usage(120, 30),
            )
        return LLMResponse(
            text='```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
            stop_reason=StopReason.END_TURN, usage=Usage(10, 5),
        )

    mock_provider.create_message = AsyncMock(side_effect=_route)
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: msgs,
    )
    return GatedSummaryClient(LLMClient(mock_provider), started, gate)


def make_failing_summary_client(exc: Exception) -> LLMClient:
    """Mock LLM client whose summarisation call raises ``exc``."""
    mock_provider = AsyncMock()

    async def _route(*, model, messages, system, tools, max_tokens, temperature):
        if max_tokens == SUMMARIZATION_MAX_OUTPUT_TOKENS:
            raise exc
        return LLMResponse(
            text='```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
            stop_reason=StopReason.END_TURN,
            usage=Usage(10, 5),
        )

    mock_provider.create_message = AsyncMock(side_effect=_route)
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: msgs,
    )
    return LLMClient(mock_provider)


async def make_agent(client: LLMClient | None = None) -> _LLMPersonaAgent:
    agent = create_persona_agent(
        agent_id=PERSONA_CONFIG["id"],
        config=PERSONA_CONFIG,
        llm_client=client or make_summary_client(),
    )
    await agent.initialize_memory()
    return agent


async def episode_summary(agent: _LLMPersonaAgent) -> str:
    db = agent._episodic_memory._ensure_db()
    async with db.execute(
        "SELECT summary FROM episodes WHERE agent_id = ? ORDER BY created_at",
        (agent.agent_id,),
    ) as cursor:
        rows = list(await cursor.fetchall())
    assert len(rows) == 1, f"expected exactly one episode, got {len(rows)}"
    return rows[0][0]


async def drain(agent: _LLMPersonaAgent) -> None:
    """Await every in-flight background summary task.

    PR #229 review Must-Fix #1 + Should-Fix #1 moved the LLM
    summarisation off the per-agent ``_lock`` and into a background
    task spawned by :meth:`_persist_closed_interaction`.  Tests that
    assert on the *final* episode ``summary`` column must drain the
    pending tasks first; otherwise the assertion races the background
    task and reads the ``[summary pending]`` sentinel.
    """
    await agent.drain_pending_summaries()


async def send_n_turns(
    agent: _LLMPersonaAgent, peer: str, n: int,
) -> None:
    for i in range(n):
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": f"turn {i}"},
            sender_id=peer,
        ))
