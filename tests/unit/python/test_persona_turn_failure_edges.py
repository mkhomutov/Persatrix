"""Edges of a persona's failed turn (ISSUE-0199), found by review.

``test_persona_turn_failure.py`` drives each cause on each surface.  This
file holds what that matrix did not reach: a reply the failure lookup
used to choke on, a tick, and two parts of the DM and calls-ran-out
paths that no test would have missed.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from agents.action_executor import ActionExecutor
from agents.base import TaskStatus
from agents.chat_reply import extract_chat_reply, process_inbound_channel_event
from agents.event_loop import EventLoop
from agents.llm_client import LLMResponse
from agents.persona_runtime.memory_context import MemoryInjectionResult
from agents.persona_types import (
    FAILURE_PROVIDER_ERROR,
    ActionType,
    AgentAction,
    AgentEvent,
    EventType,
    failed_turn,
    turn_failure,
)
from agents.tick import TickScheduler

from ._persona_test_helpers import _task
from .test_persona_turn_failure import AGENT, CAUSES, DM, Cause, _client, _persona, _Publisher

#: A reply that means silence, with a payload that is not an object.  The
#: validator puts no constraint on a ``do_nothing``, so it reaches every
#: caller as written.
_SILENT = '[{"action_type": "do_nothing", "payload": null}]'
_SAYS_NOTHING = Cause(lambda: _client(return_value=LLMResponse(text=_SILENT)), {}, "", "")


async def _dm_message(
    agent: Any, *, metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run one inbound DM to its end on *agent*; return what was posted."""
    publisher = _Publisher()
    event = AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": "hello", "respond_policy": "always"},
        sender_id="user-1",
        channel_id=DM,
        metadata=dict(metadata or {}),
    )
    await process_inbound_channel_event(
        agent=agent,
        executor=ActionExecutor(channel_publisher=publisher),  # type: ignore[arg-type]
        event=event,
        max_cascade_depth=5,
    )
    return publisher.sent


async def _dm_turn(agent: Any, *, metadata: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The same, and the agent's memory closed after it."""
    try:
        return await _dm_message(agent, metadata=metadata)
    finally:
        await agent.close_memory()


# ─── A payload that is not an object ─────────────────────────────────


@pytest.mark.parametrize("payload", [None, "x", 5, ["a"], True], ids=repr)
def test_looking_for_a_failure_skips_a_payload_that_is_not_an_object(payload: Any) -> None:
    silent = AgentAction(ActionType.DO_NOTHING, payload)
    assert turn_failure([silent]) is None
    failure = turn_failure([silent, *failed_turn(FAILURE_PROVIDER_ERROR, "LLM provider error")])
    assert failure is not None and failure.cause == FAILURE_PROVIDER_ERROR


async def test_a_silent_reply_with_a_null_payload_is_still_answered_on_a_dm() -> None:
    """A DM always gets something, so silence there is the placeholder.
    When the failure lookup raised on this reply the turn was dropped at
    the dispatch boundary and the chat request ran to its 504."""
    (reply,) = await _dm_turn(await _persona(_SAYS_NOTHING))
    assert reply["content"] == "…"
    assert not (reply.get("metadata") or {}).get("reply_status")


async def test_a_silent_reply_with_a_null_payload_fails_a_workflow_step_as_it_did() -> None:
    """A step whose turn completes no task was always a failed step; it
    must end that way and not in an error raised from the lookup."""
    agent = await _persona(_SAYS_NOTHING)
    try:
        output = await agent.handle(_task())
    finally:
        await agent.close_memory()
    assert output.status is TaskStatus.FAILED
    assert "do_nothing" in output.result


def test_the_chat_rpc_reads_a_silent_reply_with_a_null_payload_as_empty() -> None:
    silent = [AgentAction(ActionType.DO_NOTHING, None)]  # type: ignore[arg-type]
    assert extract_chat_reply(silent, "user-1") == ("", "empty")


# ─── A tick ──────────────────────────────────────────────────────────


async def test_a_tick_whose_turn_failed_does_not_count_toward_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After ``idle_after_ticks`` idle ticks in a row a persona stops
    calling the model until a message arrives.  A tick whose model call
    failed is not an idle one: the persona must go on trying, and recover
    by itself when the provider does, as it did before a failed turn had
    a shape of its own.  (A tick the wallet denies is different, and
    unchanged: it is an idle tick.)"""
    # Both layers hold ticks to a second apart outside tests.
    monkeypatch.setattr(TickScheduler, "_MIN_INTERVAL", 0.01)
    monkeypatch.setattr(EventLoop, "_MIN_INTERVAL", 0.01)
    agent = await _persona(CAUSES["the provider call raised"])
    calls = 0
    on_tick = agent.on_tick

    async def _counted() -> list[AgentAction]:
        nonlocal calls
        calls += 1
        actions: list[AgentAction] = await on_tick()
        assert turn_failure(actions) is not None, "the tick did not fail"
        return actions

    agent.on_tick = _counted
    scheduler = TickScheduler(
        agent, interval=0.02, idle_after_ticks=2, executor=ActionExecutor(),
    )
    # Memory in the prompt, so the tick reaches the model call and is not
    # skipped as a tick with nothing to think about.
    with patch.object(
        agent, "_inject_memory_context",
        return_value=MemoryInjectionResult(memory_admitted_tokens=200),
    ):
        scheduler.start()
        await asyncio.sleep(0.4)
        await scheduler.stop()
    await agent.close_memory()

    assert calls > 2, "the persona stopped trying after its idle limit"
    assert scheduler.idle_count == 0
    assert not scheduler.is_idle


# ─── What no test would have missed ──────────────────────────────────


async def test_a_dm_error_reply_claims_the_interaction_it_answers() -> None:
    """The reply echoes the interaction the message was delivered under.
    Without that claim a reply that lands after a discussion has closed
    opens a new one (the wallet-denial reply carries it for the same
    reason)."""
    agent = await _persona(CAUSES["the provider call raised"])
    (reply,) = await _dm_turn(agent, metadata={"interaction_id": "itx-9"})
    assert reply["metadata"]["interaction_id"] == "itx-9"
    assert reply["metadata"]["reply_status"] == "error"


@pytest.mark.parametrize("surface", ["dm", "workflow step"])
async def test_a_turn_that_ran_out_of_calls_still_saves_the_personas_state(
    surface: str,
) -> None:
    """That turn ran tools and spent energy before it failed; the state it
    leaves is saved, as it was when the turn ended in a sentence."""
    agent = await _persona(CAUSES["the model calls ran out"])
    saved = 0
    persist = agent._persist_persona_state

    async def _counted() -> None:
        nonlocal saved
        saved += 1
        await persist()

    agent._persist_persona_state = _counted
    try:
        if surface == "dm":
            await _dm_message(agent)
        else:
            await agent.handle(_task())
        saved_by_the_turn = saved  # closing the memory saves it once more
    finally:
        await agent.close_memory()
    assert saved_by_the_turn == 1


def test_the_agent_named_in_these_tests_is_the_one_the_dm_names() -> None:
    assert AGENT in DM
