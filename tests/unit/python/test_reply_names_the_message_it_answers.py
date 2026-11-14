"""A reply names the message it answers (ISSUE-0200).

A chat request publishes the person's message into a DM and waits for the
persona's reply.  The orchestrator matched the two by channel and sender
only, so after one request timed out the persona's late answer to it was
returned to the next request on that DM.

Every message an agent publishes in reply now carries ``in_reply_to``: the
id of the message the agent was sent, which the orchestrator already puts
on the event.  The orchestrator accepts a reply for a waiting request only
when that id is the request's own (``internal/channels/waiter.go``).  So
the echo has to be on every kind of reply a request can be waiting for: an
ordinary answer, an end-of-interaction vote, and the error reply a DM gets
when the turn failed or the wallet refused the model call.  A message the
agent publishes on its own, such as one from a timer, answers nothing and
carries no such key.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import grpc
import pytest

from agents.action_executor import ActionExecutor
from agents.chat_reply import process_inbound_channel_event
from agents.dispatch_context import DispatchContext
from agents.event_loop import ScheduledWake
from agents.llm_client import LLMClient, LLMResponse
from agents.persona import create_persona_agent
from agents.persona_types import ActionType, AgentAction, AgentEvent, EventType
from agents.tick import TickScheduler
from agents.wallet_client import BudgetExceededError

from ._persona_test_helpers import _PERSONA_CONFIG

AGENT = "ember-owl"
DM = f"dm:alice:{AGENT}"
GROUP = "group:planning"
QUESTION_ID = "0b5c7a0e-question-2"


def _event(
    channel_id: str = DM, *, message_id: str | None = QUESTION_ID, **metadata: Any,
) -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": "Q2: what is 2+2?", "respond_policy": "always"},
        sender_id="alice", channel_id=channel_id, message_id=message_id,
        metadata=dict(metadata),
    )


class _Publisher:
    """Records what the agent would post to the orchestrator."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def publish(self, **message: Any) -> None:
        self.sent.append(message)


def _metadata(publisher: _Publisher) -> list[dict[str, Any]]:
    return [dict(m.get("metadata") or {}) for m in publisher.sent]


# ─── The rule, where every publish takes it from ─────────────


@pytest.mark.parametrize(
    ("event", "target", "want"),
    [
        (_event(DM), DM, {"in_reply_to": QUESTION_ID}),
        (
            _event(DM, interaction_id="itx-1"), DM,
            {"in_reply_to": QUESTION_ID, "interaction_id": "itx-1"},
        ),
        (_event(GROUP, interaction_id="itx-1"), "group:elsewhere", None),
        (_event(DM), "dm:bob:ember-owl", None),
        (_event(DM, message_id=None, interaction_id="itx-1"), DM, {"interaction_id": "itx-1"}),
        (_event(DM, message_id=""), DM, None),
    ],
    ids=[
        "a reply on the channel the message came on",
        "the same, in a tracked interaction",
        "a message to another channel, tracked",
        "a message to another DM",
        "an event that carries no message id",
        "an event with an empty message id and no interaction",
    ],
)
def test_a_same_channel_publish_names_the_message_it_answers(
    event: AgentEvent, target: str, want: dict[str, Any] | None,
) -> None:
    context = DispatchContext.for_event(event, cascade_depth=1)

    assert context.same_channel_claim(target) == want


def test_a_synthesis_reply_names_it_too() -> None:
    event = _event(GROUP, interaction_id="itx-1")
    event.payload["synthesis_turn"] = True
    context = DispatchContext.for_event(event, cascade_depth=1)

    assert context.same_channel_claim(GROUP) == {
        "in_reply_to": QUESTION_ID, "interaction_id": "itx-1", "synthesis_reply": True,
    }
    assert context.same_channel_claim(GROUP, claim_synthesis_reply=False) == {
        "in_reply_to": QUESTION_ID, "interaction_id": "itx-1",
    }


def test_a_context_with_no_event_behind_it_names_nothing() -> None:
    """A timer's context: the agent speaks first, in reply to nothing."""
    context = DispatchContext(cascade_depth=1)

    assert context.origin_message_id == ""
    assert context.same_channel_claim(DM) is None
    assert context.same_channel_claim("") is None


# ─── Every kind of reply, through the real executor ──────────


