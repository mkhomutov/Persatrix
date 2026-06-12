"""Prose surrounding a fenced ```json action block must be preserved.

``parse_actions`` extracts only the fenced JSON block from an LLM
response — historically any prose the model wrote around it was silently
discarded. That loss is worst for the RFC 0030 chair-stall escalation
(``docs/rfcs/0030-amendment-chair-stall-escalation.md``): the
``chair-escalation`` prompt snippet steers the synthesis into the vote
action's ``content`` payload precisely because prose-beside-block was
dropped, but an LLM that disobeys lost the synthesis with no trace — the
room saw a close-vote with no synthesis on the record.

These tests pin the recovery seam: non-empty prose around the block is
appended as a ``COMPLETE_TASK`` carrying ``payload["result"]``, which
:func:`agents.persona_runtime.channel_reply.synthesize_channel_reply`
promotes into a channel publish on CHANNEL_MESSAGE turns. The promotion
is a no-op when the block already contains a ``SEND_CHANNEL_MESSAGE``
for the inbound channel, so explicit-publish turns never double-post.
"""

from __future__ import annotations

from agents.llm_client import LLMResponse
from agents.persona_runtime.action_parser import parse_actions
from agents.persona_runtime.channel_reply import synthesize_channel_reply
from agents.persona_types import ActionType, AgentEvent, EventType

# ─── parse_actions: prose preservation ─────────────────────


def _fenced(actions_json: str) -> str:
    return f"```json\n{actions_json}\n```"


class TestParseActionsSurroundingProse:
    def test_prose_before_block_is_appended_as_complete_task(self):
        response = LLMResponse(
            text=(
                "We've converged; closing it out.\n"
                + _fenced(
                    '[{"action_type": "end_interaction_vote",'
                    ' "payload": {"content": "closing"}}]'
                )
            ),
        )

        actions = parse_actions(response)

        assert len(actions) == 2
        assert actions[0].action_type is ActionType.END_INTERACTION_VOTE
        assert actions[1].action_type is ActionType.COMPLETE_TASK
        assert actions[1].payload["result"] == "We've converged; closing it out."

    def test_prose_on_both_sides_is_joined(self):
        response = LLMResponse(
            text=(
                "Summary of the discussion.\n"
                + _fenced('[{"action_type": "do_nothing", "payload": {}}]')
                + "\nThanks everyone."
            ),
        )

        actions = parse_actions(response)

        assert len(actions) == 2
        assert actions[1].action_type is ActionType.COMPLETE_TASK
        assert actions[1].payload["result"] == (
            "Summary of the discussion.\n\nThanks everyone."
        )

    def test_whitespace_only_remainder_stays_dropped(self):
        """A block wrapped in nothing but newlines/spaces must not grow a
        ghost ``COMPLETE_TASK`` — empty narration is not narration, and a
        whitespace ``result`` would still be skipped by the synthesiser's
        ``strip()`` guard anyway. Keep the parse output minimal."""
        response = LLMResponse(
            text="  \n" + _fenced('[{"action_type": "do_nothing", "payload": {}}]') + "\n  ",
        )

        actions = parse_actions(response)

        assert len(actions) == 1
        assert actions[0].action_type is ActionType.DO_NOTHING

    def test_all_actions_skipped_keeps_full_text_fallback(self):
        """When every parsed action is dropped (unknown action_type), the
        pre-existing fallback — one ``COMPLETE_TASK`` carrying the FULL raw
        text — must win; the prose seam must not produce a second,
        prose-only ``COMPLETE_TASK`` beside it."""
        raw = "Narration.\n" + _fenced('[{"action_type": "fly_to_moon", "payload": {}}]')
        response = LLMResponse(text=raw)

        actions = parse_actions(response)

        assert len(actions) == 1
        assert actions[0].action_type is ActionType.COMPLETE_TASK
        assert actions[0].payload["result"] == raw


# ─── Integration with synthesize_channel_reply ─────────────


def _channel_event(channel_id: str = "group:planning") -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={
            "content": "status?",
            "channel_type": "group",
            "mentions": ["ember-owl"],
            "respond_policy": "always",
        },
        channel_id=channel_id,
        sender_id="alice",
        message_id="msg-1",
        metadata={"sender_participant_type": "user"},
    )


class TestProseSynthesisIntegration:
    """The RFC 0030 failure mode this seam exists for: a chair that
    disobeys the ``chair-escalation`` snippet and writes its synthesis
    as prose beside the vote block must still get the synthesis onto
    the channel record, alongside the vote."""

    def test_prose_beside_vote_block_publishes_prose_and_keeps_vote(self):
        response = LLMResponse(
            text=(
                "Synthesis: we agreed on option B with rollout next week.\n"
                + _fenced(
                    '[{"action_type": "end_interaction_vote",'
                    ' "payload": {"content": "closing — synthesis above"}}]'
                )
            ),
        )
        event = _channel_event()

        actions = synthesize_channel_reply(
            event, parse_actions(response), agent_id="ember-owl",
        )

        votes = [
            a for a in actions if a.action_type is ActionType.END_INTERACTION_VOTE
        ]
        sends = [
            a for a in actions if a.action_type is ActionType.SEND_CHANNEL_MESSAGE
        ]
        assert len(votes) == 1
        # bind_end_vote_channel stamps the inbound channel onto the vote.
        assert votes[0].payload["channel_id"] == "group:planning"
        assert len(sends) == 1
        assert sends[0].payload["channel_id"] == "group:planning"
        assert sends[0].payload["content"] == (
            "Synthesis: we agreed on option B with rollout next week."
        )

    def test_explicit_send_plus_narration_does_not_double_publish(self):
        """An explicit ``SEND_CHANNEL_MESSAGE`` for the inbound channel
        suppresses promotion — the narration ``COMPLETE_TASK`` rides along
        unpublished rather than turning into a second post."""
        response = LLMResponse(
            text=(
                "Posting my reply now.\n"
                + _fenced(
                    '[{"action_type": "send_channel_message",'
                    ' "payload": {"channel_id": "group:planning",'
                    ' "content": "explicit reply", "mentions": []}}]'
                )
            ),
        )
        event = _channel_event()

        actions = synthesize_channel_reply(
            event, parse_actions(response), agent_id="ember-owl",
        )

        sends = [
            a for a in actions if a.action_type is ActionType.SEND_CHANNEL_MESSAGE
        ]
        assert len(sends) == 1
        assert sends[0].payload["content"] == "explicit reply"
        # The narration is still carried (legacy chat priority-3 surface),
        # just never promoted to a publish on this turn.
        completes = [
            a for a in actions if a.action_type is ActionType.COMPLETE_TASK
        ]
        assert len(completes) == 1
        assert completes[0].payload["result"] == "Posting my reply now."
