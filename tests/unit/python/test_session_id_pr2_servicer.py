"""ISSUE-0081 PR 2 — gRPC servicer stamps the session envelope.

The pure ``_session_from_metadata`` helper and the ``on_event`` binding are
covered in ``test_session_id_pr2_binding.py``.  This file pins the *wiring*
between them: both inbound servicer methods must read the
``persatrix-session`` header off the live gRPC context and stamp it onto
:attr:`AgentEvent.metadata` under :data:`EVENT_SESSION_METADATA_KEY`.

That stamp is the seam the whole PR exists to build — it is what lets
``on_event`` later re-enter a ``session_scope`` for the handler.  It is also
the one piece the rest of the PR's TDD did not exercise directly, so a
regression (wrong key, omitted stamping, or a broken
``invocation_metadata`` cast) would go unnoticed until the Go orchestrator
(PR 2b) starts emitting the header in production.

Contract pinned here, for both ``SendChatMessage`` (sync dispatch) and
``ReceiveChannelMessage`` (fire-and-forget enqueue):

* header present  → ``event.metadata[EVENT_SESSION_METADATA_KEY]`` carries it;
* header absent   → the key is **omitted** (the handler falls back to its
  construction snapshot — a blank/legacy stamp would re-merge concurrent
  conversations, defeating the fix);
* the RFC 0031 session and the CLI ``chat_session_id`` coexist on the same
  ``event.metadata`` dict without collision.

Split from ``test_session_id_pr2_binding.py`` to keep both files under the
500-line ``scripts/checks/file_size.py --strict`` cap.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import grpc

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.dispatch import EventDispatcher
from agents.generated import task_pb2
from agents.persona_types import ActionType, AgentAction
from agents.server_servicers import AgentServiceServicer
from agents.session_id import EVENT_SESSION_METADATA_KEY, SESSION_METADATA_GRPC_KEY

# ─── Harness ────────────────────────────────────────────────


class _StubAgent(BaseAgent):
    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="ok")


def _context_with_metadata(metadata: list[tuple[str, str]]) -> MagicMock:
    """A gRPC servicer context whose ``invocation_metadata()`` returns
    *metadata* — a list of ``(key, value)`` pairs, the flattened shape
    ``grpc.aio.Metadata`` yields at runtime."""
    context = MagicMock(spec=grpc.aio.ServicerContext)
    context.invocation_metadata.return_value = metadata
    return context


def _chat_servicer() -> tuple[AgentServiceServicer, MagicMock]:
    agent = _StubAgent(agent_id="ember-owl", config={"model": "test"})
    dispatcher = MagicMock(spec=EventDispatcher)
    dispatcher.dispatch = AsyncMock(return_value=[
        AgentAction(ActionType.SEND_CHANNEL_MESSAGE,
                    {"content": "hi", "mentions": ["local"]}),
    ])
    dispatcher.executor = MagicMock()
    dispatcher.executor.execute = AsyncMock(return_value=[])
    return AgentServiceServicer({"ember-owl": agent}, dispatcher), dispatcher


def _channel_servicer() -> tuple[AgentServiceServicer, MagicMock]:
    agent = _StubAgent(agent_id="ember-owl", config={"model": "test"})
    dispatcher = MagicMock(spec=EventDispatcher)
    dispatcher.enqueue_inbound = MagicMock(return_value=True)
    return AgentServiceServicer({"ember-owl": agent}, dispatcher), dispatcher


def _channel_request() -> task_pb2.ChannelMessageEvent:
    return task_pb2.ChannelMessageEvent(
        message_id="msg-001",
        channel_id="group:general",
        channel_type="group",
        sender_id="iron-fox",
        content="hello",
        timestamp="2026-05-04T00:00:00Z",
        respond_policy="always",
    )


# ─── SendChatMessage (synchronous dispatch path) ────────────


class TestSendChatStampsSession:
    async def test_stamps_session_from_header(self) -> None:
        servicer, dispatcher = _chat_servicer()
        context = _context_with_metadata([(SESSION_METADATA_GRPC_KEY, "conv-x")])

        await servicer.SendChatMessage(
            task_pb2.ChatRequest(
                agent_id="ember-owl", user_id="local", message="hi",
            ),
            context,
        )

        event = dispatcher.dispatch.call_args.args[1]
        assert event.metadata.get(EVENT_SESSION_METADATA_KEY) == "conv-x"

    async def test_omits_key_when_header_absent(self) -> None:
        # No ``persatrix-session`` header → the key is *omitted* (handler
        # falls back to its construction snapshot), not stamped blank — a
        # blank/legacy stamp would re-merge concurrent conversations.
        servicer, dispatcher = _chat_servicer()
        context = _context_with_metadata([("user-agent", "grpc-go")])

        await servicer.SendChatMessage(
            task_pb2.ChatRequest(
                agent_id="ember-owl", user_id="local", message="hi",
            ),
            context,
        )

        event = dispatcher.dispatch.call_args.args[1]
        assert EVENT_SESSION_METADATA_KEY not in event.metadata

    async def test_session_distinct_from_chat_session_id(self) -> None:
        # The RFC 0031 operator namespace (``persatrix_session``) and the
        # CLI chat-session id (``chat_session_id``) ride the same
        # ``event.metadata`` dict under different keys; pin end-to-end that
        # they coexist without collision at the servicer.
        servicer, dispatcher = _chat_servicer()
        context = _context_with_metadata([(SESSION_METADATA_GRPC_KEY, "conv-x")])

        await servicer.SendChatMessage(
            task_pb2.ChatRequest(
                agent_id="ember-owl", user_id="local", message="hi",
                chat_session_id="chat-42",
            ),
            context,
        )

        event = dispatcher.dispatch.call_args.args[1]
        assert event.metadata.get(EVENT_SESSION_METADATA_KEY) == "conv-x"
        assert event.metadata.get("chat_session_id") == "chat-42"


# ─── ReceiveChannelMessage (fire-and-forget enqueue path) ───


class TestReceiveChannelStampsSession:
    async def test_stamps_session_from_header(self) -> None:
        servicer, dispatcher = _channel_servicer()
        context = _context_with_metadata([(SESSION_METADATA_GRPC_KEY, "conv-y")])

        ack = await servicer.ReceiveChannelMessage(_channel_request(), context)

        assert ack.success
        event = dispatcher.enqueue_inbound.call_args.args[1]
        assert event.metadata.get(EVENT_SESSION_METADATA_KEY) == "conv-y"

    async def test_omits_key_when_header_absent(self) -> None:
        servicer, dispatcher = _channel_servicer()
        context = _context_with_metadata([("user-agent", "grpc-go")])

        await servicer.ReceiveChannelMessage(_channel_request(), context)

        event = dispatcher.enqueue_inbound.call_args.args[1]
        assert EVENT_SESSION_METADATA_KEY not in event.metadata
