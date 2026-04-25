"""
Tests for _extract_chat_reply and reply sanitization.

Covers the pure reply-extraction helper and sanitize-reply edge cases.
SendChatMessage integration tests live in test_send_chat_message.py.
(RFC 0016 PR 3, OQ 5/6/7/9/11/13/16)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import grpc
import pytest

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.dispatch import EventDispatcher
from agents.generated import task_pb2
from agents.persona_types import ActionType, AgentAction
from agents.server_servicers import AgentServiceServicer, _extract_chat_reply


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
        """LLM-echoed <|user_message|> delimiters are stripped from reply text."""
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

    def test_torn_opening_fragment_at_end_stripped(self):
        """A torn opening fragment with no closing ``|>`` at end-of-string is stripped."""
        raw = "Real reply text.\n<|user_mess"
        actions = [
            AgentAction(ActionType.SEND_MESSAGE, {"content": raw, "mentions": []}),
        ]
        reply, _ = _extract_chat_reply(actions, "local")
        assert reply == "Real reply text."
        assert "<|" not in reply

    def test_tag_with_inner_pipe_stripped(self):
        """Tag whose attribute value contains a pipe (e.g. ``user_id="a|b"``) is stripped."""
        raw = '<|user_message user_id="a|b"|>\nhi\n<|/user_message|>\nHello!'
        actions = [
            AgentAction(ActionType.SEND_MESSAGE, {"content": raw, "mentions": []}),
        ]
        reply, _ = _extract_chat_reply(actions, "local")
        assert "<|user_message" not in reply
        assert "<|/user_message" not in reply
        assert "Hello!" in reply
