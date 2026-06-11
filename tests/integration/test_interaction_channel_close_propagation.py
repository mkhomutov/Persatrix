"""RFC 0030 channel-side structural close propagation (MT-CHANNEL-GOV-003 §3).

The interaction-id producer made the orchestrator's vote-quorum close
real, but nothing propagated it to the agent-local
:class:`~agents.memory.interactions.InteractionTracker` — the
vote-closed discussion and the channel's next topic merged into one
local interaction that eventually closed as ``idle_gap``, so the
interaction-summary surface (``persatrix agent interactions``) never
showed a converged discussion as "ended".  This suite pins the two
propagation seams (``agents/persona_runtime/interaction_boundary.py``):

* **Publish-confirmed vote close** (PR 607 review finding 5) — a turn
  whose decided actions carry an ``END_INTERACTION_VOTE`` for the
  event's group channel PARKS the voter's local close
  (``agents/persona_runtime/vote_close.py``); the executor's
  publish-outcome callback (``resolve_end_vote_publish``) closes the
  scope with ``REASON_STRUCTURAL`` on publish success and drops the
  park on failure — a vote that never reached the orchestrator leaves
  no early "ended" record.  DM votes and votes bound to a different
  channel must not park anything — the executor-side gates
  (``end_vote_action.py``) mirrored locally.

* **Wire interaction-id rotation** — when the orchestrator-minted
  ``interaction_id`` on an inbound event differs from the one the open
  local interaction was opened under, the previous conversation ended
  (quorum or idle rotation) and the local scope splits at the same
  boundary, labelled by the wire-carried close cause (producer plan
  OQ 5: ``previous_interaction_id`` + trigger → ``idle_gap`` for an
  idle rotation, ``structural`` for the quorum; absent or mismatched →
  the legacy ``structural``).  Untracked traffic (no wire id — old
  orchestrator, non-channel ingress) keeps the pre-producer idle-only
  behaviour.

Shared persona config / mock LLM client / clock-aware agent factory
live in :mod:`_interaction_multi_turn_helpers`.
"""

from __future__ import annotations

import pytest

from agents.clock import FrozenClock
from agents.memory.interactions import (
    REASON_STRUCTURAL,
    scope_for_dm,
    scope_for_group,
    scope_for_thread,
)
from agents.persona_types import EventType
from agents.tools.registry import clear_registry

