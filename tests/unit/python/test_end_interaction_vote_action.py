"""The RFC 0030 Layer 4 vote producer — END_INTERACTION_VOTE end-to-end.

Producer plan PR 2 (``docs/rfcs/0030-interaction-id-producer-pr-plan.md``,
IP6), TDD-first: this matrix was written red against the planned executor
and binding API.

* The executor publishes the vote as a REAL channel message (the landed
  ``processEndVote`` runs post-persistence) with ``end_interaction_vote:
  true`` merged into the publish metadata — the key the Go reader
  (``readEndInteractionVote``) counts toward the quorum, scoped to the
  router's own resolved interaction (IP2), never a publisher claim.
* The vote needs a channel: the action-loop seam stamps the inbound
  channel onto a vote that omits ``channel_id``
  (:func:`agents.persona_runtime.channel_reply.bind_end_vote_channel`,
  the ``synthesize_channel_reply`` posture), so a persona can vote
  without echoing routing details.
* The legacy in-process path stays ``not_implemented`` — votes are a
  channels-governance concept; the chat path has no interaction router.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agents.action_executor import ActionExecutor
from agents.persona_runtime.channel_reply import bind_end_vote_channel
from agents.persona_types import ActionType, AgentAction, AgentEvent, EventType


def _vote(payload: dict | None = None) -> AgentAction:
    return AgentAction(ActionType.END_INTERACTION_VOTE, dict(payload or {}))


def _channel_event(channel_id: str = "group:planning") -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": "hi", "mentions": []},
        channel_id=channel_id,
        sender_id="alice",
    )


class TestExecutorPublishesVote:
    @pytest.mark.asyncio
    async def test_vote_publishes_flagged_message(self):
        """The vote is a real publish carrying the wire flag the Go
        accumulator counts, an empty mentions list (a vote addresses the
        room's process, not a member), and the caller's cascade_depth
        verbatim (the +1 lives on the dispatcher, the send-branch posture)."""
        publisher = AsyncMock()
        publisher.publish = AsyncMock(return_value=None)
        executor = ActionExecutor(channel_publisher=publisher)

        results = await executor.execute("ember-owl", [
            _vote({"channel_id": "group:planning"}),
        ], cascade_depth=3)

        assert results[0]["status"] == "published"
        assert results[0]["action_type"] == "end_interaction_vote"
        assert results[0]["channel_id"] == "group:planning"
        publisher.publish.assert_awaited_once()
        kwargs = publisher.publish.await_args.kwargs
        assert kwargs["channel_id"] == "group:planning"
        assert kwargs["sender_id"] == "ember-owl"
        assert kwargs["mentions"] == []
        assert kwargs["cascade_depth"] == 3
        assert kwargs["metadata"] == {"end_interaction_vote": True}
        assert kwargs["content"].strip(), "the vote carries a readable sign-off"

    @pytest.mark.asyncio
    async def test_vote_content_payload_honoured(self):
        """A persona-supplied sign-off rides the vote message verbatim."""
        publisher = AsyncMock()
        publisher.publish = AsyncMock(return_value=None)
        executor = ActionExecutor(channel_publisher=publisher)

        await executor.execute("ember-owl", [
            _vote({"channel_id": "group:planning",
                   "content": "Nothing further from me — I support option B."}),
        ])

        kwargs = publisher.publish.await_args.kwargs
        assert kwargs["content"] == "Nothing further from me — I support option B."

    @pytest.mark.asyncio
    async def test_no_publisher_stays_not_implemented(self):
        """The legacy in-process path keeps the pre-producer posture: votes
        are a channels-governance concept and the chat path has no
        interaction router."""
        executor = ActionExecutor(channel_publisher=None)

        results = await executor.execute("ember-owl", [
            _vote({"channel_id": "group:planning"}),
        ])

        assert results[0]["status"] == "not_implemented"

    @pytest.mark.asyncio
    async def test_missing_channel_is_a_distinct_no_op(self):
        """A vote with no channel (the bind seam never fired — e.g. a TICK
        turn emitted it) cannot be scoped to an interaction: dropped with a
        distinct status, never published."""
        publisher = AsyncMock()
        publisher.publish = AsyncMock(return_value=None)
        executor = ActionExecutor(channel_publisher=publisher)

        results = await executor.execute("ember-owl", [_vote({})])

        assert results[0]["status"] == "no_channel_id"
        publisher.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_publish_failure_is_reported_not_raised(self):
        """The send-branch error posture: a failed publish surfaces as a
        structured status, never an exception out of the executor."""
        publisher = AsyncMock()
        publisher.publish = AsyncMock(side_effect=RuntimeError("boom"))
        executor = ActionExecutor(channel_publisher=publisher)

        results = await executor.execute("ember-owl", [
            _vote({"channel_id": "group:planning"}),
        ])

        assert results[0]["status"] == "failed"


class TestBindEndVoteChannel:
    def test_channel_event_stamps_inbound_channel(self):
        """A vote emitted during a channel turn without routing details is
        bound to the inbound channel — the persona votes on the conversation
        it is in, the synthesize_channel_reply posture."""
        actions = [_vote({})]
        bound = bind_end_vote_channel(_channel_event(), actions)
        assert bound[0].payload["channel_id"] == "group:planning"

    def test_explicit_channel_preserved(self):
        actions = [_vote({"channel_id": "group:design"})]
        bound = bind_end_vote_channel(_channel_event(), actions)
        assert bound[0].payload["channel_id"] == "group:design"

    def test_non_channel_event_left_alone(self):
        """A TICK-emitted vote has no inbound channel to bind; the executor's
        no_channel_id status is the backstop."""
        event = AgentEvent(event_type=EventType.TICK, payload={})
        actions = [_vote({})]
        bound = bind_end_vote_channel(event, actions)
        assert "channel_id" not in bound[0].payload

    def test_other_actions_untouched(self):
        actions = [
            AgentAction(ActionType.SEND_CHANNEL_MESSAGE,
                        {"channel_id": "group:planning", "content": "hi"}),
            _vote({}),
        ]
        bound = bind_end_vote_channel(_channel_event(), actions)
        assert bound[0].action_type is ActionType.SEND_CHANNEL_MESSAGE
        assert bound[1].payload["channel_id"] == "group:planning"


class TestVotePromptVocabulary:
    def test_snippet_teaches_the_vote_action(self):
        """The prompt half of Layer 4's social contract: the persona system
        prompt carries the vote vocabulary — the exact action_type literal
        the parser recognises and the quorum framing."""
        from agents.prompt_loader import load_snippet

        snippet = load_snippet("end-interaction-vote")
        assert "end_interaction_vote" in snippet, (
            "the snippet must show the exact action_type literal the parser accepts"
        )

    def test_assembler_loads_the_snippet(self):
        """The snippet is wired into the persona system prompt next to its
        reply-discretion sibling (unconditional, with the group-channel
        framing carried inline)."""
        from pathlib import Path

        src = Path("agents/persona_runtime/prompt_assembly.py").read_text(encoding="utf-8")
        assert 'load_snippet("end-interaction-vote")' in src
