"""ISSUE-0097 defect 2 — fold a turn's free-text into its END_INTERACTION_VOTE.

A single LLM turn that emits an agreement/closing free-text block *plus* an
``end_interaction_vote`` action block is parsed into two actions: the prose
(an explicit ``SEND_CHANNEL_MESSAGE`` for the channel, or — for a persona that
replied conversationally around a ```json fence — a ``COMPLETE_TASK`` that the
ISSUE-0048 synthesis would promote into its own publish) and the vote. The
executor publishes each as a *separate* channel message.

That split is the defect. The RFC 0030 Layer 4 quorum counts published
messages as turns (``internal/channels/end_vote.go``: ``state.turn++`` per
publish, window check ``state.turn - voteTurn < w``). A split costs one extra
turn, so a concurring vote that trails its own prose lands one position
further from the chair's vote and falls outside ``end_vote_window`` — the
04:04:01Z miss this issue captured. Prompt steering (PR 2) could not fix it:
the split is structural — a turn's free-text and its action block persist as
two messages regardless of what the snippet says.

``fold_prose_into_end_vote`` closes that gap structurally: when a turn carries
a *group-channel* END_INTERACTION_VOTE, the sibling free-text is folded INTO
the vote's ``content`` and the separate prose action is dropped, so the vote
travels as ONE publish — exactly the single-message shape the chair path
already produces. The fold is gated to group channels because that is the only
place the vote both publishes (the executor drops DM votes) and participates
in a quorum; on a DM the prose must keep its own publish (the DM-must-reply
invariant, ISSUE-0048).
"""

from __future__ import annotations

from agents.persona_runtime.channel_reply import (
    fold_prose_into_end_vote,
    synthesize_channel_reply,
)
from agents.persona_types import ActionType, AgentAction, AgentEvent, EventType


def _group_event(
    *,
    channel_id: str = "group:planning",
    sender_id: str = "nova-sparrow",
) -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={
            "content": "Let's converge — vote to close if you concur.",
            "channel_type": "group",
            "mentions": [],
            "respond_policy": "when_mentioned",
        },
        channel_id=channel_id,
        sender_id=sender_id,
        message_id="msg-chair",
    )


def _vote(channel_id: str = "group:planning", content: str = "") -> AgentAction:
    return AgentAction(
        action_type=ActionType.END_INTERACTION_VOTE,
        payload={"channel_id": channel_id, "content": content},
    )


def _send(channel_id: str, content: str) -> AgentAction:
    return AgentAction(
        action_type=ActionType.SEND_CHANNEL_MESSAGE,
        payload={"channel_id": channel_id, "content": content, "mentions": []},
    )


def _complete(result: str) -> AgentAction:
    return AgentAction(
        action_type=ActionType.COMPLETE_TASK,
        payload={"result": result},
    )


# ─── Pure helper: fold_prose_into_end_vote ─────────────────


