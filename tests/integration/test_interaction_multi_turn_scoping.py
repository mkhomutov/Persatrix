"""RFC 0020 PR 6 slice 6 — multi-turn scope routing coverage (PR-3 review #17).

Sibling of :mod:`test_interaction_multi_turn` (the original PR-3 suite)
and :mod:`test_interaction_multi_turn_cap_failure` (slice-6 close-path
coverage).  This module pins the scope-routing surface that PR 3
introduced but the original suite under-exercised:

* **MENTION aggregation parity with CHANNEL_MESSAGE** — both event
  types are members of ``_MULTI_TURN_EVENT_TYPES`` and route through
  :meth:`_handle_multi_turn_event`, so a multi-turn ``MENTION`` flow
  must collapse into a single interaction the same way
  ``CHANNEL_MESSAGE`` already does.  Without coverage, a future change
  that special-cased ``MENTION`` (e.g. routing it through the
  single-turn path) would land green.

* **Two concurrent open scopes stay independent** — DM-A and DM-B on
  the same agent must accumulate side-by-side without bleeding turns
  across, until each closes through its own session-end / idle-gap.
  Pins the scope-keyed isolation contract that
  :class:`InteractionTracker._open` provides on the runtime hot path,
  not just at the unit-test level.

* **`channel_id` vs `sender_id` precedence** — RFC 0020 §G discriminator
  cascade: a CHANNEL_MESSAGE with *both* ``channel_id`` and ``sender_id``
  set must route by the channel-id-derived scope (group / thread /
  prefixed-DM), not the sender-id legacy DM scope.  Pre-PR-5 the
  precedence was implicit; PR 5 will reshuffle the routing surface
  with channel-aware metadata, and locking the precedence here keeps
  the reshuffle reviewable.

* **`scope=None` fallback** — a CHANNEL_MESSAGE with neither
  ``channel_id`` nor ``sender_id`` (under-populated payload from a
  malformed adapter) must land in the legacy NULL-interaction shape
  *and* emit a warning naming the event type, rather than silently
  dropping the row or routing it under ``SCOPE_TICK``.

Shared persona config / mock LLM client / clock-aware agent factory
live in :mod:`_interaction_multi_turn_helpers`.
"""

from __future__ import annotations

import logging

import pytest

from agents.clock import FrozenClock
from agents.memory.interactions import (
    scope_for_dm,
    scope_for_group,
    scope_for_thread,
)
from agents.persona_types import AgentEvent, EventType
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


# ─── PR-3 review #17a: MENTION aggregation parity ──────────────


@pytest.mark.asyncio
class TestMentionAggregation:
    """``MENTION`` must aggregate into the same interaction as
    ``CHANNEL_MESSAGE`` for the same scope.

    Both are members of ``_EpisodeRoutingMixin._MULTI_TURN_EVENT_TYPES``
    and share the multi-turn aggregation path.  The original PR-3
    suite covered ``CHANNEL_MESSAGE`` aggregation only, leaving
    ``MENTION`` on the legacy NULL-interaction parity path.  A future
    refactor that re-classified ``MENTION`` (e.g. moving it to
    ``_SINGLE_TURN_EVENT_TYPES``) would silently produce one episode
    per mention instead of one per interaction, with no test failure
    to flag the regression.
    """

    async def test_repeated_mention_collapses_into_one_interaction(self):
        clock = FrozenClock(at=1_000.0)
        agent = await make_agent_with_clock(clock)
        peer = "iron-fox"
        scope = scope_for_dm(agent.agent_id, peer)
        for i in range(3):
            await agent.on_event(AgentEvent(
                event_type=EventType.MENTION,
                payload={"content": f"@me ping-{i}"},
                sender_id=peer,
            ))
            clock.advance(0.5)
        # No session-end yet — interaction stays open.  The contract
        # under test is per-turn aggregation, not closure.
        open_interaction = agent._interaction_tracker.get(scope)
        assert open_interaction is not None
        assert open_interaction.turn_count == 3
        # No closed-interaction episode yet (no close happened).
        episodes = await all_episodes(agent)
        assert episodes == []

    async def test_mention_session_end_persists_one_episode(self):
        clock = FrozenClock(at=1_000.0)
        agent = await make_agent_with_clock(clock)
        peer = "iron-fox"
        scope = scope_for_dm(agent.agent_id, peer)
        for i in range(2):
            await agent.on_event(AgentEvent(
                event_type=EventType.MENTION,
                payload={"content": f"@me ping-{i}"},
                sender_id=peer,
            ))
            clock.advance(0.5)
        await agent.on_event(AgentEvent(
            event_type=EventType.MENTION,
            payload={"content": "@me final"},
            sender_id=peer,
            metadata={"chat_end": True},
        ))
        await agent.drain_pending_summaries()
        episodes = await all_episodes(agent)
        assert len(episodes) == 1
        assert episodes[0]["scope"] == scope
        assert episodes[0]["turn_count"] == 3


# ─── PR-3 review #17b: Two concurrent open scopes stay isolated ─


