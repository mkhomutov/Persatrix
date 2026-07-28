"""RFC 0037 Phase-2 integration — §E projections end to end (PR 6).

The RFC's Phase-2 acceptance case: a persona closes a ``restricted``
interaction (the combined close-consolidation call returns the §E
``projections`` half), then acts in a ``public`` channel — the assembled
working memory carries the declassified one-liner, NOT the verbatim
protected text; acting back at ``restricted`` sees the verbatim summary
again.  The producer (``persist_closed_interaction`` Phase 1 + 2 →
``memory_projections`` rows) and the consumer (the §D gate's projection
branch inside ``_inject_memory_context``) are exercised through the real
runtime seams — nothing is seeded directly into ``memory_projections``.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.memory.interaction_types import Interaction, Turn
from agents.memory.projections import ENTRY_TIER_EPISODE, projections_for
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_runtime.close_path import persist_closed_interaction
from agents.persona_runtime.finalize_close import drain_pending_summary_tasks
from agents.persona_types import AgentEvent, EventType
from agents.tools.registry import clear_registry

_SECRET_TOKEN = "REDWOLF-2291"
_PUBLIC_LINE = "A quarterly roadmap decision was made."
_INTERNAL_LINE = "The sunset question was settled by leadership."

_ENVELOPE = json.dumps({
    "summary": f"Leadership decided to sunset {_SECRET_TOKEN} next quarter.",
    "facts": [],
    "projections": {"public": _PUBLIC_LINE, "internal": _INTERNAL_LINE},
})


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _persona_config(agent_id: str) -> dict:
    return {
        "id": agent_id,
        "model": "test-model",
        "role": "RFC 0037 §E projection integration persona",
        "type": "persona",
        "max_llm_calls": 5,
        "max_tokens": 1024,
        "tools": [],
        "persona": {
            "name": agent_id,
            "background": "Used by the RFC 0037 §E projection tests.",
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
        },
        "relationships": [],
    }


def _envelope_client() -> LLMClient:
    provider = AsyncMock()
    provider.create_message = AsyncMock(return_value=LLMResponse(
        text=_ENVELOPE, stop_reason=StopReason.END_TURN, usage=Usage(120, 40),
    ))
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: msgs,
    )
    return LLMClient(provider)


def _restricted_interaction() -> Interaction:
    return Interaction(
        interaction_id="ix-leadership",
        scope="group:leadership",
        started_at=0.0,
        closed_at=30.0,
        close_reason="structural",
        classification="restricted",
        source_channel_id="group:leadership",
        turns=[
            Turn(at=0.0, payload={
                "sender": "alice", "summary": "opened",
                "text": f"we will sunset {_SECRET_TOKEN} next quarter",
            }),
            Turn(at=20.0, payload={"sender": "proj-agent", "summary": "ack"}),
        ],
    )


def _event(classification: str, content: str) -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        channel_id="group:town-square",
        sender_id="alice",
        payload={
            "content": content,
            "respond_policy": "always",
            "mentions": [],
            "thread_parent_sender_id": "",
        },
        metadata={"channel_classification": classification},
    )


async def _make_agent() -> _LLMPersonaAgent:
    agent = create_persona_agent(
        agent_id="proj-agent",
        config=_persona_config("proj-agent"),
        llm_client=_envelope_client(),
    )
    await agent.initialize_memory()
    return agent


async def _close_restricted_interaction(agent: _LLMPersonaAgent) -> None:
    """Run the REAL two-phase close over the restricted interaction: the
    Phase-1 stamped episode row, then the Phase-2 combined call whose
    envelope carries the §E projections half."""
    pending: set[asyncio.Task[None]] = set()

    async def _noop() -> None:
        return None

    await persist_closed_interaction(
        episodic=agent._episodic_memory,
        llm_client=agent._llm_client,
        memory_ns=agent.memory,
        agent_id=agent.agent_id,
        interaction=_restricted_interaction(),
        pending_tasks=pending,
        on_finalized=_noop,
    )
    await drain_pending_summary_tasks(pending)


def _visible_memory(agent: _LLMPersonaAgent) -> str:
    return "\n".join(s.content for s in agent._working_memory._sections)


@pytest.mark.asyncio
class TestProjectionArc:
    async def test_close_writes_the_projection_rows(self) -> None:
        agent = await _make_agent()
        try:
            await _close_restricted_interaction(agent)
            rows = await projections_for(
                agent._episodic_memory,
                entry_tier=ENTRY_TIER_EPISODE,
                entry_ids=["ix-leadership"],
                levels=["public", "internal"],
            )
            assert rows == {"ix-leadership": [
                ("internal", _INTERNAL_LINE),
                ("public", _PUBLIC_LINE),
            ]}
        finally:
            await agent.close_memory()

    async def test_public_turn_is_informed_without_disclosure(self) -> None:
        """The RFC's Phase-2 sentence, verbatim: informed by — but does
        not verbatim-disclose — a restricted memory, via its projection."""
        agent = await _make_agent()
        try:
            await _close_restricted_interaction(agent)
            await agent._inject_memory_context(
                _event("public", "any news on the sunset plan?"),
                query="sunset",
            )
            visible = _visible_memory(agent)
            assert _PUBLIC_LINE in visible
            assert _SECRET_TOKEN not in visible
        finally:
            await agent.close_memory()

    async def test_internal_turn_gets_the_higher_projection(self) -> None:
        """Highest-``≤ L`` selection through the runtime: the ``internal``
        turn is served the ``internal`` abstraction, not the ``public``
        one, and still never the verbatim text."""
        agent = await _make_agent()
        try:
            await _close_restricted_interaction(agent)
            await agent._inject_memory_context(
                _event("internal", "any news on the sunset plan?"),
                query="sunset",
            )
            visible = _visible_memory(agent)
            assert _INTERNAL_LINE in visible
            assert _PUBLIC_LINE not in visible
            assert _SECRET_TOKEN not in visible
        finally:
            await agent.close_memory()

    async def test_restricted_turn_still_sees_the_verbatim_summary(
        self,
    ) -> None:
        agent = await _make_agent()
        try:
            await _close_restricted_interaction(agent)
            await agent._inject_memory_context(
                _event("restricted", "any news on the sunset plan?"),
                query="sunset",
            )
            visible = _visible_memory(agent)
            assert _SECRET_TOKEN in visible
        finally:
            await agent.close_memory()

    async def test_manifest_reports_the_projection_level(self) -> None:
        agent = await _make_agent()
        try:
            await _close_restricted_interaction(agent)
            result = await agent._inject_memory_context(
                _event("public", "any news on the sunset plan?"),
                query="sunset",
            )
            served = [
                entry for entry in result.manifest
                if entry.tier in ("episodic", "channel_history")
            ]
            assert served
            assert all(
                entry.protection_level == "public" for entry in served
            )
        finally:
            await agent.close_memory()
