"""Prose beside a fenced ```json vote block must be preserved — only there.

``parse_actions`` extracts only the fenced JSON block from an LLM
response — historically any prose the model wrote around it was silently
discarded. That loss is worst for the RFC 0030 chair-stall escalation
(``docs/rfcs/0030-amendment-chair-stall-escalation.md``): the
``chair-escalation`` prompt snippet steers the synthesis into the vote
action's ``content`` payload precisely because prose-beside-block was
dropped, but an LLM that disobeys lost the synthesis with no trace — the
room saw a close-vote with no synthesis on the record.

These tests pin the recovery seam AND its scope (PR 610 review): prose
around a block containing an ``END_INTERACTION_VOTE`` is appended as a
``COMPLETE_TASK`` carrying ``payload["result"]``, which
:func:`agents.persona_runtime.channel_reply.synthesize_channel_reply`
promotes into a channel publish on CHANNEL_MESSAGE turns. The vote is
the one action whose *substance* models habitually externalize as
prose; beside any other block shape the prose is overwhelmingly schema
narration ("Here are my actions:") or a narrated decision to stay
silent, and preserving it would let the promotion seam publish
boilerplate — or stamp a post over a deliberate ``do_nothing`` silence,
defeating the ``reply-discretion`` affordance. Non-vote prose therefore
stays dropped, exactly the pre-seam behavior.
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

    def test_one_line_fenced_vote_parses_into_structured_vote(self):
        """Regression for ISSUE-0101 / MT-CHANNEL-GOV-004 Edge Case 2: the
        chair (nova-sparrow) emitted its forced-turn vote as a SINGLE-LINE
        fence — ``` ```json [..] ``` ``` with spaces, not newlines, around the
        body. The prior ``` ```json\\n..\\n``` ``` anchor could not match it,
        so the turn published the literal JSON as channel text with no vote
        metadata; the orchestrator read the missing vote as an ISSUE-0099
        hand-off misfire and re-forced a turn — a visible double-synthesis.
        A one-line fence must parse into a structured END_INTERACTION_VOTE."""
        response = LLMResponse(
            text=(
                '```json [{"action_type": "end_interaction_vote",'
                ' "payload": {"content": "Synthesizing the three risks"}}] ```'
            ),
        )

        actions = parse_actions(response)

        assert len(actions) == 1
        assert actions[0].action_type is ActionType.END_INTERACTION_VOTE
        assert actions[0].payload["content"] == "Synthesizing the three risks"

    def test_one_line_fenced_vote_preserves_surrounding_prose(self):
        """The one-line fence must keep the prose seam too: synthesis prose
        beside a single-line vote fence is still folded back as the trailing
        COMPLETE_TASK, exactly as for the block fence."""
        response = LLMResponse(
            text=(
                "We've converged. "
                '```json [{"action_type": "end_interaction_vote",'
                ' "payload": {"content": "closing"}}] ```'
            ),
        )

        actions = parse_actions(response)

        assert len(actions) == 2
        assert actions[0].action_type is ActionType.END_INTERACTION_VOTE
        assert actions[1].action_type is ActionType.COMPLETE_TASK
        assert actions[1].payload["result"] == "We've converged."

    def test_prose_on_both_sides_is_joined(self):
        response = LLMResponse(
            text=(
                "Summary of the discussion.\n"
                + _fenced(
                    '[{"action_type": "end_interaction_vote",'
                    ' "payload": {"content": "closing"}}]'
                )
                + "\nThanks everyone."
            ),
        )

        actions = parse_actions(response)

        assert len(actions) == 2
        assert actions[1].action_type is ActionType.COMPLETE_TASK
        assert actions[1].payload["result"] == (
            "Summary of the discussion.\n\nThanks everyone."
        )

    def test_prose_beside_non_vote_block_is_dropped(self):
        """The seam's scope (PR 610 review finding 1): prose beside a block
        with no ``END_INTERACTION_VOTE`` stays dropped. A narrated
        ``do_nothing`` ("I don't have anything to add here.") is a
        deliberate silence — preserving the narration would hand
        ``synthesize_channel_reply`` a non-empty ``COMPLETE_TASK`` to
        promote, stamping a publish over the silence the
        ``reply-discretion`` snippet promises the persona it may choose."""
        response = LLMResponse(
            text=(
                "I don't have anything to add here.\n"
                + _fenced('[{"action_type": "do_nothing", "payload": {}}]')
                + "\nStaying out of this one."
            ),
        )

        actions = parse_actions(response)

        assert len(actions) == 1
        assert actions[0].action_type is ActionType.DO_NOTHING

    def test_schema_preamble_beside_send_is_dropped(self):
        """The other common non-vote shape: pure schema-following preamble
        beside an explicit send. Preserved, it would ride along as a
        ``COMPLETE_TASK`` and — on any turn whose send targets a *different*
        channel — get promoted into a boilerplate post on the inbound one."""
        response = LLMResponse(
            text=(
                "Here are my actions:\n"
                + _fenced(
                    '[{"action_type": "send_channel_message",'
                    ' "payload": {"channel_id": "group:other",'
                    ' "content": "cross-post", "mentions": []}}]'
                )
            ),
        )

        actions = parse_actions(response)

        assert len(actions) == 1
        assert actions[0].action_type is ActionType.SEND_CHANNEL_MESSAGE

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
    """The RFC 0030 failure mode this seam exists for: a chair (or a
    concurrer) that writes its synthesis/agreement as prose beside the vote
    block must still get that text onto the channel record. ISSUE-0097
    defect 2 changed *where* it lands: ``fold_prose_into_end_vote`` now folds
    the prose INSIDE the vote ``content`` so the turn is a single publish,
    rather than publishing the prose as a separate message beside the vote
    (the split that dropped a concurring vote out of ``end_vote_window``)."""

    def test_prose_beside_vote_block_folds_into_single_vote(self):
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
        # The synthesis prose now travels INSIDE the vote (prose leads, the
        # vote's own content trails) — ISSUE-0097 defect 2. No separate
        # publish, so the vote is a single turn for the W-window quorum.
        assert sends == []
        assert votes[0].payload["content"] == (
            "Synthesis: we agreed on option B with rollout next week."
            "\n\nclosing — synthesis above"
        )

    def test_explicit_send_plus_narration_does_not_double_publish(self):
        """An explicit ``SEND_CHANNEL_MESSAGE`` with narration beside it:
        the narration is dropped at parse (non-vote block — the seam's
        scope), so nothing reaches the promotion seam and the turn posts
        exactly the explicit reply."""
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
        completes = [
            a for a in actions if a.action_type is ActionType.COMPLETE_TASK
        ]
        assert completes == []

    def test_narrated_do_nothing_stays_silent_on_group_channel(self):
        """End-to-end pin for PR 610 review finding 1: narration beside a
        ``do_nothing`` block on a group channel must produce NO publish.
        Before the scoping fix the narration became a ``COMPLETE_TASK``
        and ``synthesize_channel_reply`` promoted it — posting "I don't
        have anything to add here." to the room over a deliberate
        silence."""
        response = LLMResponse(
            text=(
                "I don't have anything to add here.\n"
                + _fenced('[{"action_type": "do_nothing", "payload": {}}]')
            ),
        )
        event = _channel_event()

        actions = synthesize_channel_reply(
            event, parse_actions(response), agent_id="ember-owl",
        )

        assert [a.action_type for a in actions] == [ActionType.DO_NOTHING]
