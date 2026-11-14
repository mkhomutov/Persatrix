"""A refused channel message says why (ISSUE-0205, second half).

A channel message for an agent that has no dispatch target on this
server (a task agent, say) used to be refused as ``receiver overloaded:
agent event-loop queue full``, which names the wrong cause: no queue was
ever involved.  It is refused under a text of its own, and the agent's
log has a warning for it.  The first half of the issue, the process that
stops when a persona's memory does not open, is in
``test_persona_without_memory_stops_the_agent.py``.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import grpc
import pytest

from agents.channel_receive import NO_DISPATCH_TARGET, RECEIVER_OVERLOADED
from agents.dispatch import EventDispatcher
from agents.persona_runtime import _LLMPersonaAgent
from agents.server_servicers import AgentServiceServicer
from agents.tick import TickScheduler
from agents.tools.registry import clear_registry

from ._receive_channel_message_helpers import StubAgent, channel_event

AGENT = "ember-owl"


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


@pytest.mark.asyncio
class TestRefusalNamesTheCause:
    @pytest.mark.parametrize(
        ("has_target", "enqueue_accepts", "want"),
        [
            (False, True, NO_DISPATCH_TARGET.format(agent_id=AGENT)),
            (True, False, RECEIVER_OVERLOADED),
            (True, True, ""),
        ],
        ids=["no dispatch target", "target with a full queue", "accepted"],
    )
    async def test_each_refusal_has_its_own_text(
        self, has_target: bool, enqueue_accepts: bool, want: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        agent = StubAgent(agent_id=AGENT, config={"model": "test"})
        dispatcher = MagicMock(spec=EventDispatcher)
        dispatcher.has_inbound_target = MagicMock(return_value=has_target)
        dispatcher.enqueue_inbound = MagicMock(return_value=enqueue_accepts)
        servicer = AgentServiceServicer({AGENT: agent}, dispatcher)

        with caplog.at_level(logging.WARNING, logger="Persatrix.agent.channel_receive"):
            ack = await servicer.ReceiveChannelMessage(
                channel_event(), MagicMock(spec=grpc.aio.ServicerContext),
            )

        assert ack.error_message == want
        assert ack.success is (want == "")
        assert dispatcher.enqueue_inbound.called is has_target
        # The handler's own warning is for the missing target and nothing else.
        own = [r for r in caplog.records if r.name == "Persatrix.agent.channel_receive"]
        assert len(own) == (0 if has_target else 1)

    @pytest.mark.parametrize(
        ("registered", "loop", "want"),
        [
            (False, None, False),
            (True, None, True),
            (False, "running", True),
            (False, "stopped", False),
            (True, "stopped", True),
        ],
        ids=[
            "nothing registered",
            "a registered agent",
            "a running event loop",
            "an event loop that is not running",
            "a registered agent whose loop is not running",
        ],
    )
    async def test_what_counts_as_a_dispatch_target(
        self, registered: bool, loop: str | None, want: bool,
    ) -> None:
        """The dispatcher's own rule: the two places an inbound event can go."""
        dispatcher = EventDispatcher()
        if registered:
            dispatcher.register_agent(AGENT, MagicMock(spec=_LLMPersonaAgent))
        if loop is not None:
            scheduler = MagicMock(spec=TickScheduler)
            scheduler.is_running = loop == "running"
            dispatcher.register_tick_scheduler(AGENT, scheduler)

        assert dispatcher.has_inbound_target(AGENT) is want
        assert dispatcher.has_inbound_target("someone-else") is False

    async def test_a_task_agent_is_not_an_overloaded_receiver(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The real dispatcher, hosting nothing: what a task agent's server has."""
        agent = StubAgent(agent_id="planner", config={"model": "test"})
        servicer = AgentServiceServicer({"planner": agent}, EventDispatcher())

        with caplog.at_level(logging.WARNING):
            ack = await servicer.ReceiveChannelMessage(
                channel_event(channel_id="group:general"),
                MagicMock(spec=grpc.aio.ServicerContext),
            )

        assert ack.success is False
        assert ack.error_message == NO_DISPATCH_TARGET.format(agent_id="planner")
        assert "planner" in ack.error_message
        # The agent's own log says it too: the refusal is otherwise only in
        # the orchestrator's log, and the dispatcher's warning for a missing
        # target is no longer reached.
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert [(r.name, r.getMessage()) for r in warnings] == [(
            "Persatrix.agent.channel_receive",
            "Refused a channel message on group:general for planner: "
            "the agent has no event dispatch target on this server",
        )]
        assert "overloaded" not in ack.error_message
        assert "queue" not in ack.error_message


def test_the_two_texts_cannot_be_mistaken_for_each_other() -> None:
    assert "overloaded" in RECEIVER_OVERLOADED
    assert "overloaded" not in NO_DISPATCH_TARGET
    assert "{agent_id}" in NO_DISPATCH_TARGET
