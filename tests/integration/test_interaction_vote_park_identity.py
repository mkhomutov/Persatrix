"""PR 607 second-pass review — park/discharge identity + late delivery.

Split out of :mod:`test_interaction_channel_close_propagation` (the
vote / rotation propagation seams) for the 500-line cap, the
``test_interaction_close_cause_labels`` precedent.  Two defence seams:

* **Park/discharge identity** — the vote-close park is correlated to
  the votes it covers by a stamped token
  (:data:`agents.persona_types.VOTE_CLOSE_TOKEN_KEY`) plus an in-flight
  count (``agents/persona_runtime/vote_close.py``), so a publish
  outcome can only discharge the park that stamped its action.  This
  pins the two confirmed cross-fires of the old channel-only key: a
  failed duplicate consuming the park its successful sibling still
  needed, and a stranded park discharged by a later (threaded) vote's
  outcome.  It also pins the re-vote dedup mirror: Go counts a
  participant once per interaction, so a deduped re-vote (still a 2xx
  publish) must not mint a second "ended" local record.

* **Late-delivery defence** — Go's fanout gives no cross-publish
  per-recipient ordering, so a straggler message of the RETIRED
  interaction can arrive after the successor's first message; the
  successor's record knows its predecessor's wire id
  (``Interaction.predecessor_wire_id``) and
  ``wire_rotation_closes`` reads a matching id as "late", never as a
  second rotation.

Shared persona config / event builders live in
:mod:`_interaction_multi_turn_helpers`.
"""

from __future__ import annotations

import pytest

from agents.clock import FrozenClock
from agents.memory.interactions import (
    REASON_CATCHUP_COMPLETE,
    REASON_STRUCTURAL,
    scope_for_group,
)
from agents.persona_types import VOTE_CLOSE_TOKEN_KEY
from agents.tools.registry import clear_registry