from ._interaction_multi_turn_helpers import (
    GROUP_CHANNEL,
    all_episodes,
    channel_event,
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


# ─── Publish-confirmed vote close ────────────────────────────


@pytest.mark.asyncio
class TestVoteClosesLocalInteraction:
    """A turn that votes to end the discussion PARKS the voter's local
    close (PR 607 review finding 5); the executor's publish-outcome
    callback closes it structurally on success — the record
    MT-CHANNEL-GOV-003 Step 3 reads back exists the moment the vote
    actually landed on the wire, and never for a publish that failed."""

    async def test_vote_parks_then_publish_success_closes(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        for i in range(2):
            await agent._store_event_episode(
                channel_event(f"turn-{i}", wire_id="wire-A"), [],
            )
        assert agent._interaction_tracker.get(SCOPE) is not None
        await agent._store_event_episode(
            channel_event("wrap it up?", wire_id="wire-A"), [vote()],
        )
        # Decide time: parked, NOT closed — the vote has not been
        # published yet (the episode stores before the executor runs).
        parked = agent._interaction_tracker.get(SCOPE)
        assert parked is not None
        assert parked.is_open
        assert await all_episodes(agent) == []
        # Publish outcome: success closes the parked scope (RFC 0020 §C
        # never-reopen) and the closed episode row carries the structural
        # trigger the CLI renders as "ended".
        await agent.resolve_end_vote_publish(CHANNEL, published=True)
        assert agent._interaction_tracker.get(SCOPE) is None
        episodes = await all_episodes(agent)
        assert len(episodes) == 1
        assert episodes[0]["scope"] == SCOPE
        assert episodes[0]["turn_count"] == 3
        assert _close_reasons(episodes) == [REASON_STRUCTURAL]

    async def test_failed_publish_leaves_record_open(self):
        # PR 607 review finding 5 itself: a vote publish that fails
        # (timeout, channels disabled, no publisher) must not leave an
        # early "ended" record — the scope stays open for the ordinary
        # closes (wire rotation once a real quorum forms, or idle).
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("wrap it up?", wire_id="wire-A"), [vote()],
        )
        await agent.resolve_end_vote_publish(CHANNEL, published=False)
        open_interaction = agent._interaction_tracker.get(SCOPE)
        assert open_interaction is not None
        assert open_interaction.is_open
        assert await all_episodes(agent) == []
        # The park was dropped, not deferred: a later success callback
        # (a duplicate / stray notification) finds nothing to close.
        await agent.resolve_end_vote_publish(CHANNEL, published=True)
        assert agent._interaction_tracker.get(SCOPE) is not None
        assert await all_episodes(agent) == []

    async def test_stale_park_does_not_close_successor(self):
        # Between decide and discharge the scope can move on (here: the
        # wire id rotates and the close-then-reopen replaces the parked
        # interaction). The discharge must no-op rather than close the
        # successor on the strength of the predecessor's vote.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("wrap it up?", wire_id="wire-A"), [vote()],
        )
        await agent._store_event_episode(
            channel_event("new topic", wire_id="wire-B"), [],
        )
        episodes = await all_episodes(agent)
        assert len(episodes) == 1  # the rotation close, not the vote's
        await agent.resolve_end_vote_publish(CHANNEL, published=True)
        successor = agent._interaction_tracker.get(SCOPE)
        assert successor is not None
        assert successor.is_open
        assert len(await all_episodes(agent)) == 1

    async def test_callback_without_park_is_noop(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("ongoing", wire_id="wire-A"), [],
        )
        await agent.resolve_end_vote_publish(CHANNEL, published=True)
        open_interaction = agent._interaction_tracker.get(SCOPE)
        assert open_interaction is not None
        assert open_interaction.is_open
        assert await all_episodes(agent) == []

    async def test_unbound_vote_parks_for_inbound_channel(self):
        # ``bind_end_vote_channel`` stamps the inbound channel before
        # publish; the local park applies the same default so the two
        # halves cannot disagree about which conversation the persona
        # ended.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("closable question"), [vote(channel_id=None)],
        )
        await agent.resolve_end_vote_publish(CHANNEL, published=True)
        assert agent._interaction_tracker.get(SCOPE) is None
        episodes = await all_episodes(agent)
        assert _close_reasons(episodes) == [REASON_STRUCTURAL]

    async def test_vote_for_other_channel_does_not_close(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("ongoing"), [vote(channel_id="group:other")],
        )
        # Nothing parked for either channel: a success callback for the
        # vote's target closes nothing here.
        await agent.resolve_end_vote_publish("group:other", published=True)
        open_interaction = agent._interaction_tracker.get(SCOPE)
        assert open_interaction is not None
        assert open_interaction.is_open
        assert await all_episodes(agent) == []

    async def test_dm_vote_does_not_close(self):
        # The executor drops DM votes (``status=dm_channel``); the local
        # park must mirror that gate, not race ahead of it.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        dm_channel = "dm:alex"
        dm_scope = scope_for_dm(agent.agent_id, "alex")
        await agent._store_event_episode(
            channel_event(
                "wrap up?", channel=dm_channel, channel_type="dm",
            ),
            [vote(channel_id=dm_channel)],
        )
        await agent.resolve_end_vote_publish(dm_channel, published=True)
        open_interaction = agent._interaction_tracker.get(dm_scope)
        assert open_interaction is not None
        assert open_interaction.is_open
        assert await all_episodes(agent) == []


