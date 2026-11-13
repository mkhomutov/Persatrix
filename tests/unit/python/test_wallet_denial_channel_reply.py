"""A wallet denial is answered on a DM and nowhere else (ISSUE-0239).

When the wallet refuses a persona's model call, the agent answers the
person who wrote with an error reply, so their chat request ends with
``reply_status: error`` and not a time-out (ISSUE-0065, ISSUE-0066).  A
chat request always travels on a DM.  The agent used to send that reply on
every channel: anywhere but a DM the room got a line such as
``per_agent budget exceeded: spent=10.000000, limit=10.000000,
estimated=0.010000`` as an ordinary message from the persona, mentioning
whoever had written.

Each denial below is driven through a real persona whose model call
raises it, and through the real executor to a publisher that records what
would be posted.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock

import grpc
import grpc.aio
import pytest

from agents.action_executor import ActionExecutor
from agents.chat_reply import process_inbound_channel_event
from agents.llm_client import LLMClient
from agents.persona import create_persona_agent
from agents.persona_types import AgentEvent, EventType
from agents.wallet_client import BudgetExceededError

from ._persona_test_helpers import _PERSONA_CONFIG

AGENT = "ember-owl"
DM = f"dm:{AGENT}:user-1"
#: Every kind of channel that is not a DM, and an id with no kind at all.
NOT_A_DM = ["group:planning", "thread:group:planning:m-1", "general"]

#: The wallet's own wording for each refusal.
_SPENT = "per_agent budget exceeded: spent=10.000000, limit=10.000000, estimated=0.010000"
_CEILING = (
    'interaction "itx-9" cost ceiling exceeded: '
    "900 spent + 200 estimated > 1000 budget tokens"
)
_UNREACHABLE = "wallet unreachable: the model call fails closed"
_LEASES = "agent already holds the maximum 3 active leases"
_AT_CAPACITY = "Agent is at capacity — please retry in a moment."
#: Where the agent logs these.  The documents name it, and anywhere but a
#: DM the log line is the only record of the denial.
_LOGGER = "Persatrix.agent.server"


class Denial(NamedTuple):
    raised: Callable[[], BaseException]
    reply: str
    reason: str
    logged_as: str
    #: What the log line must say besides: the cause an operator needs.
    logged: tuple[str, ...]


DENIALS: dict[str, Denial] = {
    "the budget is spent": Denial(
        lambda: BudgetExceededError(
            _SPENT, scope="per_agent", spent_usd=10.0, limit_usd=10.0,
            estimated_usd=0.01, reason="budget_exceeded",
        ),
        _SPENT, "budget_exceeded", "budget-denied",
        ("scope=per_agent", "reason=budget_exceeded", _SPENT),
    ),
    # The cost ceiling of one discussion, set per channel.  The only
    # refusal with a step of its own before it reaches the caller: the
    # persona closes and summarises the discussion first.
    "the discussion's cost ceiling is reached": Denial(
        lambda: BudgetExceededError(
            _CEILING, scope="per_interaction", reason="interaction_budget_exhausted",
        ),
        _CEILING, "interaction_budget_exhausted", "budget-denied",
        ("scope=per_interaction", "reason=interaction_budget_exhausted", _CEILING),
    ),
    "the wallet cannot be reached": Denial(
        lambda: BudgetExceededError(_UNREACHABLE, reason="wallet_unreachable"),
        _UNREACHABLE, "wallet_unreachable", "budget-denied",
        ("reason=wallet_unreachable", _UNREACHABLE),
    ),
    "the agent is at its lease or rate limit": Denial(
        lambda: grpc.aio.AioRpcError(
            grpc.StatusCode.RESOURCE_EXHAUSTED,
            grpc.aio.Metadata(), grpc.aio.Metadata(),
            details=_LEASES,
        ),
        _AT_CAPACITY, "resource_exhausted", "resource-exhausted",
        (_LEASES,),
    ),
}

each_denial = pytest.mark.parametrize("name", DENIALS)
each_other_channel = pytest.mark.parametrize("channel_id", NOT_A_DM)


def _client(raised: BaseException) -> LLMClient:
    provider = AsyncMock()
    provider.create_message = AsyncMock(side_effect=raised)
    provider.format_tool_definitions = MagicMock(return_value=[])
    return LLMClient(provider)


class _Publisher:
    """Records what the agent would post to the orchestrator."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def publish(self, **message: Any) -> None:
        self.sent.append(message)


