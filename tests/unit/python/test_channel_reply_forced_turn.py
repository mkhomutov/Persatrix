"""RFC 0052 §B/§D — a convene / synthesis forced-turn reply is open-floor.

Part of the convene / synthesis DELIVERY fix. ``synthesize_channel_reply`` (the
mock / plain-text channel-reply path the offline demo runs on) auto-@-mentions
the inbound ``sender_id`` so an ordinary peer reply is addressed back to its
sender. But a convene (opening) or synthesis (closing) forced turn is dispatched
from a SYNTHETIC control sender (``orchestrator:convene`` / ``orchestrator:synthesis``)
that is NOT a real participant — mentioning it makes the convener's opener fail
the orchestrator's publish-path participant-id validation
(``mentions[0]: invalid participant id``), so the opener ``400``s and the whole
discussion stalls before it starts (surfaced by booting ``make demo-autonomous``).
The opener "names no one" (RFC §B); it addresses the open floor.

These tests pin that a forced control turn's reply carries NO mention (keyed on
the forced-turn MARKER, so it stays correct if the sentinel id ever changes),
while an ordinary peer reply still gets its reply-to auto-mention.
"""

from __future__ import annotations

import pytest

from agents.persona_runtime.channel_reply import synthesize_channel_reply
from agents.persona_types import ActionType, AgentAction, AgentEvent, EventType


def _forced_turn_event(marker: str, sender_id: str) -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={
            "content": "Goal: A synthesized recommendation. Topic: monorepo.",
            "channel_type": "group",
            "mentions": [],
            marker: True,
        },
        channel_id="group:roundtable",
        sender_id=sender_id,
        message_id="dir-1",
    )


def _peer_event(sender_id: str) -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={
            "content": "what do you think?",
            "channel_type": "group",
            "mentions": [],
        },
        channel_id="group:roundtable",
        sender_id=sender_id,
        message_id="peer-1",
    )


def _reply(result: str) -> list[AgentAction]:
    return [AgentAction(action_type=ActionType.COMPLETE_TASK, payload={"result": result})]


@pytest.mark.parametrize(
    "marker,sender",
    [
        ("convene", "orchestrator:convene"),
        ("synthesis_turn", "orchestrator:synthesis"),
    ],
)
def test_forced_control_turn_reply_is_open_floor(marker: str, sender: str) -> None:
    out = synthesize_channel_reply(
        _forced_turn_event(marker, sender),
        _reply("Let me open the discussion."),
        agent_id="nova-sparrow",
    )
    send = out[0]
    assert send.action_type is ActionType.SEND_CHANNEL_MESSAGE
    assert send.payload["mentions"] == [], (
        "a convene/synthesis forced-turn reply addresses the open floor — it must "
        "not @-mention the synthetic dispatch sender (the publish path rejects it)"
    )


def test_ordinary_peer_reply_still_mentions_the_sender() -> None:
    # Regression: the reply-to auto-mention is unchanged for a normal peer message.
    out = synthesize_channel_reply(
        _peer_event("iron-fox"),
        _reply("Good point."),
        agent_id="nova-sparrow",
    )
    assert out[0].payload["mentions"] == ["iron-fox"]
