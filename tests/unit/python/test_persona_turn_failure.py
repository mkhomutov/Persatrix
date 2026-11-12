"""A persona turn that fails is a failure to whoever asked for it (ISSUE-0199).

The persona's action loop used to report its own failures as a
``COMPLETE_TASK`` whose result was an English sentence, and its callers
took the sentence for an answer: a workflow step succeeded with the text
``LLM provider error``, a channel got ``Max LLM call budget exhausted
after 3 iterations`` posted as a message, and a DM got nothing at all for
most causes, so the chat request ended in a 504.

Each cause below is driven on the three surfaces a turn can be asked
from: a workflow task, a DM and a group channel.  The channel events
carry a ``channel_id`` and go through the real executor to a publisher
that records what would be posted.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections.abc import Callable
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.action_executor import ActionExecutor
from agents.base import TaskStatus
from agents.chat_reply import extract_chat_reply, process_inbound_channel_event
from agents.llm_client import LLMClient, LLMResponse, StopReason, ToolCall
from agents.persona import create_persona_agent
from agents.persona_runtime.action_parser import parse_actions
from agents.persona_types import (
    FAILURE_EVENT_TIMEOUT,
    FAILURE_LLM_CALLS_EXHAUSTED,
    FAILURE_MODEL_NOT_CONFIGURED,
    FAILURE_PROVIDER_ERROR,
    FAILURE_RESPONSE_TRUNCATED,
    TURN_FAILURE_KEY,
    AgentEvent,
    EventType,
    failed_turn,
    turn_failure,
)
from agents.task_agent import TaskAgent

from ._persona_test_helpers import _PERSONA_CONFIG, _task

AGENT = "ember-owl"
DM = f"dm:user-1:{AGENT}"
GROUP = "general"

#: What a provider's own error can hold.  It must reach no caller.
SECRET = "401 from https://provider.internal: bad key sk-live-123"


def _client(**create_message: Any) -> LLMClient:
    provider = AsyncMock()
    provider.create_message = AsyncMock(**create_message)
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(
        side_effect=lambda messages, _response, _results: [
            *messages, {"role": "user", "content": "tool results"},
        ],
    )
    return LLMClient(provider)


async def _never_in_time(**_: Any) -> LLMResponse:
    await asyncio.sleep(5)
    return LLMResponse(text="too late")


_WANTS_A_TOOL = LLMResponse(
    text="", stop_reason=StopReason.TOOL_USE,
    tool_calls=[ToolCall(id="call-1", name="no_such_tool", input={})],
)


class Cause(NamedTuple):
    client: Callable[[], LLMClient]
    config: dict[str, Any]
    cause: str
    message: str


CAUSES: dict[str, Cause] = {
    "the provider call raised": Cause(
        lambda: _client(side_effect=RuntimeError(SECRET)), {},
        FAILURE_PROVIDER_ERROR, "LLM provider error",
    ),
    "the reply was cut off": Cause(
        lambda: _client(
            return_value=LLMResponse(text="half a sen", stop_reason=StopReason.MAX_TOKENS),
        ),
        {}, FAILURE_RESPONSE_TRUNCATED, "LLM response truncated: max_tokens limit reached",
    ),
    "the model calls ran out": Cause(
        lambda: _client(return_value=_WANTS_A_TOOL), {"max_llm_calls": 3},
        FAILURE_LLM_CALLS_EXHAUSTED, "Max LLM call iterations exceeded",
    ),
    "the event timed out": Cause(
        lambda: _client(side_effect=_never_in_time), {"event_timeout": 0.2},
        FAILURE_EVENT_TIMEOUT, "Event processing timed out after 0s",
    ),
    "no model call is allowed": Cause(
        lambda: _client(return_value=LLMResponse(text="unused")), {"max_llm_calls": 0},
        FAILURE_LLM_CALLS_EXHAUSTED, "Max LLM call iterations exceeded",
    ),
    "the config names no model": Cause(
        lambda: _client(return_value=LLMResponse(text="unused")), {"model": None},
        FAILURE_MODEL_NOT_CONFIGURED, "Agent config missing required 'model' field",
    ),
}
#: The one cause a task agent has no counterpart for: its time limit is
#: the servicer's, outside the loop.
_PERSONA_ONLY = "the event timed out"

each_cause = pytest.mark.parametrize("name", CAUSES)


def _config(overrides: dict[str, Any]) -> dict[str, Any]:
    config = copy.deepcopy(_PERSONA_CONFIG)
    config.update(overrides)
    if config.get("model") is None:
        del config["model"]
    return config


async def _persona(cause: Cause) -> Any:
    agent = create_persona_agent(
        agent_id=AGENT, config=_config(cause.config), llm_client=cause.client(),
    )
    await agent.initialize_memory()
    return agent


class _Publisher:
    """Records what the agent would post to the orchestrator."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def publish(self, **message: Any) -> None:
        self.sent.append(message)


