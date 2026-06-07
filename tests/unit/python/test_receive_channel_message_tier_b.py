"""RFC 0030 Tier B (v0.3.8) PR 2b — ``ReceiveChannelMessage`` unpacks the
salience-bid proto fields into ``event.payload``.

PR 2b flips the PR-2a-dormant seam live: the Go dispatcher now populates the
Tier B ``ChannelMessageEvent`` fields, and this servicer must unpack them under
the exact keys ``agents.persona_runtime.tier_b_gate`` reads
(``tier_b_active`` / ``threshold`` / ``channel_size`` /
``tier_b_max_channel_members``). Split out of
``test_receive_channel_message.py`` so that file stays under the 500-line cap.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import grpc

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.dispatch import EventDispatcher
from agents.generated import task_pb2
from agents.persona_types import AgentEvent
from agents.server_servicers import AgentServiceServicer


class _StubAgent(BaseAgent):
    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="ok")


def _make_servicer() -> tuple[AgentServiceServicer, MagicMock]:
    agents = {"ember-owl": _StubAgent(agent_id="ember-owl", config={"model": "test"})}
    dispatcher = MagicMock(spec=EventDispatcher)
    dispatcher.enqueue_inbound = MagicMock(return_value=True)
    return AgentServiceServicer(agents, dispatcher), dispatcher


def _channel_event(**overrides: Any) -> task_pb2.ChannelMessageEvent:
    fields: dict[str, Any] = {
        "message_id": "msg-001",
        "channel_id": "group:general",
        "channel_type": "group",
        "sender_id": "iron-fox",
        "content": "hello",
        "timestamp": "2026-05-04T00:00:00Z",
        "thread_id": "",
        "mentions": [],
        "respond_policy": "always",
        "thread_parent_sender_id": "",
    }
    fields.update(overrides)
    return task_pb2.ChannelMessageEvent(**fields)


def _enqueued_event(dispatcher: MagicMock) -> AgentEvent:
    dispatcher.enqueue_inbound.assert_called_once()
    return dispatcher.enqueue_inbound.call_args.args[1]


class TestReceiveChannelMessageTierB:
    async def test_unpacks_tier_b_fields_into_payload(self):
        servicer, dispatcher = _make_servicer()
        await servicer.ReceiveChannelMessage(
            _channel_event(
                tier_b_active=True,
                threshold=0.3,
                channel_size=4,
                tier_b_max_channel_members=20,
            ),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = _enqueued_event(dispatcher)
        assert event.payload["tier_b_active"] is True
        assert event.payload["threshold"] == 0.3
        assert event.payload["channel_size"] == 4
        assert event.payload["tier_b_max_channel_members"] == 20

    async def test_unset_threshold_unpacks_to_none(self):
        """The `optional double threshold` tri-state survives the unpack: an
        absent field must reach the seam as ``None`` (unset → bias-to-silence,
        demands a decisive score), NOT proto3's 0.0 default (which the bid
        reads as a real "speak on any score" floor)."""
        servicer, dispatcher = _make_servicer()
        # tier_b_active set, threshold omitted (a plain `participant`).
        await servicer.ReceiveChannelMessage(
            _channel_event(tier_b_active=True, channel_size=4),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = _enqueued_event(dispatcher)
        assert event.payload["threshold"] is None, (
            "an unset optional threshold must unpack to None, not 0.0"
        )

    async def test_explicit_zero_threshold_unpacks_to_zero(self):
        """An explicit 0.0 is distinct from unset and must survive as 0.0."""
        servicer, dispatcher = _make_servicer()
        await servicer.ReceiveChannelMessage(
            _channel_event(tier_b_active=True, threshold=0.0, channel_size=4),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = _enqueued_event(dispatcher)
        assert event.payload["threshold"] == 0.0

    async def test_legacy_event_defaults_keep_seam_dormant(self):
        """A pre-v0.3.8 publisher omits every Tier B field: tier_b_active
        defaults False, so the seam stays "not applicable" — back-compat."""
        servicer, dispatcher = _make_servicer()
        await servicer.ReceiveChannelMessage(
            _channel_event(), MagicMock(spec=grpc.aio.ServicerContext)
        )
        event = _enqueued_event(dispatcher)
        assert event.payload["tier_b_active"] is False
        assert event.payload["threshold"] is None
