"""
Tests for SendChatMessage gRPC servicer and _extract_chat_reply helper.

All tests use mock dispatcher and mock context — no real gRPC wire calls.
(RFC 0016 PR 3, OQ 5/6/7/9/11/13/16)
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import pytest

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.dispatch import EventDispatcher
from agents.generated import task_pb2
from agents.persona_types import ActionType, AgentAction
from agents.server import AgentServiceServicer, _extract_chat_reply
from agents.tools.registry import clear_registry


# ─── Fixtures / Helpers ──────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


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
    # executor.execute is called after reply extraction
    dispatcher.executor = MagicMock()
    dispatcher.executor.execute = AsyncMock(return_value=[])
    return AgentServiceServicer({agent_id: agent}, dispatcher)


# ─── _extract_chat_reply Tests ───────────────────────────────


class TestExtractChatReply:

    def test_user_targeted_send_message_wins(self):
        """SEND_MESSAGE with user_id in mentions is highest priority."""
        actions = [
            AgentAction(ActionType.SEND_MESSAGE, {"content": "to other", "mentions": ["iron-fox"]}),
            AgentAction(ActionType.SEND_MESSAGE, {"content": "to user", "mentions": ["local"]}),
        ]
        reply, status = _extract_chat_reply(actions, "local")
        assert reply == "to user"
        assert status == "ok"

    def test_fallback_to_any_send_message(self):
        """Any SEND_MESSAGE wins when no user-targeted one exists."""
        actions = [
            AgentAction(ActionType.SEND_MESSAGE, {"content": "general", "mentions": ["iron-fox"]}),
        ]
        reply, status = _extract_chat_reply(actions, "local")
        assert reply == "general"
        assert status == "ok"

    def test_fallback_to_complete_task(self):
        """COMPLETE_TASK result used when no SEND_MESSAGE exists."""
        actions = [
            AgentAction(ActionType.COMPLETE_TASK, {"result": "task done"}),
        ]
        reply, status = _extract_chat_reply(actions, "local")
        assert reply == "task done"
        assert status == "ok"

    def test_empty_reply_when_no_applicable_action(self):
        """Empty reply + 'empty' status when no relevant action found."""
        actions = [
            AgentAction(ActionType.DO_NOTHING, {}),
        ]
        reply, status = _extract_chat_reply(actions, "local")
        assert reply == ""
        assert status == "empty"

    def test_send_message_preferred_over_complete_task(self):
        """SEND_MESSAGE beats COMPLETE_TASK regardless of order."""
        actions = [
            AgentAction(ActionType.COMPLETE_TASK, {"result": "task result"}),
            AgentAction(ActionType.SEND_MESSAGE, {"content": "message", "mentions": []}),
        ]
        reply, status = _extract_chat_reply(actions, "local")
        assert reply == "message"
        assert status == "ok"

    def test_empty_actions_list(self):
        reply, status = _extract_chat_reply([], "local")
        assert reply == ""
        assert status == "empty"

    def test_user_id_empty_skips_user_targeted_priority(self):
        """Empty user_id falls through to any SEND_MESSAGE priority."""
        actions = [
            AgentAction(ActionType.SEND_MESSAGE, {"content": "broadcast", "mentions": []}),
        ]
        reply, status = _extract_chat_reply(actions, "")
        assert reply == "broadcast"
        assert status == "ok"


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

        # No error — "user" is a valid participant_type
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
        # Simulate a dispatch that never completes
        async def _slow_dispatch(*args, **kwargs):
            await asyncio.sleep(3600)
            return []
        dispatcher.dispatch = _slow_dispatch
        servicer = AgentServiceServicer({"ember-owl": agent}, dispatcher)
        context = _mock_context()

        # timeout_seconds=1 gets clamped to max(1, min(1, 300)) = 1
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

        # executor.execute should have been called with all actions
        servicer._dispatcher.executor.execute.assert_called_once()
        call_args = servicer._dispatcher.executor.execute.call_args
        assert call_args.args[1] == actions

    async def test_timeout_clamped_to_max(self):
        """timeout_seconds > 300 is clamped to 300."""
        actions = [AgentAction(ActionType.SEND_MESSAGE, {"content": "hi", "mentions": []})]
        servicer = _make_servicer(actions)
        context = _mock_context()

        # Patch asyncio.wait_for to capture the timeout argument.
        original_wait_for = asyncio.wait_for
        captured_timeouts: list[float] = []

        async def _patched_wait_for(coro, timeout):
            captured_timeouts.append(timeout)
            return await original_wait_for(coro, timeout)

        with patch("agents.server.asyncio.wait_for", side_effect=_patched_wait_for):
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

        with patch("agents.server.asyncio.wait_for", side_effect=_patched_wait_for):
            await servicer.SendChatMessage(
                _chat_request(timeout_seconds=0), context,
            )

        # 0 → default 30 → clamped to max(1, min(30, 300)) = 30
        assert captured_timeouts[0] == 30