async def _channel_turn(
    raised: BaseException, channel_id: str, *, payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run one inbound channel message to its end; return what was posted."""
    agent = create_persona_agent(
        agent_id=AGENT, config=copy.deepcopy(_PERSONA_CONFIG),
        llm_client=_client(raised),
    )
    await agent.initialize_memory()
    publisher = _Publisher()
    event = AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": "hello", "respond_policy": "always", **(payload or {})},
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


# ─── A channel that is not a DM ──────────────────────────────────────


@each_other_channel
@each_denial
async def test_a_channel_that_is_not_a_dm_gets_no_message(
    name: str, channel_id: str,
) -> None:
    """Nothing about the denial enters the channel record.

    The price of silence is paid in a floor-control round, where a
    speaker that posts nothing holds the floor until its turn times out.
    """
    assert await _channel_turn(DENIALS[name].raised(), channel_id) == []


@each_other_channel
@each_denial
async def test_a_denial_off_a_dm_is_still_logged_with_its_cause(
    name: str, channel_id: str, caplog: pytest.LogCaptureFixture,
) -> None:
    """The operator still learns why the persona went quiet, and which
    of the refusals it was: a spent budget and an unreachable wallet
    share a line's first words, and only its reason tells them apart."""
    denial = DENIALS[name]
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await _channel_turn(denial.raised(), channel_id)
    logged = [
        r for r in caplog.records
        if r.getMessage().startswith(f"ReceiveChannelMessage {denial.logged_as}")
    ]
    assert len(logged) == 1
    assert logged[0].levelno == logging.WARNING
    assert logged[0].name == _LOGGER
    line = logged[0].getMessage()
    assert AGENT in line
    assert channel_id in line
    for cause in denial.logged:
        assert cause in line


# ─── A DM ────────────────────────────────────────────────────────────


@each_denial
async def test_a_dm_keeps_its_error_reply(name: str) -> None:
    """The reply the chat handler turns into HTTP 200 with
    ``reply_status: error``; without it the person waits for a 504."""
    denial = DENIALS[name]
    (reply,) = await _channel_turn(denial.raised(), DM)
    assert reply["channel_id"] == DM
    assert reply["sender_id"] == AGENT
    assert reply["content"] == denial.reply
    assert reply["mentions"] == ["user-1"]
    assert reply["cascade_depth"] == 0
    assert reply["metadata"]["reply_status"] == "error"
    assert reply["metadata"]["error_reason"] == denial.reason


@each_denial
async def test_a_synthesis_turn_is_still_never_answered(name: str) -> None:
    """The one turn that was already held back, on any channel: its reply
    is what closes a discussion, and an error reply would be taken for it."""
    for channel_id in (DM, *NOT_A_DM):
        sent = await _channel_turn(
            DENIALS[name].raised(), channel_id, payload={"synthesis_turn": True},
        )
        assert sent == [], channel_id


async def test_a_synthesis_turn_that_fails_is_not_answered_either() -> None:
    """The same holds for a persona's own failed turn.  An error reply
    under the chair's name would be as easy to take for the synthesis as
    a wallet's, so one rule keeps both back."""
    for channel_id in (DM, *NOT_A_DM):
        sent = await _channel_turn(
            RuntimeError("the provider call raised"), channel_id,
            payload={"synthesis_turn": True},
        )
        assert sent == [], channel_id


# ─── The two kinds of failure agree ──────────────────────────────────


@pytest.mark.parametrize("channel_id", [DM, *NOT_A_DM])
@each_denial
async def test_a_denial_is_answered_where_a_failed_turn_is(
    name: str, channel_id: str,
) -> None:
    """A wallet denial and a persona's own failed turn (ISSUE-0199) are
    answered on the same channels: one rule decides both."""
    denied = await _channel_turn(DENIALS[name].raised(), channel_id)
    failed = await _channel_turn(RuntimeError("the provider call raised"), channel_id)
    assert len(failed) == (1 if channel_id == DM else 0)
    assert len(denied) == len(failed)
    for reply in (*denied, *failed):
        assert reply["metadata"]["reply_status"] == "error"