from ._interaction_multi_turn_helpers import (
    GROUP_CHANNEL,
    all_episodes,
    channel_event,
    discharge_vote,
    make_agent_with_clock,
)
from ._interaction_multi_turn_helpers import (
    close_reasons as _close_reasons,
)
from ._interaction_multi_turn_helpers import (
    vote_action as vote,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


CHANNEL = GROUP_CHANNEL
SCOPE = scope_for_group(CHANNEL)
THREAD_ID = "t-veto"


@pytest.mark.asyncio
class TestVoteParkIdentity:
    """The park is correlated to the votes it covers by a stamped token
    plus an in-flight count, so a publish outcome can only discharge the
    park that stamped its action — outcomes of other votes (an earlier
    stranded park, a threaded vote the seam exempts) cannot cross-fire,
    and a failed duplicate cannot consume the park a successful sibling
    still needs."""

    async def test_failed_then_successful_duplicate_votes_still_close(self):
        # One decided turn can carry duplicate END_INTERACTION_VOTE
        # actions (the parser does not dedup).  The executor publishes
        # them sequentially; if the first fails and the second lands,
        # a vote IS on the wire — the local record must still close.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        votes = [vote(), vote()]
        await agent._store_event_episode(
            channel_event("done here", wire_id="wire-A"), votes,
        )
        token = votes[0].payload[VOTE_CLOSE_TOKEN_KEY]
        assert votes[1].payload[VOTE_CLOSE_TOKEN_KEY] == token
        await agent.resolve_end_vote_publish(
            CHANNEL, published=False, token=token,
        )
        # The failure consumed one in-flight slot, not the park itself.
        assert CHANNEL in agent._pending_vote_closes
        await agent.resolve_end_vote_publish(
            CHANNEL, published=True, token=token,
        )
        assert agent._interaction_tracker.get(SCOPE, speaker_id="alex") is None
        episodes = await all_episodes(agent)
        assert _close_reasons(episodes) == [REASON_STRUCTURAL]

    async def test_all_duplicate_publishes_failing_drops_park(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        votes = [vote(), vote()]
        await agent._store_event_episode(
            channel_event("done here", wire_id="wire-A"), votes,
        )
        token = votes[0].payload[VOTE_CLOSE_TOKEN_KEY]
        await agent.resolve_end_vote_publish(
            CHANNEL, published=False, token=token,
        )
        await agent.resolve_end_vote_publish(
            CHANNEL, published=False, token=token,
        )
        assert CHANNEL not in agent._pending_vote_closes
        # A stray late success for the same turn finds nothing.
        await agent.resolve_end_vote_publish(
            CHANNEL, published=True, token=token,
        )
        open_interaction = agent._interaction_tracker.get(SCOPE, speaker_id="alex")
        assert open_interaction is not None
        assert open_interaction.is_open
        assert await all_episodes(agent) == []

    async def test_threaded_vote_outcome_does_not_discharge_stale_park(self):
        # A park can be STRANDED (the publish coroutine was cancelled
        # before the outcome callback ran — event-timeout, loop stop).
        # A later vote decided on a THREADED turn in the same channel
        # writes no park (thread scope) and its action is never stamped;
        # ``bind_end_vote_channel`` still publishes it to the parent
        # channel.  Its success outcome must NOT pop the stale park and
        # close the floor record on the strength of one threaded vote.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("wrap the floor?", wire_id="wire-A"), [vote()],
        )
        assert CHANNEL in agent._pending_vote_closes  # stranded from here on
        thread_vote = vote()
        await agent._store_event_episode(
            channel_event("thread aside", thread_id=THREAD_ID),
            [thread_vote],
        )
        assert VOTE_CLOSE_TOKEN_KEY not in thread_vote.payload
        await agent.resolve_end_vote_publish(CHANNEL, published=True, token="")
        floor = agent._interaction_tracker.get(SCOPE, speaker_id="alex")
        assert floor is not None
        assert floor.is_open
        assert await all_episodes(agent) == []

    async def test_revote_on_same_wire_interaction_does_not_fragment(self):
        # Go's vote gate dedupes a re-vote per (participant, interaction)
        # but the suppressed duplicate still commits → REST 2xx → status
        # "published".  The voter's mirror must not mint a second "ended"
        # record for a wire interaction it already vote-closed.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        first = vote()
        await agent._store_event_episode(
            channel_event("vote 1", wire_id="wire-A"), [first],
        )
        await agent.resolve_end_vote_publish(
            CHANNEL, published=True, token=first.payload[VOTE_CLOSE_TOKEN_KEY],
        )
        assert len(await all_episodes(agent)) == 1
        # No quorum (one voter < K): members keep talking under wire-A
        # and the voter's scope reopens stamped with the SAME wire id.
        await agent._store_event_episode(
            channel_event("still going", wire_id="wire-A"), [],
        )
        reopened = agent._interaction_tracker.get(SCOPE, speaker_id="alex")
        assert reopened is not None
        assert reopened.wire_interaction_id == "wire-A"
        second = vote()
        await agent._store_event_episode(
            channel_event("vote 2", wire_id="wire-A"), [second],
        )
        await agent.resolve_end_vote_publish(
            CHANNEL, published=True, token=second.payload[VOTE_CLOSE_TOKEN_KEY],
        )
        # Still exactly one "ended" record; the reopened scope stays
        # open, mirroring Go (interaction A never closed).
        assert len(await all_episodes(agent)) == 1
        still_open = agent._interaction_tracker.get(SCOPE, speaker_id="alex")
        assert still_open is not None
        assert still_open.is_open

    async def test_synthesis_deferral_still_memos_the_counted_vote(self):
        """PR #846 review: the re-vote memo records what GO COUNTED, so it
        must be written when the discharge confirms the publish — not
        gated on the fan happening to close a record.

        The synthesis-reply leg is the concurrency-free route into the
        gap: it pops the park and closes nothing, deferring to the
        close-notification self-echo.  When Go DEMOTED that vote (the arm
        was consumed, or an operator raised ``max_rounds``) no
        notification arrives and the wire id never rotates — so the next
        in-window vote, which Go dedups but still commits 2xx →
        "published", used to find an empty memo, pass the anchor test and
        bury the live record as "ended" plus a summariser call.  That is
        the record PR #718's carve-out exists to keep open."""
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        first = vote()
        await agent._store_event_episode(
            channel_event("synthesis + vote", wire_id="wire-A"), [first],
        )
        await agent.resolve_end_vote_publish(
            CHANNEL, published=True,
            token=first.payload[VOTE_CLOSE_TOKEN_KEY],
            synthesis_reply=True,
        )
        deferred = agent._interaction_tracker.get(SCOPE, speaker_id="alex")
        assert deferred is not None and deferred.is_open, (
            "the synthesis deferral leaves the record open for the self-echo"
        )
        assert await all_episodes(agent) == []
        # Go demoted the vote: no self-echo, no rotation, discussion live.
        await agent._store_event_episode(
            channel_event("still going", wire_id="wire-A"), [],
        )
        second = vote()
        await agent._store_event_episode(
            channel_event("vote 2", wire_id="wire-A"), [second],
        )
        await agent.resolve_end_vote_publish(
            CHANNEL, published=True,
            token=second.payload[VOTE_CLOSE_TOKEN_KEY],
        )
        still_open = agent._interaction_tracker.get(SCOPE, speaker_id="alex")
        assert still_open is not None and still_open.is_open, (
            "Go deduped the re-vote on wire-A; the live record must stay open"
        )
        assert await all_episodes(agent) == []

    async def test_vote_after_real_rotation_closes_normally(self):
        # The re-vote memory must not over-suppress: once the channel
        # genuinely rotates, a vote on the successor closes it.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        first = vote()
        await agent._store_event_episode(
            channel_event("vote 1", wire_id="wire-A"), [first],
        )
        await agent.resolve_end_vote_publish(
            CHANNEL, published=True, token=first.payload[VOTE_CLOSE_TOKEN_KEY],
        )
        second = vote()
        await agent._store_event_episode(
            channel_event(
                "next topic, wrap again", wire_id="wire-B",
                prev_id="wire-A", prev_trigger="end_votes",
            ),
            [second],
        )
        await agent.resolve_end_vote_publish(
            CHANNEL, published=True, token=second.payload[VOTE_CLOSE_TOKEN_KEY],
        )
        episodes = await all_episodes(agent)
        assert len(episodes) == 2
        assert _close_reasons(episodes) == [REASON_STRUCTURAL, REASON_STRUCTURAL]


@pytest.mark.asyncio
class TestLateDeliveryDefence:
    """Go's fanout gives no cross-publish per-recipient ordering (one
    detached goroutine per publish, fresh dial per dispatch), so the
    successor's first message can arrive BEFORE the retired
    interaction's last message.  The straggler must not split the fresh
    record into close/reopen/re-close fragments: the successor's record
    knows its predecessor's id from the wire pair, and a wire id equal
    to it is a late delivery, not a rotation."""

    async def test_late_predecessor_message_does_not_split_successor(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("old topic", wire_id="wire-A"), [],
        )
        await agent._store_event_episode(
            channel_event(
                "new topic", wire_id="wire-B",
                prev_id="wire-A", prev_trigger="end_votes",
            ),
            [],
        )
        assert len(await all_episodes(agent)) == 1  # the genuine rotation
        await agent._store_event_episode(
            channel_event("late straggler from A", wire_id="wire-A"), [],
        )
        open_interaction = agent._interaction_tracker.get(SCOPE, speaker_id="alex")
        assert open_interaction is not None
        assert open_interaction.is_open
        assert open_interaction.wire_interaction_id == "wire-B"
        assert open_interaction.turn_count == 2  # B's turn + the straggler
        assert len(await all_episodes(agent)) == 1
        # Subsequent successor traffic is unaffected.
        await agent._store_event_episode(
            channel_event("more new topic", wire_id="wire-B"), [],
        )
        assert len(await all_episodes(agent)) == 1
        survivor = agent._interaction_tracker.get(SCOPE, speaker_id="alex")
        assert survivor is not None
        assert survivor.turn_count == 3

    async def test_missed_generation_still_closes(self):
        # The defence must not over-suppress: a record opened under
        # wire-A whose channel is two rotations on (wire-C arrives,
        # attributing wire-B's close) DID end — the differing id is
        # neither the record's own nor its predecessor, so it closes.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event(
                "topic B", wire_id="wire-B",
                prev_id="wire-A", prev_trigger="end_votes",
            ),
            [],
        )
        await agent._store_event_episode(
            channel_event(
                "topic C", wire_id="wire-C",
                prev_id="wire-B", prev_trigger="idle",
            ),
            [],
        )
        assert len(await all_episodes(agent)) == 1
        fresh = agent._interaction_tracker.get(SCOPE, speaker_id="alex")
        assert fresh is not None
        assert fresh.wire_interaction_id == "wire-C"

    async def test_discharge_spares_a_successor_opened_after_the_park(self):
        """PR #846 re-review: the fan must not bury a record opened AFTER
        the vote was parked.

        The parked record closes inline between decide and publish and
        its speaker resumes on the still-unrotated wire id, so the fresh
        record carries the anchor and sailed through the wire-id test —
        buried as a phantom 1-turn "ended" episode.  The old identity
        guard refused this, but refused the true siblings with it; the
        decide-time id capture keeps both halves."""
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("robin's point", wire_id="wire-A", sender="robin"), [],
        )
        await agent._store_event_episode(
            channel_event("wrap it up?", wire_id="wire-A"), [vote()],
        )
        parked = agent._interaction_tracker.get(SCOPE, speaker_id="alex")
        assert parked is not None
        agent._interaction_tracker.close_record(
            parked, reason=REASON_STRUCTURAL,
        )
        # alex resumes on the SAME (unrotated) wire id before publish.
        await agent._store_event_episode(
            channel_event("actually, one more thing", wire_id="wire-A"), [],
        )
        successor = agent._interaction_tracker.get(SCOPE, speaker_id="alex")
        assert successor is not None and successor is not parked

        await discharge_vote(agent)

        assert successor.is_open, (
            "a record opened after the park was never judged by that vote"
        )
        assert agent._interaction_tracker.get(SCOPE, speaker_id="robin") is None, (
            "the true sibling still closes — Go's fan excludes this voter"
        )
        assert _close_reasons(await all_episodes(agent)) == [REASON_STRUCTURAL]

    async def test_vote_quorum_never_relabels_a_replayed_span(self):
        """PR #846 re-review: a quorum reached mid catch-up must not
        retire a replay-opened span under a live structural cause.

        The vote fan carries the same ``replayed`` guard as the cost and
        notification fans, but on THIS path it is defence in depth: the
        live vote turn trips the ISSUE-0130 replay->live split first, so
        the record is already closed as ``REASON_CATCHUP_COMPLETE`` by
        the time the discharge fans.  Pinned from that side, since that
        is the behaviour the system actually produces.  (The cost fan's
        guard IS load-bearing — it fires from the LLM-error path, before
        the stale fan reconciles the scope: see
        ``test_cost_fan_leaves_replayed_records_to_the_catchup_sweep``.)"""
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        replay_event = channel_event(
            "replayed backlog", wire_id="wire-A", sender="robin",
        )
        replay_event.metadata["replay_mode"] = True
        await agent._store_event_episode(replay_event, [])
        replayed = agent._interaction_tracker.get(SCOPE, speaker_id="robin")
        assert replayed is not None and replayed.replayed
        await agent._store_event_episode(
            channel_event("wrap it up?", wire_id="wire-A"), [vote()],
        )

        await discharge_vote(agent)

        assert replayed.close_reason == REASON_CATCHUP_COMPLETE, (
            "the ISSUE-0130 replay->live split owns this record; the vote "
            "quorum must never relabel a replayed span as structural"
        )

    async def test_recordless_straggler_keeps_its_retired_stamp(self):
        """PR #846 re-review: a late wire-A straggler from a speaker with
        NO open record keeps the HONEST retired stamp.

        The earlier suppression left such a record blank, but blank is
        the universally-admitted state in all three close fans and in
        the close-notification wire-id backfill — so the retired
        conversation's turn was closed as, cross-referenced to, and (on
        a bounded close) billed to the SUCCESSOR conversation.  The
        honest stamp is what makes those conjuncts skip it."""
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event(
                "topic B", wire_id="wire-B",
                prev_id="wire-A", prev_trigger="end_votes",
            ),
            [],
        )
        # Straggler from robin, who never spoke under wire-B.
        await agent._store_event_episode(
            channel_event("late from A", wire_id="wire-A", sender="robin"), [],
        )
        straggler = agent._interaction_tracker.get(SCOPE, speaker_id="robin")
        assert straggler is not None
        assert straggler.wire_interaction_id == "wire-A", (
            "the record's opening turn carried wire-A; stamping it is "
            "what lets the wire-id conjuncts tell it from a successor"
        )