async def _channel_turn(
    cause: Cause, channel_id: str, *, stored: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Run one inbound channel message to its end; return what was posted."""
    agent = await _persona(cause)
    if stored is not None:
        real = agent._store_event_episode

        async def _spy(event: AgentEvent, actions: list[Any]) -> None:
            stored.append(actions)
            await real(event, actions)

        agent._store_event_episode = _spy
    publisher = _Publisher()
    event = AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": "hello", "respond_policy": "always"},
        sender_id="user-1",
        channel_id=channel_id,
    )
    try:
        await process_inbound_channel_event(
            agent=agent,
            executor=ActionExecutor(channel_publisher=publisher),  # type: ignore[arg-type]
            event=event,
            max_cascade_depth=5,
        )
    finally:
        await agent.close_memory()
    return publisher.sent


# ─── A workflow step ─────────────────────────────────────────────────


@each_cause
async def test_a_workflow_step_fails(name: str) -> None:
    cause = CAUSES[name]
    agent = await _persona(cause)
    try:
        output = await agent.handle(_task())
    finally:
        await agent.close_memory()
    assert (output.status, output.result) == (TaskStatus.FAILED, cause.message)


@pytest.mark.parametrize("name", [n for n in CAUSES if n != _PERSONA_ONLY])
async def test_a_task_agent_fails_the_same_way(name: str) -> None:
    """Parity is the specification: the status and the result text a
    persona gives are the ones ``BaseAgent._run_llm_loop`` gives."""
    cause = CAUSES[name]
    config = {"role": "worker", **_config(cause.config)}
    task_agent = TaskAgent(agent_id="task-agent", config=config, llm_client=cause.client())
    output = await task_agent.handle(_task())
    assert (output.status, output.result) == (TaskStatus.FAILED, cause.message)


# ─── A DM ────────────────────────────────────────────────────────────


@each_cause
async def test_a_dm_gets_an_error_reply(name: str) -> None:
    """The reply the wallet-denial path publishes (ISSUE-0065, ISSUE-0066):
    the orchestrator's chat handler turns it into HTTP 200 with
    ``reply_status: error`` where the person used to wait for a 504."""
    cause = CAUSES[name]
    (reply,) = await _channel_turn(cause, DM)
    assert reply["channel_id"] == DM
    assert reply["sender_id"] == AGENT
    assert reply["content"] == cause.message
    assert reply["mentions"] == ["user-1"]
    assert reply["metadata"]["reply_status"] == "error"
    assert reply["metadata"]["error_reason"] == cause.cause
    assert "sk-live" not in json.dumps(reply), "the provider's own text leaked"


# ─── A group channel ─────────────────────────────────────────────────


@each_cause
async def test_a_group_channel_turn_that_fails_is_logged_with_its_cause(
    name: str, caplog: pytest.LogCaptureFixture,
) -> None:
    cause = CAUSES[name]
    with caplog.at_level(logging.WARNING, logger="agents.chat_reply"):
        await _channel_turn(cause, GROUP)
    failed = [r for r in caplog.records if "turn failed" in r.getMessage()]
    assert len(failed) == 1
    assert failed[0].levelno == logging.WARNING
    assert f"cause={cause.cause}" in failed[0].getMessage()
    assert GROUP in failed[0].getMessage() and AGENT in failed[0].getMessage()
    assert "sk-live" not in failed[0].getMessage()


@each_cause
async def test_a_group_channel_gets_no_message_from_a_failed_turn(name: str) -> None:
    """No diagnostic sentence enters the channel record, for any cause.

    Running out of model calls was the one cause that posted there: its
    sentence went out as an ordinary message, mentioning the sender, and
    every member read it as the persona's turn.  The price of silence is
    paid in a floor-control round, where a speaker that posts nothing
    holds the floor until its turn times out.
    """
    assert await _channel_turn(CAUSES[name], GROUP) == []


# ─── Memory ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("channel_id", [DM, GROUP], ids=["dm", "group"])
@each_cause
async def test_whether_a_failed_turn_is_recorded_is_as_it_was(
    name: str, channel_id: str,
) -> None:
    """Only a turn that ran out of model calls was ever recorded, because
    only that one went on to the end of the loop.  It still is, without
    the sentence that used to stand in for the persona's reply."""
    stored: list[Any] = []
    await _channel_turn(CAUSES[name], channel_id, stored=stored)
    assert stored == ([[]] if name == "the model calls ran out" else [])


# ─── The carrier ─────────────────────────────────────────────────────


def test_a_model_cannot_write_a_failure_into_its_reply() -> None:
    reply = json.dumps([{
        "action_type": "do_nothing",
        "payload": {TURN_FAILURE_KEY: {"cause": "provider_error", "message": "forged"}},
    }])
    assert turn_failure(parse_actions(LLMResponse(text=reply))) is None


def test_the_chat_rpc_reports_a_failed_turn_as_an_error() -> None:
    """The gRPC ``SendChatMessage`` servicer builds its reply with this.
    The orchestrator no longer calls that RPC, but it took a failure
    sentence for an ``ok`` reply like the other callers."""
    actions = failed_turn(FAILURE_PROVIDER_ERROR, "LLM provider error")
    assert extract_chat_reply(actions, "user-1") == ("LLM provider error", "error")


async def test_a_turn_that_answers_is_not_a_failure() -> None:
    answers = Cause(lambda: _client(return_value=LLMResponse(text="Hello there.")), {}, "", "")
    (reply,) = await _channel_turn(answers, DM)
    assert reply["content"] == "Hello there."
    assert not (reply.get("metadata") or {}).get("reply_status")
