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
from agents.server_servicers import AgentServiceServicer, _extract_chat_reply
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

        # 0 → default 30 → clamped to max(1, min(30, 300)) = 30
        assert captured_timeouts[0] == 30

    async def test_executor_failure_still_returns_reply(self):
        """Reply is returned even when executor.execute() raises (two-phase guarantee)."""
        actions = [
            AgentAction(ActionType.SEND_MESSAGE, {"content": "safe reply", "mentions": ["local"]}),
        ]
        servicer = _make_servicer(actions)
        # Make executor raise after reply extraction
        servicer._dispatcher.executor.execute = AsyncMock(
            side_effect=RuntimeError("side-effect boom"),
        )
        context = _mock_context()

        resp = await servicer.SendChatMessage(
            _chat_request(user_id="local"), context,
        )

        assert resp.reply == "safe reply"
        assert resp.reply_status == "ok"

    async def test_relationship_memory_recorded(self):
        """record_interaction() is called when agent has relationship memory."""
        actions = [
            AgentAction(ActionType.SEND_MESSAGE, {"content": "hi", "mentions": ["local"]}),
        ]
        agent = _StubAgent(agent_id="ember-owl", config={"model": "test"})
        # Attach a mock memory.relationship attribute
        memory = MagicMock()
        memory.relationship = MagicMock()
        memory.relationship.record_interaction = AsyncMock()
        agent.memory = memory  # type: ignore[attr-defined]

        dispatcher = MagicMock(spec=EventDispatcher)
        dispatcher.dispatch = AsyncMock(return_value=actions)
        dispatcher.executor = MagicMock()
        dispatcher.executor.execute = AsyncMock(return_value=[])

        servicer = AgentServiceServicer({"ember-owl": agent}, dispatcher)
        context = _mock_context()

        await servicer.SendChatMessage(
            _chat_request(user_id="local", participant_type="user"), context,
        )

        memory.relationship.record_interaction.assert_called_once_with(
            other_id="local",
            interaction_type="chat",
            outcome="hi",
            other_participant_type="user",
        )

    async def test_relationship_memory_failure_still_returns_reply(self):
        """Reply is returned even when record_interaction() raises.

        Validates the exception handler at server_servicers.py that wraps
        the relationship memory call — the reply must never be lost due to
        a memory subsystem failure.  (Review finding: untested path.)
        """
        actions = [
            AgentAction(ActionType.SEND_MESSAGE, {"content": "hi", "mentions": ["local"]}),
        ]
        agent = _StubAgent(agent_id="ember-owl", config={"model": "test"})
        memory = MagicMock()
        memory.relationship = MagicMock()
        memory.relationship.record_interaction = AsyncMock(
            side_effect=RuntimeError("memory db unavailable"),
        )
        agent.memory = memory  # type: ignore[attr-defined]

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
        memory.relationship.record_interaction.assert_called_once()

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

        # -10 is truthy → raw_timeout=-10 → max(1, min(-10, 300)) = 1
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


# ─── PR 6 review follow-up tests ────────────────────────────


class TestChatServicerFollowUps:
    """Tests for review findings from PRs 1–5 (PR 6 follow-ups)."""

    async def test_agent_event_payload_structure(self):
        """Verify the AgentEvent payload passed to dispatch() has correct structure.

        Asserts payload keys (content, user_id, participant_type), sender_id,
        and metadata["session_id"].
        (PR 6 review fix: PR 3 finding #1 / test gap #9.)
        """
        actions = [AgentAction(ActionType.SEND_MESSAGE, {"content": "hi", "mentions": ["local"]})]
        servicer = _make_servicer(actions)
        context = _mock_context()

        await servicer.SendChatMessage(
            _chat_request(
                agent_id="ember-owl",
                user_id="local",
                message="hello world",
                session_id="sess-abc",
                participant_type="user",
            ),
            context,
        )

        # Inspect dispatch() call arguments
        dispatch_call = servicer._dispatcher.dispatch
        dispatch_call.assert_called_once()
        call_args = dispatch_call.call_args

        agent_id_arg = call_args.args[0]
        event_arg = call_args.args[1]

        assert agent_id_arg == "ember-owl"
        assert event_arg.payload["content"] == "hello world"
        assert event_arg.payload["user_id"] == "local"
        assert event_arg.payload["participant_type"] == "user"
        assert event_arg.sender_id == "local"
        assert event_arg.metadata["session_id"] == "sess-abc"
        assert event_arg.metadata["sender_participant_type"] == "user"

        # execute_actions=False keyword argument
        assert call_args.kwargs.get("execute_actions") is False

    async def test_malformed_agent_id_returns_not_found(self):
        """Agent ID with special chars that doesn't exist → NOT_FOUND.

        Valid (non-empty) agent IDs that don't match any registered agent
        return NOT_FOUND. Format validation is left to the orchestrator layer.
        (PR 6 review fix: PR 3 test gap #10.)
        """
        servicer = _make_servicer([], agent_id="ember-owl")
        context = _mock_context()

        resp = await servicer.SendChatMessage(
            _chat_request(agent_id="no-such-agent"), context,
        )

        context.set_code.assert_called_once_with(grpc.StatusCode.NOT_FOUND)
        assert resp.reply_status == "error"

    def test_extract_reply_send_message_missing_content(self):
        """SEND_MESSAGE with no 'content' key falls back to empty string via .get().

        (PR 6 review fix: PR 3 test gap #11.)
        """
        actions = [
            AgentAction(ActionType.SEND_MESSAGE, {"mentions": ["local"]}),
        ]
        reply, status = _extract_chat_reply(actions, "local")
        assert reply == ""
        assert status == "ok"

    def test_extract_reply_strips_delimiter_tags(self):
        """LLM-echoed <|user_message|> delimiters are stripped from reply text.

        The persona runtime wraps user messages in delimiter tags for prompt
        injection mitigation.  If the LLM echoes them back, the raw markup
        must not reach the end user.
        """
        raw = (
            '<|user_message|>\ndo you have access?\n<|/user_message|>\n\n'
            'No, I do not have internet access.'
        )
        actions = [
            AgentAction(ActionType.COMPLETE_TASK, {"result": raw}),
        ]
        reply, status = _extract_chat_reply(actions, "local")
        assert status == "ok"
        assert "<|user_message" not in reply
        assert "<|/user_message" not in reply
        assert "No, I do not have internet access." in reply

    def test_extract_reply_strips_delimiters_from_send_message(self):
        """Delimiter tags stripped from SEND_MESSAGE content too."""
        content = '<|user_message user_id="local"|>\nhi\n<|/user_message|>\nHello!'
        actions = [
            AgentAction(ActionType.SEND_MESSAGE, {
                "content": content,
                "channel_id": "ch-1",
                "mentions": ["local"],
            }),
        ]
        reply, status = _extract_chat_reply(actions, "local")
        assert status == "ok"
        assert "<|user_message" not in reply
        assert "Hello!" in reply