class TestFoldProseIntoEndVote:
    """The free-text travels INSIDE the vote — one message, never two."""

    def test_complete_task_prose_folds_into_empty_vote(self):
        """The live defect: a conversational concurrer emits agreement prose
        (parsed to COMPLETE_TASK) beside an empty-content vote. The prose must
        move into the vote ``content`` and the COMPLETE_TASK must be dropped,
        so only the vote remains to publish.
        """
        event = _group_event()
        actions = [_vote(), _complete("I agree — let's close it out.")]

        result = fold_prose_into_end_vote(event, actions)

        votes = [a for a in result if a.action_type is ActionType.END_INTERACTION_VOTE]
        assert len(votes) == 1
        assert votes[0].payload["content"] == "I agree — let's close it out."
        # No surviving free-text sibling that could become a second publish.
        assert all(
            a.action_type is not ActionType.COMPLETE_TASK for a in result
        )
        assert len(result) == 1

    def test_explicit_send_prose_folds_into_empty_vote(self):
        """A well-prompted agent that emits an explicit SEND_CHANNEL_MESSAGE
        for the channel beside the vote splits exactly the same way — fold
        the send's content into the vote and drop the send.
        """
        event = _group_event()
        actions = [
            _send("group:planning", "Agreed, nothing further from me."),
            _vote(),
        ]

        result = fold_prose_into_end_vote(event, actions)

        assert len(result) == 1
        assert result[0].action_type is ActionType.END_INTERACTION_VOTE
        assert result[0].payload["content"] == "Agreed, nothing further from me."

    def test_vote_with_own_content_keeps_both_prose_leads(self):
        """When the vote already carries content AND there is sibling prose,
        nothing is lost: the prose leads, the vote's own content trails, joined
        as one block inside the single vote message.
        """
        event = _group_event()
        actions = [_vote(content="Closing remark."), _complete("I concur.")]

        result = fold_prose_into_end_vote(event, actions)

        assert len(result) == 1
        assert result[0].payload["content"] == "I concur.\n\nClosing remark."

    def test_clean_single_message_vote_is_untouched(self):
        """The chair path already emits synthesis-in-vote with no sibling
        prose — the fold is a no-op there.
        """
        event = _group_event()
        actions = [_vote(content="Synthesis: we ship Tuesday. I vote to close.")]

        result = fold_prose_into_end_vote(event, actions)

        assert result == actions

    def test_no_vote_is_passthrough(self):
        """A turn with no vote is the ISSUE-0048 synthesis domain — fold must
        leave it entirely alone.
        """
        event = _group_event()
        actions = [_complete("Just a reply, no vote.")]

        result = fold_prose_into_end_vote(event, actions)

        assert result == actions

    def test_dm_vote_is_not_folded(self):
        """A vote on a DM is dropped by the executor (``end_vote_action.py``);
        folding the prose into it would lose the reply and 504 the DM-must-
        reply round-trip. The prose keeps its own publish on DMs.
        """
        event = _group_event(channel_id="dm:alice:ember-owl")
        actions = [_vote(channel_id="dm:alice:ember-owl"), _complete("Bye.")]

        result = fold_prose_into_end_vote(event, actions)

        assert result == actions

    def test_send_to_other_channel_is_not_consumed(self):
        """A cross-posted SEND to a *different* channel is not this vote's
        free-text — it must survive untouched, and the vote stays empty.
        """
        event = _group_event()
        actions = [
            _send("group:other", "fyi over there"),
            _vote(),
        ]

        result = fold_prose_into_end_vote(event, actions)

        # The cross-post survives; the vote is unchanged (no same-channel prose).
        assert result == actions

    def test_empty_prose_does_not_fire_fold(self):
        """A whitespace-only COMPLETE_TASK beside a vote carries nothing to
        fold — leave the list unchanged rather than blanking the vote content.
        """
        event = _group_event()
        actions = [_vote(content="vote remark"), _complete("   \n ")]

        result = fold_prose_into_end_vote(event, actions)

        assert result == actions


# ─── Integration: synthesize_channel_reply runs the fold ───


class TestSynthesizeRunsFold:
    """``synthesize_channel_reply`` must fold before it would promote prose
    into a separate publish — so the conversational concurrence case ends as a
    single vote action, not a SEND + vote pair.
    """

    def test_conversational_concurrence_collapses_to_one_publish(self):
        """End-to-end of the seam: an unbound vote (channel stamped by
        ``bind_end_vote_channel``) plus a COMPLETE_TASK agreement must come out
        as exactly one vote action carrying the agreement — no synthesised
        SEND_CHANNEL_MESSAGE.
        """
        event = _group_event()
        actions = [
            AgentAction(
                action_type=ActionType.END_INTERACTION_VOTE,
                payload={"content": ""},  # channel-less: bind stamps it
            ),
            _complete("I agree with the synthesis."),
        ]

        result = synthesize_channel_reply(event, actions, agent_id="ember-owl")

        sends = [a for a in result if a.action_type is ActionType.SEND_CHANNEL_MESSAGE]
        votes = [a for a in result if a.action_type is ActionType.END_INTERACTION_VOTE]
        assert sends == []
        assert len(votes) == 1
        assert votes[0].payload["channel_id"] == "group:planning"
        assert votes[0].payload["content"] == "I agree with the synthesis."

    def test_non_vote_reply_still_synthesizes(self):
        """Regression guard: with no vote, the ISSUE-0048 promotion of a
        conversational COMPLETE_TASK into a SEND_CHANNEL_MESSAGE is unchanged.
        """
        event = _group_event()
        actions = [_complete("Here's my take on the risk.")]

        result = synthesize_channel_reply(event, actions, agent_id="ember-owl")

        sends = [a for a in result if a.action_type is ActionType.SEND_CHANNEL_MESSAGE]
        assert len(sends) == 1
        assert sends[0].payload["content"] == "Here's my take on the risk."
        assert sends[0].payload["channel_id"] == "group:planning"
