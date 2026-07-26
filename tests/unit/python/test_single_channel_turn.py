"""RFC 0038 §B single-channel-turn guard (carved into RFC 0037 PR 4).

A channel-anchored turn publishes only to its acting channel: a
``SEND_CHANNEL_MESSAGE`` targeting any other channel is replaced with
``DO_NOTHING`` + WARNING (the audit event is a tracked follow-up).  The
tick-shaped class is exempt — its injection is already floored to the §D
``public`` acting level, so no target can receive anything above
``public`` from it.
"""

from __future__ import annotations

import logging

import pytest

from agents.persona_runtime.single_channel_turn import (
    enforce_single_channel_turn,
)
from agents.persona_types import ActionType, AgentAction, AgentEvent, EventType


def _send(channel_id: str | None, content: str = "hi") -> AgentAction:
    payload: dict = {"content": content}
    if channel_id is not None:
        payload["channel_id"] = channel_id
    return AgentAction(
        action_type=ActionType.SEND_CHANNEL_MESSAGE, payload=payload,
    )


def _channel_event(channel_id: str = "group:planning") -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        channel_id=channel_id,
        payload={"content": "inbound"},
    )


class TestCrossChannelPublishRejected:
    def test_cross_channel_send_becomes_do_nothing(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        event = _channel_event("group:planning")
        with caplog.at_level(
            logging.WARNING,
            logger="agents.persona_runtime.single_channel_turn",
        ):
            actions = enforce_single_channel_turn(
                event, [_send("group:other")], agent_id="a1",
            )
        assert [a.action_type for a in actions] == [ActionType.DO_NOTHING]
        warnings = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        text = warnings[0].getMessage()
        assert "group:other" in text and "group:planning" in text

    def test_same_channel_send_passes(self) -> None:
        event = _channel_event("group:planning")
        sent = _send("group:planning")
        assert enforce_single_channel_turn(
            event, [sent], agent_id="a1",
        ) == [sent]

    def test_mixed_actions_replace_only_the_offender(self) -> None:
        """One entry per parsed action survives — downstream accounting
        (energy drain, episode routing) sees a stable list shape."""
        event = _channel_event("dm:a:b")
        ok = _send("dm:a:b")
        offender = _send("group:planning")
        other = AgentAction(action_type=ActionType.USE_TOOL, payload={})
        out = enforce_single_channel_turn(
            event, [ok, offender, other], agent_id="a1",
        )
        assert out[0] is ok
        assert out[1].action_type is ActionType.DO_NOTHING
        assert out[2] is other

    def test_input_list_never_mutated(self) -> None:
        event = _channel_event("group:planning")
        offender = _send("group:other")
        actions = [offender]
        enforce_single_channel_turn(event, actions, agent_id="a1")
        assert actions == [offender]


class TestExemptions:
    def test_tick_turn_publishes_anywhere(self) -> None:
        """The §D floor is the tick exemption's soundness argument: a
        channel-less turn's context is gated to ``public``, so any
        publish target is safe by construction."""
        event = AgentEvent(event_type=EventType.TICK)
        sent = _send("group:anywhere")
        assert enforce_single_channel_turn(
            event, [sent], agent_id="a1",
        ) == [sent]

    @pytest.mark.parametrize("event_type", [
        EventType.TASK_ASSIGNED,
        EventType.SUB_AGENT_COMPLETED,
        EventType.APPROVAL_REQUESTED,
    ])
    def test_floor_class_turns_are_exempt(
        self, event_type: EventType,
    ) -> None:
        event = AgentEvent(event_type=event_type)
        sent = _send("group:anywhere")
        assert enforce_single_channel_turn(
            event, [sent], agent_id="a1",
        ) == [sent]

    def test_channel_event_without_channel_id_is_exempt(self) -> None:
        """Degenerate shape: nothing to compare against — injection was
        floored to ``public`` by the same absence (rule (b))."""
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE, payload={"content": "x"},
        )
        sent = _send("group:anywhere")
        assert enforce_single_channel_turn(
            event, [sent], agent_id="a1",
        ) == [sent]

    def test_empty_target_left_for_payload_validation(self) -> None:
        """A missing/empty target stays untouched —
        ``validate_action_payload``'s existing rejection owns that case."""
        event = _channel_event("group:planning")
        no_target = _send(None)
        empty_target = _send("")
        out = enforce_single_channel_turn(
            event, [no_target, empty_target], agent_id="a1",
        )
        assert out == [no_target, empty_target]
