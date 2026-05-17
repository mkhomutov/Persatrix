"""RFC 0020 PR 6 slice 6 — multi-turn close-path follow-ups (PR-3 review #12 + #15).

Sibling of :mod:`test_interaction_multi_turn_followups` (slice 5).  Pins
close-path contracts that the slice-5 idle-flush coverage didn't
exercise:

* **PR-3 review #12** — :class:`MaxTurnsDetector` enforced one event
  late.  Before slice 6, the cap fired only via
  :meth:`InteractionTracker.idle_check`, which the runtime calls at
  the *top* of the next event.  An interaction whose ``turn_count``
  reached the cap therefore stayed open until *another* event arrived,
  letting a structural close in between mislabel the closure as
  ``REASON_STRUCTURAL`` and surface the RFC 0020 §Security
  amplification window.  Slice 6 tightens enforcement to
  :meth:`add_turn` (inline cap check + close-and-pop on overflow); the
  multi-turn handler detects the auto-close and persists immediately.

* **PR-3 review #15** — mirror PR 2's
  ``test_store_episode_failure_is_swallowed_and_logged`` on the
  multi-turn close path.  PR 3's
  :meth:`_persist_closed_interaction` runs ``store_episode`` from
  inside its own inner ``try/except``, but no test asserted that the
  same swallow-and-log contract held there as on the single-turn
  parity path.

* **Release-prep coverage gap** — that the cap *re-arms* across
  consecutive interactions was argued only inductively
  (:class:`MaxTurnsDetector` is stateless — it reads only
  ``turn_count``) and pinned for a single re-open cycle in the unit
  suite.  :class:`TestMaxTurnsCapFiresRepeatedly` drives three full
  cap cycles in one scope so a long conversation that overruns the
  cap repeatedly is covered empirically, not just by argument.

Split off from the slice-5 follow-ups suite so each module stays under
the 500-line file-size cap (``scripts/checks/file_size.py --strict``).
Shared persona config / mock LLM client / clock-aware agent factory /
episode probe live in :mod:`_interaction_multi_turn_helpers`.
"""

from __future__ import annotations

import json
import logging

import pytest

from ._interaction_multi_turn_helpers import (
    all_episodes,
    make_agent_with_clock,
)

from agents.clock import FrozenClock
from agents.memory.boundary_detectors import REASON_MAX_TURNS
from agents.memory.interactions import scope_for_dm
from agents.persona_types import AgentEvent, EventType
from agents.tools.registry import clear_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


# ─── PR-3 review #12: MaxTurns cap fires inline on multi-turn ───


@pytest.mark.asyncio
class TestMaxTurnsCapMultiTurnPath:
    """Inline cap close on the runtime hot path (PR-3 review #12).

    The unit-side coverage in :mod:`tests.unit.python.test_interaction_tracker`
    pins the tracker contract — :meth:`InteractionTracker.add_turn`
    closes inline when the cap is reached and returns the closed
    interaction.  This integration test pins the runtime plumbing:
    :meth:`_EpisodeRoutingMixin._handle_multi_turn_event` detects the
    auto-close (``not interaction.is_open``) and persists immediately
    with ``REASON_MAX_TURNS``, rather than waiting for the next event's
    idle_check sweep where a structural close in the meantime could
    silently swap the close reason to ``REASON_STRUCTURAL``.
    """

    async def test_cap_th_event_persists_with_max_turns_reason(self):
        clock = FrozenClock(at=1_000.0)
        agent = await make_agent_with_clock(clock)
        # Lower the inline cap so the test fires within three events
        # rather than 200 (production default
        # ``DEFAULT_MAX_INTERACTION_TURNS``).  ``_max_turns`` is the
        # field :meth:`add_turn` consults inline; patching it directly
        # keeps the test focused on the runtime plumbing without
        # rebuilding the detector chain.
        agent._interaction_tracker._max_turns = 3
        peer = "iron-fox"
        scope = scope_for_dm(agent.agent_id, peer)
        for i in range(3):
            await agent.on_event(AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": f"msg-{i}"},
                sender_id=peer,
            ))
            # Tiny advance so successive turns don't collide on the
            # same monotonic instant; well below the idle window.
            clock.advance(0.1)
        await agent.drain_pending_summaries()

        # Exactly one closed-interaction episode — the cap fired on the
        # third event's add_turn and the multi-turn handler persisted
        # immediately rather than waiting for a fourth event.
        episodes = await all_episodes(agent)
        assert len(episodes) == 1, (
            "expected one episode per cap-closed interaction; got: "
            f"{episodes}"
        )
        ctx = json.loads(episodes[0]["context_json"])
        assert ctx["close_reason"] == REASON_MAX_TURNS
        assert episodes[0]["turn_count"] == 3
        # Scope popped — a subsequent event would open a fresh
        # interaction (RFC 0020 §C "do not reopen").
        assert agent._interaction_tracker.get(scope) is None


# ─── Repeated cap cycles across a long conversation ─────────────


