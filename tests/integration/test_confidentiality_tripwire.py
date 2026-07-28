"""RFC 0037 Phase-3 integration — the §G leak tripwire end to end (PR 7).

The RFC's Test Strategy integration case, through the real turn: a
persona holds ``restricted`` memory, acts in an ``internal`` room (the
§D gate withholds the verbatim entries and the turn stamps its tripwire
watch), and the parsed ``SEND_CHANNEL_MESSAGE`` is executed through the
real ``ActionExecutor`` with the context ``DispatchContext.for_event``
derives — the full production seam.  When the outgoing text carries a
verbatim span of a withheld entry, the metadata-only
``channel.confidentiality_tripwire`` audit record fires and the message
still publishes; benign traffic is silent; and a turn with nothing
withheld stamps no watch at all (the common-case economics).

Because §D keeps the withheld text out of the prompt, the "leak" here is
seeded through the mock LLM reply — the executor cannot tell a model
echo from a mis-stamped injection, which is exactly the §G posture (a
true hit indicates a bug upstream of the model).
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.channel_wire_metadata import DispatchContext
from agents.confidentiality_tripwire import (
    AUDIT_EVENT_TRIPWIRE,
    TRIPWIRE_WATCH_METADATA_KEY,
)
from agents.dispatch import ActionExecutor
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import ActionType, AgentEvent, EventType
from agents.tools.registry import clear_registry

# The protected sentence: 12 normalized words, comfortably above the §G
# span threshold, with a distinctive token for the metadata-only check.
_PROTECTED_SUMMARY = (
    "Leadership agreed to sunset the REDWOLF-2291 programme and move the "
    "whole team to the Nightjar platform next quarter"
)
_CHANNEL = "group:town-square"


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _persona_config(agent_id: str) -> dict:
    return {
        "id": agent_id,
        "model": "test-model",
        "role": "RFC 0037 §G tripwire test persona",
        "type": "persona",
        "max_llm_calls": 5,
        "max_tokens": 1024,
        "tools": [],
        "persona": {
            "name": agent_id,
            "background": "Used by the RFC 0037 §G tripwire integration tests.",
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


def _client(reply_json: str) -> LLMClient:
    mock_provider = AsyncMock()
    mock_provider.create_message = AsyncMock(
        return_value=LLMResponse(
            text=reply_json, stop_reason=StopReason.END_TURN, usage=Usage(10, 5),
        ),
    )
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: msgs,
    )
    return LLMClient(mock_provider)


def _send_reply(content: str) -> str:
    return (
        '```json\n[{"action_type": "send_channel_message", '
        f'"payload": {{"channel_id": "{_CHANNEL}", '
        f'"content": "{content}"}}}}]\n```'
    )


async def _make_agent(reply_json: str) -> _LLMPersonaAgent:
    agent = create_persona_agent(
        agent_id="tripwire-agent",
        config=_persona_config("tripwire-agent"),
        llm_client=_client(reply_json),
    )
    await agent.initialize_memory()
    return agent


def _event(classification: str, content: str) -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        channel_id=_CHANNEL,
        sender_id="alice",
        payload={
            "content": content,
            "respond_policy": "always",
            "mentions": [],
            "thread_parent_sender_id": "",
        },
        metadata={"channel_classification": classification},
    )


def _executor() -> tuple[ActionExecutor, AsyncMock]:
    publisher = AsyncMock()
    publisher.publish = AsyncMock(return_value=None)
    return ActionExecutor(channel_publisher=publisher), publisher


def _audit_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        rec
        for rec in caplog.records
        if getattr(rec, "audit", None) is True
        and rec.getMessage() == AUDIT_EVENT_TRIPWIRE
    ]


async def _run_turn(
    agent: _LLMPersonaAgent, event: AgentEvent,
) -> list:
    """The production dispatch shape: the turn, then the executor with
    the structurally-derived context."""
    async with agent._lock:
        actions = await agent._on_event_inner(event)
    executor, publisher = _executor()
    await executor.execute(
        agent.agent_id, actions,
        context=DispatchContext.for_event(event, cascade_depth=1),
    )
    return [actions, publisher]


class TestTripwireEndToEnd:
    async def test_verbatim_echo_fires_and_still_publishes(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Learn restricted → act internal → the reply echoes the withheld
        entry verbatim → one audit record per implicated entry, message
        published unchanged."""
        agent = await _make_agent(
            _send_reply(f"Big news everyone: {_PROTECTED_SUMMARY}!"),
        )
        try:
            await agent._episodic_memory.store_episode(
                summary=_PROTECTED_SUMMARY,
                context={},
                importance=0.9,
                protection_level="restricted",
                source_channel_id="group:leadership",
            )
            event = _event("internal", "sunset next quarter")
            with caplog.at_level(logging.INFO):
                actions, publisher = await _run_turn(agent, event)
            sends = [
                a for a in actions
                if a.action_type is ActionType.SEND_CHANNEL_MESSAGE
            ]
            assert len(sends) == 1
            publisher.publish.assert_awaited_once()
            records = _audit_records(caplog)
            assert len(records) == 1
            rec = records[0]
            assert rec.channel_id == _CHANNEL  # type: ignore[attr-defined]
            assert rec.protection_level == "restricted"  # type: ignore[attr-defined]
            assert rec.acting_classification == "internal"  # type: ignore[attr-defined]
            # §G metadata-only wall, through the whole seam.
            assert "REDWOLF" not in repr(rec.__dict__)
        finally:
            await agent.close_memory()

    async def test_benign_reply_is_silent(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        agent = await _make_agent(
            _send_reply("Nothing new to report — retro is Thursday as planned."),
        )
        try:
            await agent._episodic_memory.store_episode(
                summary=_PROTECTED_SUMMARY,
                context={},
                importance=0.9,
                protection_level="restricted",
                source_channel_id="group:leadership",
            )
            event = _event("internal", "sunset next quarter")
            with caplog.at_level(logging.INFO):
                _, publisher = await _run_turn(agent, event)
            publisher.publish.assert_awaited_once()
            assert _audit_records(caplog) == []
        finally:
            await agent.close_memory()

    async def test_nothing_withheld_stamps_no_watch(self) -> None:
        """The common case (every entry at/below the acting level) adds
        zero bytes to the event and no watch to the context."""
        agent = await _make_agent(_send_reply("All quiet on the sunset front."))
        try:
            await agent._episodic_memory.store_episode(
                summary=_PROTECTED_SUMMARY,
                context={},
                importance=0.9,
                protection_level="internal",
                source_channel_id="group:planning",
            )
            event = _event("internal", "sunset next quarter")
            async with agent._lock:
                await agent._on_event_inner(event)
            assert TRIPWIRE_WATCH_METADATA_KEY not in event.metadata
            context = DispatchContext.for_event(event, cascade_depth=1)
            assert context.origin_tripwire_watch is None
        finally:
            await agent.close_memory()
