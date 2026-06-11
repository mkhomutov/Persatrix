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

import logging
from unittest.mock import AsyncMock, MagicMock

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
    async def test_park_token_is_echoed_not_published(self):
        """The decide-time park stamps a correlation token onto the vote
        action (``VOTE_CLOSE_TOKEN_KEY``); the executor must echo it in
        the result dict — the outcome callback's correlation handle — and
        must NOT leak it onto the wire (the publish metadata carries only
        the vote flag)."""
        publisher = AsyncMock()
        publisher.publish = AsyncMock(return_value=None)
        executor = ActionExecutor(channel_publisher=publisher)

        results = await executor.execute("ember-owl", [
            _vote({"channel_id": "group:planning",
                   "vote_close_token": "tok-1"}),
        ])

        assert results[0]["vote_close_token"] == "tok-1"
        kwargs = publisher.publish.await_args.kwargs
        assert kwargs["metadata"] == {"end_interaction_vote": True}
        assert "tok-1" not in kwargs["content"]

    @pytest.mark.asyncio
    async def test_park_token_rides_every_channel_carrying_status(self):
        """Failure statuses correlate too: a failed publish must consume
        one in-flight slot of the SAME park that stamped it, so every
        status that carries the channel also carries the token."""
        publisher = AsyncMock()
        publisher.publish = AsyncMock(side_effect=RuntimeError("boom"))
        executor = ActionExecutor(channel_publisher=publisher)

        results = await executor.execute("ember-owl", [
            _vote({"channel_id": "group:planning",
                   "vote_close_token": "tok-2"}),
        ])

        assert results[0]["status"] == "failed"
        assert results[0]["vote_close_token"] == "tok-2"

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

    @pytest.mark.asyncio
    async def test_dm_vote_is_dropped_not_published(self):
        """The DM carve-out is a code gate, not prompt prose. The
        ``end-interaction-vote`` snippet says "never vote in a direct
        message — a DM has no group discussion to close", and this repo's
        posture is that DM invariants are enforced in code (the response
        gate forces DM replies; ``synthesize_channel_reply`` carries a
        DM-specific fallback) with the prompt as guidance on top. Without
        this gate an LLM that ignores the snippet publishes a flagged vote
        into the DM and Go's ``processEndVote`` counts it toward a quorum
        (it has no channel-type exemption — ``ct`` is metrics-only), so
        two agents in an agent-agent DM could close the DM's interaction.
        Dropped with a distinct status so the executor result is honest
        about why nothing was published."""
        publisher = AsyncMock()
        publisher.publish = AsyncMock(return_value=None)
        executor = ActionExecutor(channel_publisher=publisher)

        results = await executor.execute("ember-owl", [
            _vote({"channel_id": "dm:ember-owl:alice"}),
        ])

        assert results[0]["status"] == "dm_channel"
        assert results[0]["channel_id"] == "dm:ember-owl:alice"
        publisher.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_whitespace_channel_id_is_no_channel(self):
        """A whitespace-only ``channel_id`` is the no-channel case, not a
        publish target. The bind seam already strips when deciding whether
        to stamp the inbound channel (``bind_end_vote_channel``); without
        the same strip here, a whitespace payload value on a non-channel
        turn slips past the bind (nothing to stamp) AND past the executor's
        emptiness check — publishing the vote into a junk channel instead
        of taking the ``no_channel_id`` drop that exists for exactly this
        "cannot be scoped to an interaction" case."""
        publisher = AsyncMock()
        publisher.publish = AsyncMock(return_value=None)
        executor = ActionExecutor(channel_publisher=publisher)

        results = await executor.execute("ember-owl", [_vote({"channel_id": "  "})])

        assert results[0]["status"] == "no_channel_id"
        publisher.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failure_logs_under_the_module_logger(self, caplog):
        """The carve-out logs under its own ``__name__``
        (``agents.end_vote_action``) — the convention of the executor it
        was carved from and of every sibling module
        (``action_executor``/``dispatch``/``channel_publisher`` all use
        ``getLogger(__name__)``). A hand-written name outside the
        ``agents.*`` hierarchy would silently move the vote's WARN logs
        out of any handler/filter configured for the executor family."""
        publisher = AsyncMock()
        publisher.publish = AsyncMock(side_effect=RuntimeError("boom"))
        executor = ActionExecutor(channel_publisher=publisher)

        with caplog.at_level(logging.WARNING):
            await executor.execute("ember-owl", [
                _vote({"channel_id": "group:planning"}),
            ])

        assert any(
            record.name == "agents.end_vote_action" for record in caplog.records
        ), f"vote failure logged under {sorted({r.name for r in caplog.records})!r}"


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


