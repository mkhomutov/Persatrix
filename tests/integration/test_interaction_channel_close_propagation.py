"""RFC 0030 channel-side structural close propagation (MT-CHANNEL-GOV-003 §3).

The interaction-id producer made the orchestrator's vote-quorum close
real, but nothing propagated it to the agent-local
:class:`~agents.memory.interactions.InteractionTracker` — the
vote-closed discussion and the channel's next topic merged into one
local interaction that eventually closed as ``idle_gap``, so the
interaction-summary surface (``persatrix agent interactions``) never
showed a converged discussion as "ended".  This suite pins the two
propagation seams (``agents/persona_runtime/interaction_boundary.py``):

* **Vote-time close** — a turn whose decided actions carry an
  ``END_INTERACTION_VOTE`` for the event's group channel closes the
  voter's local scope with ``REASON_STRUCTURAL`` (the channel sibling
  of RFC 0020 §B's explicit ``END_INTERACTION`` trigger).  DM votes and
  votes bound to a different channel must not close anything — the
  executor-side gates (``end_vote_action.py``) mirrored locally.

* **Wire interaction-id rotation** — when the orchestrator-minted
  ``interaction_id`` on an inbound event differs from the one the open
  local interaction was opened under, the previous conversation ended
  (quorum or idle rotation) and the local scope splits at the same
  boundary.  Untracked traffic (no wire id — old orchestrator,
  non-channel ingress) keeps the pre-producer idle-only behaviour.

Shared persona config / mock LLM client / clock-aware agent factory
live in :mod:`_interaction_multi_turn_helpers`.
"""

from __future__ import annotations

import json

import pytest

from agents.clock import FrozenClock
from agents.memory.interactions import (
    REASON_STRUCTURAL,
    scope_for_dm,
    scope_for_group,
)
from agents.persona_types import ActionType, AgentAction, AgentEvent, EventType
from agents.tools.registry import clear_registry

from ._interaction_multi_turn_helpers import (
    all_episodes,
    make_agent_with_clock,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


CHANNEL = "group:planning"
SCOPE = scope_for_group(CHANNEL)


def channel_event(
    content: str,
    *,
    wire_id: str | None = None,
    channel: str = CHANNEL,
    channel_type: str = "group",
    sender: str = "alex",
) -> AgentEvent:
    metadata: dict = {}
    if wire_id is not None:
        metadata["interaction_id"] = wire_id
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": content, "channel_type": channel_type},
        channel_id=channel,
        sender_id=sender,
        metadata=metadata,
    )


def vote(channel_id: str | None = CHANNEL) -> AgentAction:
    payload: dict = {} if channel_id is None else {"channel_id": channel_id}
    return AgentAction(
        action_type=ActionType.END_INTERACTION_VOTE, payload=payload,
    )


def _close_reasons(episodes: list[dict]) -> list[str]:
    return [
        json.loads(e["context_json"] or "{}").get("close_reason", "")
        for e in episodes
    ]


# ─── Vote-time close ─────────────────────────────────────────


@pytest.mark.asyncio
class TestVoteClosesLocalInteraction:
    """A turn that votes to end the discussion closes the voter's own
    local interaction structurally — the record MT-CHANNEL-GOV-003
    Step 3 reads back exists the moment the persona judged "done",
    not an idle window later."""

    async def test_vote_closes_group_scope_structurally(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        for i in range(2):
            await agent._store_event_episode(
                channel_event(f"turn-{i}", wire_id="wire-A"), [],
            )
        assert agent._interaction_tracker.get(SCOPE) is not None
        await agent._store_event_episode(
            channel_event("wrap it up?", wire_id="wire-A"), [vote()],
        )
        # Scope popped (RFC 0020 §C never-reopen) and the closed episode
        # row carries the structural trigger the CLI renders as "ended".
        assert agent._interaction_tracker.get(SCOPE) is None
        episodes = await all_episodes(agent)
        assert len(episodes) == 1
        assert episodes[0]["scope"] == SCOPE
        assert episodes[0]["turn_count"] == 3
        assert _close_reasons(episodes) == [REASON_STRUCTURAL]

    async def test_unbound_vote_counts_for_inbound_channel(self):
        # ``bind_end_vote_channel`` stamps the inbound channel before
        # publish; the local close applies the same default so the two
        # halves cannot disagree about which conversation the persona
        # ended.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("closable question"), [vote(channel_id=None)],
        )
        assert agent._interaction_tracker.get(SCOPE) is None
        episodes = await all_episodes(agent)
        assert _close_reasons(episodes) == [REASON_STRUCTURAL]

    async def test_vote_for_other_channel_does_not_close(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("ongoing"), [vote(channel_id="group:other")],
        )
        open_interaction = agent._interaction_tracker.get(SCOPE)
        assert open_interaction is not None
        assert open_interaction.is_open
        assert await all_episodes(agent) == []

    async def test_dm_vote_does_not_close(self):
        # The executor drops DM votes (``status=dm_channel``); the local
        # close must mirror that gate, not race ahead of it.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        dm_channel = "dm:alex"
        dm_scope = scope_for_dm(agent.agent_id, "alex")
        await agent._store_event_episode(
            channel_event(
                "wrap up?", channel=dm_channel, channel_type="dm",
            ),
            [vote(channel_id=dm_channel)],
        )
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
        # discussion, then a new topic on a rotated wire id opens a
        # fresh local interaction without a spurious second close.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("relay or beacon?", wire_id="wire-A"), [],
        )
        await agent._store_event_episode(
            channel_event("agreed: relay", wire_id="wire-A"), [vote()],
        )
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
