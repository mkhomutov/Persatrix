"""
Tests for ChannelServiceServicer: SendMessage routing and Subscribe stub.

All tests use mocked EventDispatcher — no real dispatch calls.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import grpc
import grpc.aio

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.dispatch import EventDispatcher
from agents.generated import agent_message_pb2
from agents.persona_types import EventType
from agents.server import ChannelServiceServicer


# ─── Helpers ─────────────────────────────────────────────────


class _StubAgent(BaseAgent):
    """Minimal agent for channel servicer tests."""

    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="stub result")


def _agent_message(**kwargs) -> agent_message_pb2.AgentMessage:
    defaults = {
        "message_id": "msg-001",
        "channel_id": "general",
        "sender_id": "tester",
        "content": "Sarah, what is your current focus?",
    }
    defaults.update(kwargs)
    return agent_message_pb2.AgentMessage(**defaults)


def _channel_servicer(
    agents: dict | None = None,
) -> tuple[ChannelServiceServicer, AsyncMock]:
    """Return a servicer with a mocked EventDispatcher.dispatch."""
    agents = agents or {}
    dispatcher = MagicMock(spec=EventDispatcher)
    dispatcher.dispatch = AsyncMock(return_value=[])
    return ChannelServiceServicer(agents, dispatcher), dispatcher.dispatch


# ─── ChannelServiceServicer.SendMessage Tests ─────────────────


class TestChannelServiceServicerSendMessage:
    """Tests for ChannelServiceServicer.SendMessage (MT-PERSONA-002)."""

    async def test_explicit_mentions_dispatches_only_to_those_agents(self):
        """SendMessage with mentions routes exclusively to the listed agent IDs."""
        stub = _StubAgent(agent_id="ember-owl", config={"model": "test"})
        servicer, dispatch = _channel_servicer({"ember-owl": stub})
        context = MagicMock(spec=grpc.aio.ServicerContext)

        resp = await servicer.SendMessage(
            _agent_message(mentions=["ember-owl"]), context
        )
        await asyncio.sleep(0)  # let create_task execute

        assert resp.delivered is True
        assert resp.message_id == "msg-001"
        dispatch.assert_called_once()
        target_id, event = dispatch.call_args.args
        assert target_id == "ember-owl"

    async def test_no_mentions_broadcasts_to_all_agents(self):
        """SendMessage without mentions delivers to every agent on the server."""
        agents = {
            "agent-a": _StubAgent(agent_id="agent-a", config={"model": "test"}),
            "agent-b": _StubAgent(agent_id="agent-b", config={"model": "test"}),
        }
        servicer, dispatch = _channel_servicer(agents)
        context = MagicMock(spec=grpc.aio.ServicerContext)

        resp = await servicer.SendMessage(_agent_message(), context)
        await asyncio.sleep(0)

        assert resp.delivered is True
        assert dispatch.call_count == 2
        dispatched_targets = {call.args[0] for call in dispatch.call_args_list}
        assert dispatched_targets == {"agent-a", "agent-b"}

    async def test_no_agents_returns_not_delivered(self):
        """SendMessage with an empty agent registry returns delivered=False."""
        servicer, dispatch = _channel_servicer({})
        context = MagicMock(spec=grpc.aio.ServicerContext)

        resp = await servicer.SendMessage(_agent_message(), context)

        assert resp.delivered is False
        dispatch.assert_not_called()

    async def test_event_type_is_message_received(self):
        """Dispatched event must carry EventType.MESSAGE_RECEIVED."""
        stub = _StubAgent(agent_id="ember-owl", config={"model": "test"})
        servicer, dispatch = _channel_servicer({"ember-owl": stub})
        context = MagicMock(spec=grpc.aio.ServicerContext)

        await servicer.SendMessage(
            _agent_message(mentions=["ember-owl"]), context
        )
        await asyncio.sleep(0)

        _, event = dispatch.call_args.args
        assert event.event_type == EventType.MESSAGE_RECEIVED

    async def test_event_payload_matches_message_fields(self):
        """Event payload carries content and channel_id from the AgentMessage."""
        stub = _StubAgent(agent_id="ember-owl", config={"model": "test"})
        servicer, dispatch = _channel_servicer({"ember-owl": stub})
        context = MagicMock(spec=grpc.aio.ServicerContext)

        await servicer.SendMessage(
            _agent_message(
                content="what is your focus?",
                channel_id="engineering",
                sender_id="tester",
                message_id="msg-xyz",
                mentions=["ember-owl"],
            ),
            context,
        )
        await asyncio.sleep(0)

        _, event = dispatch.call_args.args
        assert event.payload["content"] == "what is your focus?"
        assert event.payload["channel_id"] == "engineering"
        assert event.sender_id == "tester"
        assert event.channel_id == "engineering"
        assert event.message_id == "msg-xyz"

    async def test_response_message_id_echoes_request(self):
        """SendMessageResponse.message_id must equal the request message_id."""
        stub = _StubAgent(agent_id="ember-owl", config={"model": "test"})
        servicer, _ = _channel_servicer({"ember-owl": stub})
        context = MagicMock(spec=grpc.aio.ServicerContext)

        resp = await servicer.SendMessage(
            _agent_message(message_id="custom-id-42", mentions=["ember-owl"]),
            context,
        )

        assert resp.message_id == "custom-id-42"

    async def test_dispatcher_failure_is_logged_not_propagated(self, caplog):
        """Dispatcher exceptions must be logged and not bubble up to the client.

        PR #101 review: before the ``_dispatch_and_log`` wrapper, an exception
        raised inside ``EventDispatcher.dispatch`` surfaced only as a
        ``Task exception was never retrieved`` warning at GC time, silently
        under-reporting real routing failures. This test locks in the fix.
        """
        stub = _StubAgent(agent_id="ember-owl", config={"model": "test"})
        servicer = ChannelServiceServicer(
            {"ember-owl": stub},
            MagicMock(spec=EventDispatcher),
        )
        servicer._dispatcher.dispatch = AsyncMock(
            side_effect=RuntimeError("upstream channel unavailable"),
        )
        context = MagicMock(spec=grpc.aio.ServicerContext)

        caplog.set_level(logging.ERROR, logger="agents.server")
        resp = await servicer.SendMessage(
            _agent_message(mentions=["ember-owl"]), context
        )
        # Await all pending dispatch tasks so the exception surfaces before
        # the test exits — asyncio.gather with return_exceptions swallows
        # the RuntimeError that the wrapper already logged.
        await asyncio.gather(
            *servicer._pending_dispatches, return_exceptions=True,
        )

        assert resp.delivered is True
        assert any(
            "Channel dispatch to agent ember-owl failed" in record.message
            and record.levelno == logging.ERROR
            for record in caplog.records
        ), "dispatcher exception should be logged at ERROR with agent id"

    async def test_pending_dispatches_retain_task_references(self):
        """Fire-and-forget tasks must be tracked until completion.

        Python 3.11+ asyncio docs warn that ``create_task`` returns a weakly
        referenced task — without a strong ref, GC may cancel it mid-flight.
        This test verifies the servicer holds a ref until done.
        """
        dispatch_started = asyncio.Event()
        dispatch_may_finish = asyncio.Event()

        async def slow_dispatch(_target_id, _event):
            dispatch_started.set()
            await dispatch_may_finish.wait()
            return []

        stub = _StubAgent(agent_id="ember-owl", config={"model": "test"})
        servicer = ChannelServiceServicer(
            {"ember-owl": stub},
            MagicMock(spec=EventDispatcher),
        )
        servicer._dispatcher.dispatch = slow_dispatch
        context = MagicMock(spec=grpc.aio.ServicerContext)

        await servicer.SendMessage(
            _agent_message(mentions=["ember-owl"]), context
        )
        await dispatch_started.wait()

        # Strong ref must be retained while dispatch is in flight.
        assert len(servicer._pending_dispatches) == 1

        dispatch_may_finish.set()
        await asyncio.gather(
            *servicer._pending_dispatches, return_exceptions=True,
        )
        # Done-callback should have evicted the task by now.
        assert len(servicer._pending_dispatches) == 0


# ─── ChannelServiceServicer.Subscribe Tests ───────────────────


class TestChannelServiceServicerSubscribe:
    """Tests for ChannelServiceServicer.Subscribe (v0.3 stub)."""

    async def test_subscribe_returns_unimplemented(self):
        """Subscribe must set UNIMPLEMENTED until v0.3 channel streaming is built."""
        servicer, _ = _channel_servicer()
        context = MagicMock(spec=grpc.aio.ServicerContext)
        request = agent_message_pb2.SubscribeRequest(
            channel_id="general", agent_id="ember-owl"
        )

        await servicer.Subscribe(request, context)

        context.set_code.assert_called_once_with(grpc.StatusCode.UNIMPLEMENTED)
