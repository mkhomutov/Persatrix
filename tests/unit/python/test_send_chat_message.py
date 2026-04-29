"""
Tests for SendChatMessage gRPC servicer.

Covers the full RPC lifecycle: routing, session handling, participant validation,
timeout clamping, executor side-effects, and relationship memory recording.
_extract_chat_reply and sanitize-reply tests live in test_extract_chat_reply.py.
(RFC 0016 PR 3, OQ 5/6/7/9/11/13/16)
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import grpc

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.dispatch import EventDispatcher
from agents.generated import task_pb2
from agents.persona_types import ActionType, AgentAction
from agents.server_servicers import AgentServiceServicer


# ─── Helpers ─────────────────────────────────────────────────


class _StubAgent(BaseAgent):
    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="ok")


def _chat_request(
    agent_id: str = "ember-owl",
    user_id: str = "local",
    message: str = "hello",
    session_id: str = "",
    timeout_seconds: int = 0,
    participant_type: str = "",
) -> task_pb2.ChatRequest:
    return task_pb2.ChatRequest(
        agent_id=agent_id,
        user_id=user_id,
        message=message,
        session_id=session_id,
        timeout_seconds=timeout_seconds,
        participant_type=participant_type,
    )


def _mock_context() -> MagicMock:
    return MagicMock(spec=grpc.aio.ServicerContext)


def _make_servicer(
    actions: list[AgentAction],
    agent_id: str = "ember-owl",
) -> AgentServiceServicer:
    """Create a servicer with a mock dispatcher that returns *actions*."""
    agent = _StubAgent(agent_id=agent_id, config={"model": "test"})
    dispatcher = MagicMock(spec=EventDispatcher)
    dispatcher.dispatch = AsyncMock(return_value=actions)
    dispatcher.executor = MagicMock()
    dispatcher.executor.execute = AsyncMock(return_value=[])
    return AgentServiceServicer({agent_id: agent}, dispatcher)


# ─── SendChatMessage Servicer Tests ──────────────────────────


class TestSendChatMessage:

    async def test_basic_reply(self):
        """Happy path: returns reply from SEND_MESSAGE action."""
        actions = [
            AgentAction(ActionType.SEND_MESSAGE, {"content": "hi there", "mentions": ["local"]}),
        ]
        servicer = _make_servicer(actions)
        context = _mock_context()

        resp = await servicer.SendChatMessage(_chat_request(user_id="local"), context)

        assert resp.reply == "hi there"
        assert resp.reply_status == "ok"
        assert resp.agent_id == "ember-owl"

    async def test_agent_not_found(self):
        """Returns NOT_FOUND when agent_id is unknown."""
        servicer = AgentServiceServicer({}, MagicMock(spec=EventDispatcher))
        context = _mock_context()

        resp = await servicer.SendChatMessage(
            _chat_request(agent_id="ghost"), context,
        )

        context.set_code.assert_called_once_with(grpc.StatusCode.NOT_FOUND)
        assert resp.reply_status == "error"

    async def test_session_id_generated_when_empty(self):
        """A UUID session_id is generated when request has empty session_id."""
        actions = [AgentAction(ActionType.SEND_MESSAGE, {"content": "hello", "mentions": []})]
        servicer = _make_servicer(actions)
        context = _mock_context()

        resp = await servicer.SendChatMessage(
            _chat_request(session_id=""), context,
        )

        assert resp.session_id != ""
        assert len(resp.session_id) == 36  # UUID4 format

    async def test_session_id_reused_when_provided(self):
        """Existing session_id is echoed back unchanged."""
        actions = [AgentAction(ActionType.SEND_MESSAGE, {"content": "hi", "mentions": []})]
        servicer = _make_servicer(actions)
        context = _mock_context()

        resp = await servicer.SendChatMessage(
            _chat_request(session_id="my-session-123"), context,
        )

        assert resp.session_id == "my-session-123"

    async def test_participant_type_defaults_to_user(self):
        """Empty participant_type field defaults to 'user'."""
        actions = [AgentAction(ActionType.SEND_MESSAGE, {"content": "ok", "mentions": []})]
        servicer = _make_servicer(actions)
        context = _mock_context()

        resp = await servicer.SendChatMessage(
            _chat_request(participant_type=""), context,
        )

        assert resp.reply_status == "ok"

    async def test_invalid_participant_type_rejected(self):
        """Invalid participant_type triggers INVALID_ARGUMENT."""
        agent = _StubAgent(agent_id="ember-owl", config={"model": "test"})
        servicer = AgentServiceServicer(
            {"ember-owl": agent}, MagicMock(spec=EventDispatcher),
        )
        context = _mock_context()

        resp = await servicer.SendChatMessage(
            _chat_request(participant_type="robot"), context,
        )

        context.set_code.assert_called_once_with(grpc.StatusCode.INVALID_ARGUMENT)
        assert resp.reply_status == "error"

    async def test_timeout_returns_deadline_exceeded(self):
        """asyncio.wait_for cancellation maps to DEADLINE_EXCEEDED."""
        agent = _StubAgent(agent_id="ember-owl", config={"model": "test"})
        dispatcher = MagicMock(spec=EventDispatcher)
        async def _slow_dispatch(*args, **kwargs):
            await asyncio.sleep(3600)
            return []
        dispatcher.dispatch = _slow_dispatch
        servicer = AgentServiceServicer({"ember-owl": agent}, dispatcher)
        context = _mock_context()

        resp = await servicer.SendChatMessage(
            _chat_request(timeout_seconds=1), context,
        )

        context.set_code.assert_called_once_with(grpc.StatusCode.DEADLINE_EXCEEDED)
        assert resp.reply_status == "error"

    async def test_exception_in_dispatch_returns_internal(self):
        """Unexpected exception in dispatch() → gRPC INTERNAL."""
        agent = _StubAgent(agent_id="ember-owl", config={"model": "test"})
        dispatcher = MagicMock(spec=EventDispatcher)
        dispatcher.dispatch = AsyncMock(side_effect=RuntimeError("boom"))
        servicer = AgentServiceServicer({"ember-owl": agent}, dispatcher)
        context = _mock_context()

        resp = await servicer.SendChatMessage(_chat_request(), context)

        context.set_code.assert_called_once_with(grpc.StatusCode.INTERNAL)
        assert resp.reply_status == "error"

    async def test_empty_reply_status_when_no_action(self):
        """reply_status='empty' when agent returns no applicable actions."""
        actions = [AgentAction(ActionType.DO_NOTHING, {})]
        servicer = _make_servicer(actions)
        context = _mock_context()

        resp = await servicer.SendChatMessage(_chat_request(), context)

        assert resp.reply == ""
        assert resp.reply_status == "empty"

    async def test_timestamp_populated(self):
        """Response timestamp is a recent Unix epoch value."""
        before = int(time.time())
        actions = [AgentAction(ActionType.SEND_MESSAGE, {"content": "hi", "mentions": []})]
        servicer = _make_servicer(actions)
        context = _mock_context()

        resp = await servicer.SendChatMessage(_chat_request(), context)

        after = int(time.time())
        assert before <= resp.timestamp <= after + 1

    async def test_agent_display_name_empty(self):
        """agent_display_name is empty string (orchestrator fills it)."""
        actions = [AgentAction(ActionType.SEND_MESSAGE, {"content": "hi", "mentions": []})]
        servicer = _make_servicer(actions)
        context = _mock_context()

        resp = await servicer.SendChatMessage(_chat_request(), context)

        assert resp.agent_display_name == ""

    async def test_executor_called_after_reply_extraction(self):
        """Side-effect actions are executed via executor after reply is secured."""
        actions = [
            AgentAction(ActionType.SEND_MESSAGE, {"content": "hello", "mentions": ["local"]}),
            AgentAction(ActionType.DO_NOTHING, {}),
        ]
        servicer = _make_servicer(actions)
        context = _mock_context()

        await servicer.SendChatMessage(_chat_request(user_id="local"), context)

        servicer._dispatcher.executor.execute.assert_called_once()
        call_args = servicer._dispatcher.executor.execute.call_args
        assert call_args.args[1] == actions

    async def test_timeout_clamped_to_max(self):
        """timeout_seconds > 300 is clamped to 300."""
        actions = [AgentAction(ActionType.SEND_MESSAGE, {"content": "hi", "mentions": []})]
        servicer = _make_servicer(actions)
        context = _mock_context()

        original_wait_for = asyncio.wait_for
        captured_timeouts: list[float] = []

        async def _patched_wait_for(coro, timeout):
            captured_timeouts.append(timeout)
            return await original_wait_for(coro, timeout)

        with patch("agents.server_servicers.asyncio.wait_for", side_effect=_patched_wait_for):
            await servicer.SendChatMessage(
                _chat_request(timeout_seconds=9999), context,
            )

        assert captured_timeouts[0] == 300

    async def test_timeout_clamped_to_min(self):
        """timeout_seconds=0 defaults to 30, which is >= 1."""
        actions = [AgentAction(ActionType.SEND_MESSAGE, {"content": "hi", "mentions": []})]
        servicer = _make_servicer(actions)
        context = _mock_context()

        captured_timeouts: list[float] = []
        original_wait_for = asyncio.wait_for

        async def _patched_wait_for(coro, timeout):
            captured_timeouts.append(timeout)
            return await original_wait_for(coro, timeout)

        with patch("agents.server_servicers.asyncio.wait_for", side_effect=_patched_wait_for):
            await servicer.SendChatMessage(
                _chat_request(timeout_seconds=0), context,
            )

        assert captured_timeouts[0] == 30

    async def test_executor_failure_still_returns_reply(self):
        """Reply is returned even when executor.execute() raises (two-phase guarantee)."""
        actions = [
            AgentAction(ActionType.SEND_MESSAGE, {"content": "safe reply", "mentions": ["local"]}),
        ]
        servicer = _make_servicer(actions)
        servicer._dispatcher.executor.execute = AsyncMock(
            side_effect=RuntimeError("side-effect boom"),
        )
        context = _mock_context()

        resp = await servicer.SendChatMessage(
            _chat_request(user_id="local"), context,
        )

        assert resp.reply == "safe reply"
        assert resp.reply_status == "ok"

    async def test_relationship_memory_not_recorded_per_event(self):
        """RFC 0020 PR 4: per-event ``record_interaction`` was removed.

        The relationship row is now bumped once per closed interaction
        in :meth:`_StatePersistenceMixin._persist_closed_interaction`
        (see ``tests/integration/test_summarize_on_close.py::
        TestRecordInteractionMove``).  This test guards against the
        per-event call site sneaking back in — if it did,
        ``interaction_count`` would inflate by N for every N-turn
        chat session.
        """
        actions = [
            AgentAction(ActionType.SEND_MESSAGE, {"content": "hi", "mentions": ["local"]}),
        ]
        agent = _StubAgent(agent_id="ember-owl", config={"model": "test"})
        memory = MagicMock()
        memory.relationship = MagicMock()
        memory.relationship.record_interaction = AsyncMock()
        agent._memory = memory  # type: ignore[attr-defined]

        dispatcher = MagicMock(spec=EventDispatcher)
        dispatcher.dispatch = AsyncMock(return_value=actions)
        dispatcher.executor = MagicMock()
        dispatcher.executor.execute = AsyncMock(return_value=[])

        servicer = AgentServiceServicer({"ember-owl": agent}, dispatcher)
        context = _mock_context()

        resp = await servicer.SendChatMessage(
            _chat_request(user_id="local", participant_type="user"), context,
        )

        assert resp.reply == "hi"
        assert resp.reply_status == "ok"
        memory.relationship.record_interaction.assert_not_called()

    # ─── Input Validation Length Limits ──────────────────────

    async def test_session_id_exceeds_max_length(self):
        """session_id longer than 128 chars triggers INVALID_ARGUMENT."""
        servicer = _make_servicer([])
        context = _mock_context()

        resp = await servicer.SendChatMessage(
            _chat_request(session_id="x" * 129), context,
        )

        context.set_code.assert_called_once_with(grpc.StatusCode.INVALID_ARGUMENT)
        assert resp.reply_status == "error"
        context.set_details.assert_called_once_with("session_id exceeds 128 characters")

    async def test_message_exceeds_max_length(self):
        """message longer than 32768 chars triggers INVALID_ARGUMENT."""
        servicer = _make_servicer([])
        context = _mock_context()

        resp = await servicer.SendChatMessage(
            _chat_request(message="a" * 32769), context,
        )

        context.set_code.assert_called_once_with(grpc.StatusCode.INVALID_ARGUMENT)
        assert resp.reply_status == "error"
        context.set_details.assert_called_once_with("message exceeds 32768 characters")

    async def test_user_id_exceeds_max_length(self):
        """user_id longer than 256 chars triggers INVALID_ARGUMENT."""
        servicer = _make_servicer([])
        context = _mock_context()

        resp = await servicer.SendChatMessage(
            _chat_request(user_id="u" * 257), context,
        )

        context.set_code.assert_called_once_with(grpc.StatusCode.INVALID_ARGUMENT)
        assert resp.reply_status == "error"
        context.set_details.assert_called_once_with("user_id exceeds 256 characters")

    async def test_negative_timeout_clamped_to_minimum(self):
        """Negative timeout_seconds is clamped to 1s by max(1, ...) guard.

        Protobuf int32 allows negative values; the server treats them as
        'use minimum' rather than rejecting, since the clamp guarantees a
        safe positive timeout.  (Review finding: document this edge case.)
        """
        actions = [AgentAction(ActionType.SEND_MESSAGE, {"content": "ok", "mentions": []})]
        servicer = _make_servicer(actions)
        context = _mock_context()

        captured_timeouts: list[float] = []
        original_wait_for = asyncio.wait_for

        async def _patched_wait_for(coro, timeout):
            captured_timeouts.append(timeout)
            return await original_wait_for(coro, timeout)

        with patch("agents.server_servicers.asyncio.wait_for", side_effect=_patched_wait_for):
            resp = await servicer.SendChatMessage(
                _chat_request(timeout_seconds=-10), context,
            )

        assert captured_timeouts[0] == 1
        assert resp.reply_status == "ok"

    async def test_empty_message_still_dispatches(self):
        """Empty message string is dispatched normally (agent decides reply)."""
        actions = [AgentAction(ActionType.SEND_MESSAGE, {"content": "ok", "mentions": []})]
        servicer = _make_servicer(actions)
        context = _mock_context()

        resp = await servicer.SendChatMessage(
            _chat_request(message=""), context,
        )

        assert resp.reply == "ok"
        assert resp.reply_status == "ok"

    async def test_empty_agent_id_returns_invalid_argument(self):
        """Empty agent_id → INVALID_ARGUMENT (not NOT_FOUND).

        (PR 6 review fix: PR 3 finding #2.)
        """
        servicer = _make_servicer([])
        context = _mock_context()

        resp = await servicer.SendChatMessage(
            _chat_request(agent_id=""), context,
        )

        context.set_code.assert_called_once_with(grpc.StatusCode.INVALID_ARGUMENT)
        context.set_details.assert_called_once_with("agent_id is required")
        assert resp.reply_status == "error"