# ─── Wire interaction-id rotation ────────────────────────────


@pytest.mark.asyncio
class TestWireRotationClosesLocalInteraction:
    """The orchestrator's resolver rotating the channel's interaction id
    is the channel-side structural boundary: the stale local interaction
    closes and the new turn opens fresh — discussions no longer merge
    across the vote/idle close into one ``idle_gap`` blob."""

    async def test_rotation_closes_previous_interaction(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        for i in range(2):
            await agent._store_event_episode(
                channel_event(f"old-topic-{i}", wire_id="wire-A"), [],
            )
        await agent._store_event_episode(
            channel_event("new topic", wire_id="wire-B"), [],
        )
        episodes = await all_episodes(agent)
        assert len(episodes) == 1
        assert episodes[0]["scope"] == SCOPE
        assert episodes[0]["turn_count"] == 2
        assert _close_reasons(episodes) == [REASON_STRUCTURAL]
        fresh = agent._interaction_tracker.get(SCOPE)
        assert fresh is not None
        assert fresh.is_open
        assert fresh.turn_count == 1
        assert fresh.wire_interaction_id == "wire-B"

    async def test_first_wire_id_is_stamped_on_open(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("hello", wire_id="wire-A"), [],
        )
        opened = agent._interaction_tracker.get(SCOPE)
        assert opened is not None
        assert opened.wire_interaction_id == "wire-A"

    async def test_untracked_traffic_keeps_merging(self):
        # No wire id (old orchestrator / non-channel ingress): the
        # pre-producer idle-only behaviour is preserved verbatim.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        for i in range(3):
            await agent._store_event_episode(
                channel_event(f"legacy-{i}"), [],
            )
        open_interaction = agent._interaction_tracker.get(SCOPE)
        assert open_interaction is not None
        assert open_interaction.turn_count == 3
        assert open_interaction.wire_interaction_id == ""
        assert await all_episodes(agent) == []

    async def test_late_id_on_open_interaction_does_not_close(self):
        # A scope opened on untracked traffic later seeing a wire id is
        # adoption, not rotation — the first carried id is stamped and
        # only a *different* subsequent id splits the scope.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(channel_event("untagged"), [])
        await agent._store_event_episode(
            channel_event("tagged", wire_id="wire-A"), [],
        )
        open_interaction = agent._interaction_tracker.get(SCOPE)
        assert open_interaction is not None
        assert open_interaction.turn_count == 2
        assert open_interaction.wire_interaction_id == "wire-A"
        assert await all_episodes(agent) == []

    async def test_vote_then_fresh_topic_yields_two_clean_records(self):
        # The MT-CHANNEL-GOV-003 arc in miniature: vote-close the first
        # discussion (publish-confirmed), then a new topic on a rotated
        # wire id opens a fresh local interaction without a spurious
        # second close.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("relay or beacon?", wire_id="wire-A"), [],
        )
        await agent._store_event_episode(
            channel_event("agreed: relay", wire_id="wire-A"), [vote()],
        )
        await agent.resolve_end_vote_publish(CHANNEL, published=True)
        assert agent._interaction_tracker.get(SCOPE) is None
        await agent._store_event_episode(
            channel_event("retro agenda?", wire_id="wire-B"), [],
        )
        episodes = await all_episodes(agent)
        assert len(episodes) == 1
        assert episodes[0]["turn_count"] == 2
        assert _close_reasons(episodes) == [REASON_STRUCTURAL]
        fresh = agent._interaction_tracker.get(SCOPE)
        assert fresh is not None
        assert fresh.wire_interaction_id == "wire-B"


# The wire-carried close-cause labels (producer plan OQ 5 — idle_gap vs
# structural by the stamped trigger) are pinned in their own suite:
# test_interaction_close_cause_labels.py.


# ─── Thread scopes are wire-untracked ────────────────────────