@pytest.mark.asyncio
class TestConcurrentOpenScopesIsolation:
    """Two scopes on the same agent must accumulate independently.

    The :class:`InteractionTracker._open` map is keyed by scope, so
    DM-A and DM-B turns interleaved on the same agent must end up in
    two distinct interactions with two distinct ``interaction_id``
    values.  Closing one must not leak into the other.
    """

    async def test_two_dms_accumulate_independently_and_close_in_order(self):
        clock = FrozenClock(at=1_000.0)
        agent = await make_agent_with_clock(clock)
        peer_a = "iron-fox"
        peer_b = "ember-owl"
        scope_a = scope_for_dm(agent.agent_id, peer_a)
        scope_b = scope_for_dm(agent.agent_id, peer_b)

        # Interleaved turns: A, B, A, B, A.
        senders = [peer_a, peer_b, peer_a, peer_b, peer_a]
        for sender in senders:
            await agent.on_event(AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": "msg"},
                sender_id=sender,
            ))
            clock.advance(0.1)

        open_a = agent._interaction_tracker.get(scope_a)
        open_b = agent._interaction_tracker.get(scope_b)
        assert open_a is not None
        assert open_b is not None
        # Three turns to A, two to B — interleaving did not bleed turns
        # from one scope into the other.
        assert open_a.turn_count == 3
        assert open_b.turn_count == 2
        assert open_a.interaction_id != open_b.interaction_id

        # Close A via session-end; B must remain open and untouched.
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "bye A"},
            sender_id=peer_a,
            metadata={"chat_end": True},
        ))
        await agent.drain_pending_summaries()
        # A is gone from the tracker, B is still open with the same id.
        assert agent._interaction_tracker.get(scope_a) is None
        still_open_b = agent._interaction_tracker.get(scope_b)
        assert still_open_b is not None
        assert still_open_b.interaction_id == open_b.interaction_id
        assert still_open_b.turn_count == 2


# ─── PR-3 review #17c: channel_id vs sender_id precedence ──────


@pytest.mark.asyncio
class TestChannelIdSenderIdPrecedence:
    """RFC 0020 §G — channel-id-derived scope wins over sender-id DM.

    A CHANNEL_MESSAGE that carries both ``channel_id`` (with a
    canonical prefix or an explicit ``channel_type``) and
    ``sender_id`` must route by the channel-id-derived scope.  The
    sender-id-only DM scope is the legacy fallback for events that
    arrive without a ``channel_id`` (pre-RFC-0020 chat path); locking
    the precedence keeps PR 5's channel-aware reshuffle reviewable.
    """

    async def test_group_channel_id_wins_over_sender_id_dm(self):
        clock = FrozenClock(at=1_000.0)
        agent = await make_agent_with_clock(clock)
        peer = "iron-fox"
        # Group channel with explicit ``channel_type`` and a
        # ``sender_id`` that, on its own, would have produced a DM
        # scope.  The group scope wins.
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"channel_type": "group", "content": "hello"},
            channel_id="planning",
            sender_id=peer,
        ))
        group_scope = scope_for_group("planning")
        dm_scope = scope_for_dm(agent.agent_id, peer)
        assert agent._interaction_tracker.get(group_scope) is not None
        assert agent._interaction_tracker.get(dm_scope) is None

    async def test_thread_id_wins_over_channel_id_and_sender_id(self):
        # RFC 0020 §G discriminator step 1: thread_id wins outright.
        clock = FrozenClock(at=1_000.0)
        agent = await make_agent_with_clock(clock)
        peer = "iron-fox"
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"channel_type": "group", "content": "reply"},
            channel_id="planning",
            sender_id=peer,
            thread_id="t-42",
        ))
        thread_scope = scope_for_thread("t-42")
        group_scope = scope_for_group("planning")
        dm_scope = scope_for_dm(agent.agent_id, peer)
        assert agent._interaction_tracker.get(thread_scope) is not None
        assert agent._interaction_tracker.get(group_scope) is None
        assert agent._interaction_tracker.get(dm_scope) is None


# ─── PR-3 review #17d: scope=None fallback warns + legacy shape ─


@pytest.mark.asyncio
class TestUnderPopulatedEventFallback:
    """A multi-turn event with neither ``channel_id`` nor ``sender_id``
    must land in the legacy NULL-interaction shape *and* emit a
    warning naming the event type.

    :func:`scope_for_channel_event` returns ``None`` for an
    under-populated event; :meth:`_handle_multi_turn_event` then
    short-circuits to ``store_episode(summary=..., context=...)``
    without any interaction columns, and warns so operators can
    spot the malformed ingress before it silently accumulates as
    a downstream episode-shape regression.
    """

    async def test_no_channel_no_sender_falls_back_with_warning(self, caplog):
        clock = FrozenClock(at=1_000.0)
        agent = await make_agent_with_clock(clock)
        with caplog.at_level(
            logging.WARNING,
            logger="agents.persona_runtime.episode_routing",
        ):
            await agent.on_event(AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": "orphan"},
                # No channel_id, no sender_id.
            ))

        # Tracker has no open scope — the event bypassed
        # ``InteractionTracker.add_turn`` entirely.
        assert agent._interaction_tracker.open_scopes() == []

        # One legacy-shape row landed (NULL interaction columns).
        episodes = await all_episodes(agent)
        assert len(episodes) == 1
        assert episodes[0]["interaction_id"] is None
        assert episodes[0]["scope"] is None
        assert episodes[0]["turn_count"] is None

        # Warning carries the event type so the malformed ingress is
        # spottable in operator logs.
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "neither channel_id nor sender_id" in r.getMessage()
            and "channel_message" in r.getMessage()
            for r in warnings
        ), [r.getMessage() for r in warnings]