# ─── Sanitize Reply Edge Cases ───────────────────────────────


class TestSanitizeReplyEdgeCases:
    """Edge-case coverage for _sanitize_reply (tag-stripping fix).

    The persona runtime wraps user messages in ``<|user_message …|>``
    delimiters.  If the LLM echoes those tags in its response, they must
    be stripped before reaching the end user.
    """

    def test_clean_text_unchanged(self):
        """Text without any delimiter tags passes through untouched."""
        actions = [
            AgentAction(ActionType.SEND_MESSAGE, {"content": "Just a normal reply.", "mentions": []}),
        ]
        reply, status = _extract_chat_reply(actions, "local")
        assert reply == "Just a normal reply."
        assert status == "ok"

    def test_only_tags_returns_empty(self):
        """Reply consisting solely of delimiter tags becomes empty string."""
        actions = [
            AgentAction(ActionType.SEND_MESSAGE, {
                "content": '<|user_message|>\n<|/user_message|>',
                "mentions": [],
            }),
        ]
        reply, status = _extract_chat_reply(actions, "local")
        assert reply == ""
        assert status == "ok"

    def test_multiple_tag_pairs_stripped(self):
        """Multiple echoed tag pairs are all removed."""
        raw = (
            '<|user_message|>\nfirst question\n<|/user_message|>\n'
            'Answer one.\n'
            '<|user_message|>\nsecond question\n<|/user_message|>\n'
            'Answer two.'
        )
        actions = [
            AgentAction(ActionType.SEND_MESSAGE, {"content": raw, "mentions": []}),
        ]
        reply, _ = _extract_chat_reply(actions, "local")
        assert "<|user_message" not in reply
        assert "<|/user_message" not in reply
        assert "Answer one." in reply
        assert "Answer two." in reply

    def test_tag_with_attributes_stripped(self):
        """Tags with attributes like user_id are also stripped."""
        raw = '<|user_message user_id="max"|>\nhi\n<|/user_message|>\nHello Max!'
        actions = [
            AgentAction(ActionType.COMPLETE_TASK, {"result": raw}),
        ]
        reply, _ = _extract_chat_reply(actions, "local")
        assert "<|user_message" not in reply
        assert "Hello Max!" in reply

    def test_opening_tag_only_stripped(self):
        """Lone opening tag (without closing) is still stripped."""
        raw = '<|user_message|>\nSome leaked prefix\nActual reply here'
        actions = [
            AgentAction(ActionType.SEND_MESSAGE, {"content": raw, "mentions": []}),
        ]
        reply, _ = _extract_chat_reply(actions, "local")
        assert "<|user_message" not in reply
        assert "Actual reply here" in reply

    def test_closing_tag_only_stripped(self):
        """Lone closing tag (without opening) is still stripped."""
        raw = 'Actual reply<|/user_message|>'
        actions = [
            AgentAction(ActionType.SEND_MESSAGE, {"content": raw, "mentions": []}),
        ]
        reply, _ = _extract_chat_reply(actions, "local")
        assert "<|/user_message" not in reply
        assert "Actual reply" in reply

    async def test_sanitized_through_send_chat_message(self):
        """End-to-end: tags in dispatch output are stripped in ChatResponse."""
        raw = '<|user_message|>\ndo you recall?\n<|/user_message|>\nYes I do!'
        actions = [
            AgentAction(ActionType.SEND_MESSAGE, {
                "content": raw,
                "mentions": ["local"],
            }),
        ]
        servicer = _make_servicer(actions)
        context = _mock_context()

        resp = await servicer.SendChatMessage(
            _chat_request(user_id="local", message="do you recall?"), context,
        )

        assert "<|user_message" not in resp.reply
        assert "Yes I do!" in resp.reply
        assert resp.reply_status == "ok"
