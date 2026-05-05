"""
Tests for ``ActionExecutor._handle_send_channel_message`` REST publish branch.

RFC 0011 PR 4a-ii-β-1 rewires SEND_CHANNEL_MESSAGE actions with a
``channel_id`` to ``POST /api/v1/channels/{id}/messages`` via the injected
:class:`ChannelPublisher`.  Mention-only actions (legacy chat-reply path
with no ``channel_id``) keep using the in-process :class:`EventDispatcher`
until the chat-as-DM façade lands in PR 4a-ii-β-2.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agents.dispatch import ActionExecutor, EventDispatcher
from agents.persona_types import ActionType, AgentAction


@pytest.fixture
def publisher() -> AsyncMock:
    """A mock :class:`ChannelPublisher` whose ``publish`` is awaitable."""
    pub = AsyncMock()
    pub.publish = AsyncMock(return_value=None)
    return pub


class TestRESTPublishBranch:

    async def test_channel_id_set_calls_publisher_and_returns_published(self, publisher):
        executor = ActionExecutor(channel_publisher=publisher)
        action = AgentAction(
            ActionType.SEND_CHANNEL_MESSAGE,
            {"channel_id": "group:planning", "content": "hi", "mentions": ["agent-b"]},
        )

        result = await executor._handle_send_channel_message("agent-a", action)

        publisher.publish.assert_awaited_once_with(
            channel_id="group:planning",
            sender_id="agent-a",
            content="hi",
            mentions=["agent-b"],
        )
        assert result == {
            "action_type": "send_channel_message",
            "status": "published",
            "channel_id": "group:planning",
        }

    async def test_publisher_exception_returns_failed_status(self, publisher):
        publisher.publish = AsyncMock(side_effect=RuntimeError("boom"))
        executor = ActionExecutor(channel_publisher=publisher)
        action = AgentAction(
            ActionType.SEND_CHANNEL_MESSAGE,
            {"channel_id": "group:planning", "content": "hi", "mentions": []},
        )

        result = await executor._handle_send_channel_message("agent-a", action)

        assert result["status"] == "failed"
        assert result["channel_id"] == "group:planning"

    async def test_mentions_truncated_before_publish(self, publisher):
        executor = ActionExecutor(channel_publisher=publisher)
        # 11 mentions → truncated to 10 (the _MAX_MENTIONS_PER_ACTION cap).
        many = [f"agent-{i}" for i in range(11)]
        action = AgentAction(
            ActionType.SEND_CHANNEL_MESSAGE,
            {"channel_id": "group:planning", "content": "hi", "mentions": many},
        )

        await executor._handle_send_channel_message("agent-a", action)

        called = publisher.publish.await_args
        assert len(called.kwargs["mentions"]) == 10

    async def test_no_channel_id_falls_back_to_in_process_dispatch(self, publisher):
        # Legacy chat-reply path: the agent emits a SEND_CHANNEL_MESSAGE with
        # mentions but no channel_id (the chat user is the implicit target).
        # The REST publisher must not be touched; the in-process dispatcher
        # should handle routing until chat-as-DM unifies the two paths.
        dispatcher = EventDispatcher()
        dispatcher.dispatch = AsyncMock(return_value=[])
        executor = ActionExecutor(dispatcher=dispatcher, channel_publisher=publisher)
        action = AgentAction(
            ActionType.SEND_CHANNEL_MESSAGE,
            {"content": "reply", "mentions": ["user"]},
        )

        result = await executor._handle_send_channel_message("agent-a", action)

        publisher.publish.assert_not_awaited()
        # Falls into the in-process dispatch loop; with a stub dispatcher
        # the recipient lookup succeeds (mock returns []) so dispatched=1.
        assert result["status"] == "dispatched"
        assert result["dispatched_to"] == 1

    async def test_no_channel_id_no_mentions_returns_no_targets(self, publisher):
        executor = ActionExecutor(dispatcher=EventDispatcher(), channel_publisher=publisher)
        action = AgentAction(
            ActionType.SEND_CHANNEL_MESSAGE,
            {"content": "hi"},  # no channel_id, no mentions
        )

        result = await executor._handle_send_channel_message("agent-a", action)

        publisher.publish.assert_not_awaited()
        assert result["status"] == "no_targets"
        assert result["dispatched_to"] == 0


class TestSetChannelPublisher:

    async def test_set_channel_publisher_updates_executor(self, publisher):
        # AgentServer wires the publisher post-construction; verify the
        # setter actually plugs through to the publish branch.
        dispatcher = EventDispatcher()
        assert dispatcher._executor._channel_publisher is None

        dispatcher.set_channel_publisher(publisher)

        assert dispatcher._executor._channel_publisher is publisher

        action = AgentAction(
            ActionType.SEND_CHANNEL_MESSAGE,
            {"channel_id": "group:x", "content": "hi", "mentions": []},
        )
        result = await dispatcher._executor._handle_send_channel_message("agent-a", action)
        assert result["status"] == "published"
        publisher.publish.assert_awaited_once()