THREAD_ID = "t-veto"
THREAD_SCOPE = scope_for_thread(THREAD_ID)


@pytest.mark.asyncio
class TestThreadScopesAreWireUntracked:
    """Resolver rule IP3: thread conversations never rotate — "the thread
    IS the interaction".  A threaded reply publishes to the *parent*
    channel, so ``publishCommit`` resolves the FLOOR's interaction id for
    it and the fanout delivers ``thread_id`` and that floor id on the
    same event.  The floor id rotating (vote quorum, idle rotation, or an
    orchestrator-restart re-mint) therefore says nothing about the thread
    conversation: a thread-scoped local interaction must neither stamp
    nor compare wire ids — it keeps the idle / session-end closes only
    (PR 607 review finding 1)."""

    async def test_floor_rotation_does_not_close_thread_scope(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        for i in range(2):
            await agent._store_event_episode(
                channel_event(
                    f"thread-turn-{i}", wire_id="wire-A", thread_id=THREAD_ID,
                ),
                [],
            )
        # The floor conversation closes (quorum or idle) and the resolver
        # rotates; the next threaded reply carries the rotated floor id.
        await agent._store_event_episode(
            channel_event(
                "thread keeps going", wire_id="wire-B", thread_id=THREAD_ID,
            ),
            [],
        )
        open_interaction = agent._interaction_tracker.get(THREAD_SCOPE)
        assert open_interaction is not None
        assert open_interaction.is_open
        assert open_interaction.turn_count == 3
        assert await all_episodes(agent) == []

    async def test_thread_interaction_never_stamps_wire_id(self):
        # Not stamping is the load-bearing half: a stamped floor id is
        # exactly what would arm the rotation close above.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("threaded", wire_id="wire-A", thread_id=THREAD_ID),
            [],
        )
        opened = agent._interaction_tracker.get(THREAD_SCOPE)
        assert opened is not None
        assert opened.wire_interaction_id == ""

    async def test_vote_on_threaded_turn_does_not_close_thread_scope(self):
        # ``bind_end_vote_channel`` stamps the PARENT channel onto the
        # vote and the quorum closes the FLOOR conversation — the thread
        # the voter happened to be replying in does not end (PR 607
        # review finding 2).  The voter's floor-scope record closes on
        # the floor's id rotation like any non-voter's.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("wrap the floor up?", thread_id=THREAD_ID),
            [vote()],
        )
        open_interaction = agent._interaction_tracker.get(THREAD_SCOPE)
        assert open_interaction is not None
        assert open_interaction.is_open
        assert await all_episodes(agent) == []


# ─── MENTION events and the unbound-vote default ─────────────


@pytest.mark.asyncio
class TestMentionEventVoteGate:
    """``bind_end_vote_channel`` stamps the inbound channel only on
    ``CHANNEL_MESSAGE`` events, so an unbound vote decided on a MENTION
    turn reaches the executor without a ``channel_id`` and is dropped
    (``status=no_channel_id``) — the local park must not race ahead of
    a publish that never happens (PR 607 review finding 4).  A vote that
    explicitly names the channel is published normally and closes the
    local scope on any multi-turn event type once the publish succeeds."""

    async def test_unbound_vote_on_mention_event_does_not_close(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("you were mentioned", event_type=EventType.MENTION),
            [vote(channel_id=None)],
        )
        open_interaction = agent._interaction_tracker.get(SCOPE)
        assert open_interaction is not None
        assert open_interaction.is_open
        assert await all_episodes(agent) == []

    async def test_bound_vote_on_mention_event_closes(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("you were mentioned", event_type=EventType.MENTION),
            [vote()],
        )
        await agent.resolve_end_vote_publish(CHANNEL, published=True)
        assert agent._interaction_tracker.get(SCOPE) is None
        episodes = await all_episodes(agent)
        assert _close_reasons(episodes) == [REASON_STRUCTURAL]