class TestVotePublishOutcomeCallback:
    """PR 607 review finding 5 — the executor reports the vote publish
    outcome back to the voter (``resolve_end_vote_publish``) so the
    decide-time PARKED local close is discharged: closed on success,
    dropped on failure.  Best-effort: a missing dispatcher/agent or a
    callback error never fails the action."""

    def _executor_with_agent(self, publisher) -> tuple[ActionExecutor, AsyncMock]:
        agent = AsyncMock()
        agent.resolve_end_vote_publish = AsyncMock(return_value=None)
        dispatcher = MagicMock()
        dispatcher.get_agent = MagicMock(return_value=agent)
        executor = ActionExecutor(
            dispatcher=dispatcher, channel_publisher=publisher,
        )
        return executor, agent

    @pytest.mark.asyncio
    async def test_published_outcome_reports_success(self):
        publisher = AsyncMock()
        publisher.publish = AsyncMock(return_value=None)
        executor, agent = self._executor_with_agent(publisher)

        await executor.execute("ember-owl", [
            _vote({"channel_id": "group:planning",
                   "vote_close_token": "tok-1"}),
        ])

        agent.resolve_end_vote_publish.assert_awaited_once_with(
            "group:planning", published=True, token="tok-1",
        )

    @pytest.mark.asyncio
    async def test_unstamped_vote_reports_empty_token(self):
        """A vote the decide-time park never stamped (a threaded turn, a
        DM/thread scope the seam exempts) reports ``token=""`` — the
        discharge treats that as "not my vote" and leaves any parked
        close alone (the stale-park cross-discharge fix)."""
        publisher = AsyncMock()
        publisher.publish = AsyncMock(return_value=None)
        executor, agent = self._executor_with_agent(publisher)

        await executor.execute("ember-owl", [
            _vote({"channel_id": "group:planning"}),
        ])

        agent.resolve_end_vote_publish.assert_awaited_once_with(
            "group:planning", published=True, token="",
        )

    @pytest.mark.asyncio
    async def test_failed_publish_reports_failure(self):
        publisher = AsyncMock()
        publisher.publish = AsyncMock(side_effect=RuntimeError("boom"))
        executor, agent = self._executor_with_agent(publisher)

        await executor.execute("ember-owl", [
            _vote({"channel_id": "group:planning",
                   "vote_close_token": "tok-1"}),
        ])

        agent.resolve_end_vote_publish.assert_awaited_once_with(
            "group:planning", published=False, token="tok-1",
        )

    @pytest.mark.asyncio
    async def test_no_publisher_reports_failure(self):
        """The legacy path publishes nothing — the park must be dropped,
        so the not_implemented result carries the bound channel and the
        callback reports a non-publish."""
        executor, agent = self._executor_with_agent(None)

        results = await executor.execute("ember-owl", [
            _vote({"channel_id": "group:planning",
                   "vote_close_token": "tok-1"}),
        ])

        assert results[0]["status"] == "not_implemented"
        agent.resolve_end_vote_publish.assert_awaited_once_with(
            "group:planning", published=False, token="tok-1",
        )

    @pytest.mark.asyncio
    async def test_channel_less_drop_skips_callback(self):
        """``no_channel_id`` has no channel to discharge — nothing was
        parked for it either (the decide-time gates mirror the executor's)."""
        publisher = AsyncMock()
        publisher.publish = AsyncMock(return_value=None)
        executor, agent = self._executor_with_agent(publisher)

        await executor.execute("ember-owl", [_vote({})])

        agent.resolve_end_vote_publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_agent_is_tolerated(self):
        publisher = AsyncMock()
        publisher.publish = AsyncMock(return_value=None)
        dispatcher = MagicMock()
        dispatcher.get_agent = MagicMock(return_value=None)
        executor = ActionExecutor(
            dispatcher=dispatcher, channel_publisher=publisher,
        )

        results = await executor.execute("ember-owl", [
            _vote({"channel_id": "group:planning"}),
        ])

        assert results[0]["status"] == "published"

    @pytest.mark.asyncio
    async def test_callback_error_does_not_fail_the_action(self, caplog):
        publisher = AsyncMock()
        publisher.publish = AsyncMock(return_value=None)
        executor, agent = self._executor_with_agent(publisher)
        agent.resolve_end_vote_publish = AsyncMock(
            side_effect=RuntimeError("tracker exploded"),
        )

        with caplog.at_level(logging.WARNING, logger="agents.action_executor"):
            results = await executor.execute("ember-owl", [
                _vote({"channel_id": "group:planning"}),
            ])

        assert results[0]["status"] == "published"
        assert any(
            "publish-outcome callback failed" in r.message for r in caplog.records
        )
