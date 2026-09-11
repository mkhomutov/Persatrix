"""ISSUE-0155 — a REST chat turn leases under ``CAUSE_CHANNEL_MESSAGE``.

Since v0.3.0 the chat endpoint (``POST /api/v1/agents/{id}/chat``) publishes
the person's message into their ``dm:`` channel with the persona, and the
persona receives it through ``ReceiveChannelMessage`` like any other channel
message. The chat handler stamps ``chat_session_id`` on that publish, but
``ChannelMessageEvent`` has no field for it, so the key stops at the
orchestrator and ``cause_for_event`` takes its channel-message arm.
``CAUSE_CHAT`` comes only from the unused ``SendChatMessage`` servicer
(ISSUE-0035).

ISSUE-0155 keeps that label rather than restoring ``CAUSE_CHAT``: the wallet
reads the cause only in its log lines, and RFC 0032 plans to retire
``chat_session_id`` along with the chat façade. These tests pin the label on
the real receive path, so changing a chat turn's lease cause — through a new
wire field or a rule keyed on the DM — fails here and points back at the
issue.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import grpc

from agents.generated import task_pb2
from agents.generated import wallet_pb2 as walletpb
from agents.persona_runtime.wallet_cause import lease_attribution_for_event

from ._receive_channel_message_helpers import (
    channel_event,
    enqueued_event,
    make_servicer,
)

_INTERACTION_ID = "5f0c2a7e-8b1d-4c3e-9a6f-2d4b8e1c7a90"


def _rest_chat_turn() -> task_pb2.ChannelMessageEvent:
    """A chat turn as the orchestrator delivers it: the person's message in
    their DM with the persona, typed ``user`` by the chat handler, addressed
    to the persona, inside the DM's current interaction."""
    return channel_event(
        channel_id="dm:alice:ember-owl",
        channel_type="dm",
        sender_id="alice",
        content="What did we decide yesterday?",
        mentions=["ember-owl"],
        sender_participant_type="user",
        interaction_id=_INTERACTION_ID,
    )


class TestRestChatLeaseCause:
    def test_wire_event_has_no_chat_session_id_field(self) -> None:
        """The chat token has no field to ride from the orchestrator to the agent."""
        fields = task_pb2.ChannelMessageEvent.DESCRIPTOR.fields_by_name
        assert "chat_session_id" not in fields, (
            "ChannelMessageEvent grew a chat_session_id field, so REST chat "
            "turns may now lease as CAUSE_CHAT: revisit ISSUE-0155's decision "
            "and update RFC 0023's note and docs/diagrams/workflow-execution.md"
        )

    async def test_rest_chat_turn_leases_as_channel_message(self) -> None:
        """The lifted event is a person's DM turn, and it leases as a channel message."""
        servicer, dispatcher = make_servicer()
        ack = await servicer.ReceiveChannelMessage(
            _rest_chat_turn(), MagicMock(spec=grpc.aio.ServicerContext)
        )
        assert ack.success is True, ack.error_message

        event = enqueued_event(dispatcher)
        # Everything a rule keyed on the DM would read is present ...
        assert event.payload["channel_type"] == "dm"
        assert event.metadata["sender_participant_type"] == "user"
        # ... but the chat token is not, so the channel-message arm applies.
        assert "chat_session_id" not in event.metadata
        assert lease_attribution_for_event(event, agent_id="ember-owl") == (
            walletpb.CAUSE_CHANNEL_MESSAGE,
            "ember-owl",
            _INTERACTION_ID,
        )