@pytest.mark.asyncio
class TestMaxTurnsCapFiresRepeatedly:
    """The cap re-arms on every fresh interaction (release-prep gap).

    :class:`TestMaxTurnsCapMultiTurnPath` pins a *single* cap close;
    the unit suite (:mod:`tests.unit.python.test_interaction_tracker`)
    pins a *single* re-open cycle.  Neither drives the cap across
    multiple consecutive interactions, so "the cap keeps firing for a
    very long conversation" rested on an inductive argument
    (:class:`~agents.memory.boundary_detectors.MaxTurnsDetector` is
    stateless — it reads only ``interaction.turn_count``) rather than
    a test.

    This drives three full cap cycles in one scope and asserts each
    produces its own episode closed with ``REASON_MAX_TURNS``, with a
    distinct ``interaction_id`` — pinning that the re-opened
    interaction is a clean slate and the cap re-arms every cycle.
    """

    async def test_three_consecutive_cap_cycles_each_persist_max_turns(self):
        clock = FrozenClock(at=1_000.0)
        agent = await make_agent_with_clock(clock)
        # Cap of 3 → nine events drive exactly three full cycles.
        cap = 3
        cycles = 3
        agent._interaction_tracker._max_turns = cap
        peer = "iron-fox"
        scope = scope_for_dm(agent.agent_id, peer)
        for i in range(cap * cycles):
            await agent.on_event(AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": f"msg-{i}"},
                sender_id=peer,
            ))
            # Tiny advance — successive turns don't collide on the same
            # instant, and the total elapsed stays well below the 5s
            # idle window so no idle close confounds the attribution.
            clock.advance(0.1)
        await agent.drain_pending_summaries()

        episodes = await all_episodes(agent)
        assert len(episodes) == cycles, (
            f"expected one episode per cap cycle ({cycles}); got: {episodes}"
        )
        for n, ep in enumerate(episodes):
            ctx = json.loads(ep["context_json"])
            assert ctx["close_reason"] == REASON_MAX_TURNS, (
                f"episode {n} closed with {ctx['close_reason']!r}, "
                f"expected {REASON_MAX_TURNS!r}"
            )
            assert ep["turn_count"] == cap, (
                f"episode {n} persisted turn_count={ep['turn_count']}, "
                f"expected {cap}"
            )
        # Distinct ids prove each cycle is a clean-slate interaction,
        # not an extension of the previous one — the re-open rule held
        # every cycle, not just the first.
        ids = [ep["interaction_id"] for ep in episodes]
        assert len(set(ids)) == cycles, f"interaction_ids not distinct: {ids}"
        # The ninth event's add_turn fired the third cap and popped the
        # scope — a tenth event would open cycle four.
        assert agent._interaction_tracker.get(scope) is None


# ─── PR-3 review #15: multi-turn close-path failure swallow ─────


@pytest.mark.asyncio
class TestMultiTurnCloseFailureIsSwallowedAndLogged:
    """Mirror of PR 2's single-turn failure-swallow test on the
    multi-turn close path (PR-3 review #15).

    PR 2 already pins
    ``test_store_episode_failure_is_swallowed_and_logged`` on
    ``_store_event_episode``.  PR 3's multi-turn close path runs
    ``store_episode`` from inside :meth:`_persist_closed_interaction`
    — a different inner ``try/except`` — and no test asserted that
    the same swallow-and-log contract held there.  This test patches
    ``store_episode`` to raise on a session-end close, asserts the
    warning is emitted with the failed scope's identity, and
    confirms tracker state is consistent (no dangling open scope,
    next event opens a fresh interaction).
    """

    async def test_session_end_persist_failure_is_logged_and_state_consistent(
        self, caplog, monkeypatch,
    ):
        clock = FrozenClock(at=1_000.0)
        agent = await make_agent_with_clock(clock)
        peer = "iron-fox"
        scope = scope_for_dm(agent.agent_id, peer)

        # Open the interaction with one turn.
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hello"},
            sender_id=peer,
        ))

        async def _boom(**_kwargs):
            raise RuntimeError("simulated SQLite I/O failure")

        monkeypatch.setattr(agent._episodic_memory, "store_episode", _boom)

        with caplog.at_level(
            logging.WARNING,
            logger="agents.persona_runtime.episode_routing",
        ):
            # Session-end metadata triggers ``_handle_multi_turn_event``'s
            # close path — :meth:`_persist_closed_interaction` runs
            # ``store_episode`` (which raises) inside its own inner try.
            await agent.on_event(AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": "bye"},
                sender_id=peer,
                metadata={"chat_end": True},
            ))

        # Contract 1: tracker state consistent — close ran before the
        # store failed, so the scope is popped (mirrors single-turn
        # contract from PR 2's test).
        assert scope not in agent._interaction_tracker.open_scopes()

        # Contract 2: the warning carries the scope identity so an
        # operator can correlate the close-counter increment with the
        # missing episode row.  ``_persist_closed_interaction`` logs
        # "Failed to persist closed interaction for agent ... (scope=...)".
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "Failed to persist closed interaction" in r.getMessage()
            and scope in r.getMessage()
            for r in warnings
        ), (
            "expected a persist-failure warning naming the failed scope; "
            f"got: {[r.getMessage() for r in warnings]}"
        )

        # Contract 3: a subsequent event in the same scope opens a
        # fresh interaction.  Restore ``store_episode`` so the next
        # close path doesn't reraise (we only want to pin recovery).
        monkeypatch.undo()
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "again"},
            sender_id=peer,
        ))
        next_open = agent._interaction_tracker.get(scope)
        assert next_open is not None
        assert next_open.turn_count == 1