@pytest.mark.asyncio
class TestDischargeFanAdmission:
    """PR #846 review — the end-vote discharge fan admits per record by
    wire id, anchored on the conversation the vote judged complete (the
    id frozen at park time): a sibling stamped with a successor id
    survives, and the parked record's own inline close no longer aborts
    the fan Go's quorum will never run for this voter."""

    async def test_discharge_skips_sibling_stamped_with_other_wire(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("topic A", wire_id="wire-A"), [],
        )
        await agent._store_event_episode(
            channel_event("wrap it up?", wire_id="wire-A"), [vote()],
        )
        # A successor conversation's record from another speaker — the
        # mixed state a straggler-parked vote can meet at discharge time.
        successor = agent._interaction_tracker.add_turn(SCOPE, speaker_id="robin")
        successor.wire_interaction_id = "wire-B"
        await discharge_vote(agent)
        assert agent._interaction_tracker.get(SCOPE, speaker_id="alex") is None
        survivor = agent._interaction_tracker.get(SCOPE, speaker_id="robin")
        assert survivor is not None and survivor.is_open, (
            "a sibling stamped with a successor wire id must survive the fan"
        )
        assert _close_reasons(await all_episodes(agent)) == [REASON_STRUCTURAL]

    async def test_discharge_fans_siblings_when_parked_record_closed(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("robin's point", wire_id="wire-A", sender="robin"), [],
        )
        await agent._store_event_episode(
            channel_event("wrap it up?", wire_id="wire-A"), [vote()],
        )
        parked = agent._interaction_tracker.get(SCOPE, speaker_id="alex")
        assert parked is not None
        # The parked record closes inline (cap / idle) between decide and
        # publish — its fate says nothing about the siblings.
        agent._interaction_tracker.close_record(
            parked, reason=REASON_STRUCTURAL,
        )
        await discharge_vote(agent)
        assert agent._interaction_tracker.records_for_scope(SCOPE) == [], (
            "the fan must still close the siblings — Go's quorum fan "
            "excludes this voter, so nothing else ever would"
        )
        episodes = await all_episodes(agent)
        assert _close_reasons(episodes) == [REASON_STRUCTURAL]