@pytest.mark.asyncio
class TestWhatTheAgentPublishes:
    async def test_an_answer_names_the_question(self) -> None:
        publisher = _Publisher()
        executor = ActionExecutor(channel_publisher=publisher)  # type: ignore[arg-type]
        answer = AgentAction(
            ActionType.SEND_CHANNEL_MESSAGE, {"channel_id": DM, "content": "A2: 4"},
        )

        await executor.execute(
            AGENT, [answer], context=DispatchContext.for_event(_event(DM), cascade_depth=1),
        )

        assert [m["content"] for m in publisher.sent] == ["A2: 4"]
        assert _metadata(publisher) == [{"in_reply_to": QUESTION_ID}]

    async def test_a_message_to_another_channel_names_nothing(self) -> None:
        publisher = _Publisher()
        executor = ActionExecutor(channel_publisher=publisher)  # type: ignore[arg-type]
        aside = AgentAction(
            ActionType.SEND_CHANNEL_MESSAGE, {"channel_id": GROUP, "content": "fyi"},
        )

        await executor.execute(
            AGENT, [aside], context=DispatchContext.for_event(_event(DM), cascade_depth=1),
        )

        assert _metadata(publisher) == [{}]

    async def test_a_message_from_a_timer_names_nothing(self) -> None:
        """Through the real tick path: what a persona posts when a timer
        wakes it answers no message, so it names none.  The tick builds its
        own context; if it ever took one from an event, this fails."""
        publisher = _Publisher()
        agent = MagicMock()
        agent.agent_id = AGENT
        agent.on_tick = AsyncMock(return_value=[AgentAction(
            ActionType.SEND_CHANNEL_MESSAGE, {"channel_id": DM, "content": "by the way"},
        )])
        scheduler = TickScheduler(
            agent, interval=60.0, idle_after_ticks=10, register_legacy_timer=False,
            executor=ActionExecutor(channel_publisher=publisher),  # type: ignore[arg-type]
        )

        await scheduler._handle_scheduled_wake(
            ScheduledWake(timer_id="legacy_tick", callback_kind="tick"),
        )

        agent.on_tick.assert_awaited_once()
        assert [m["content"] for m in publisher.sent] == ["by the way"]
        assert _metadata(publisher) == [{}]

    async def test_an_end_vote_names_the_message_it_votes_on(self) -> None:
        publisher = _Publisher()
        executor = ActionExecutor(channel_publisher=publisher)  # type: ignore[arg-type]
        vote = AgentAction(
            ActionType.END_INTERACTION_VOTE, {"channel_id": GROUP, "content": "we are done"},
        )
        event = _event(GROUP, interaction_id="itx-1")

        await executor.execute(
            AGENT, [vote], context=DispatchContext.for_event(event, cascade_depth=1),
        )

        assert len(publisher.sent) == 1
        assert _metadata(publisher)[0]["in_reply_to"] == QUESTION_ID
        assert _metadata(publisher)[0]["interaction_id"] == "itx-1"


def _persona(client: LLMClient, **overrides: Any) -> Any:
    config = copy.deepcopy(_PERSONA_CONFIG)
    config.update(overrides)
    return create_persona_agent(agent_id=AGENT, config=config, llm_client=client)


def _client(**create_message: Any) -> LLMClient:
    provider = AsyncMock()
    provider.create_message = AsyncMock(**create_message)
    provider.format_tool_definitions = MagicMock(return_value=[])
    return LLMClient(provider)


@pytest.mark.asyncio
class TestTheErrorReplyOnADM:
    """ISSUE-0244's second half: an error reply is matched like any reply.

    One that arrives in time must answer its own request, so it has to
    carry the echo; one that arrives late then cannot answer the next.
    """

    @pytest.mark.parametrize(
        ("client", "reason"),
        [
            (lambda: _client(side_effect=RuntimeError("provider is down")), "provider_error"),
            (
                lambda: _client(side_effect=BudgetExceededError(
                    "per_agent budget exceeded", scope="per_agent", reason="budget_exceeded",
                )),
                "budget_exceeded",
            ),
            (
                lambda: _client(side_effect=grpc.aio.AioRpcError(
                    grpc.StatusCode.RESOURCE_EXHAUSTED,
                    grpc.aio.Metadata(), grpc.aio.Metadata(),
                    details="agent already holds the maximum 3 active leases",
                )),
                "resource_exhausted",
            ),
        ],
        ids=[
            "the turn failed",
            "the wallet refused the model call",
            "the agent is at its lease limit",
        ],
    )
    async def test_it_names_the_message_it_could_not_answer(
        self, client: Any, reason: str,
    ) -> None:
        agent = _persona(client())
        await agent.initialize_memory()
        publisher = _Publisher()
        try:
            await process_inbound_channel_event(
                agent=agent,
                executor=ActionExecutor(channel_publisher=publisher),  # type: ignore[arg-type]
                event=_event(DM, interaction_id="itx-1"),
                max_cascade_depth=5,
            )
        finally:
            await agent.close_memory()

        assert len(publisher.sent) == 1, publisher.sent
        metadata = _metadata(publisher)[0]
        assert metadata["reply_status"] == "error"
        assert metadata["error_reason"] == reason
        assert metadata["in_reply_to"] == QUESTION_ID
        assert metadata["interaction_id"] == "itx-1"


def test_the_answer_of_a_real_turn_names_the_question() -> None:
    """End to end on the agent side: a persona's own answer, as published."""
    import asyncio

    async def run() -> list[dict[str, Any]]:
        agent = _persona(_client(return_value=LLMResponse(text="A2: 4")))
        await agent.initialize_memory()
        publisher = _Publisher()
        try:
            await process_inbound_channel_event(
                agent=agent,
                executor=ActionExecutor(channel_publisher=publisher),  # type: ignore[arg-type]
                event=_event(DM),
                max_cascade_depth=5,
            )
        finally:
            await agent.close_memory()
        return publisher.sent

    sent = asyncio.run(run())

    assert [m["content"] for m in sent] == ["A2: 4"]
    assert sent[0]["channel_id"] == DM
    assert sent[0]["metadata"]["in_reply_to"] == QUESTION_ID
