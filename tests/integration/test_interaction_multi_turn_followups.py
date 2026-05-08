"""RFC 0020 PR 6 slice 5 — multi-turn aggregation follow-ups (PR-3 review #13/#14/#16).

Sibling of :mod:`test_interaction_multi_turn` covering the slice-5 review
residuals that the original PR-3 multi-turn suite did not exercise:

* **PR-3 review #16** — wire the :class:`agents.clock.Clock` seam through
  to :class:`InteractionTracker` so tests inject a deterministic clock
  once at agent construction time instead of threading ``now=`` through
  every call.  Pins the contract that
  ``InteractionTracker._clock`` resolves to the persona's clock.

* **PR-3 review #14** — end-to-end cross-scope idle flush via
  :meth:`_LLMPersonaAgent.on_event` (not the helper-driven
  :meth:`InteractionTracker.idle_check` shortcut the original suite uses).
  Opens scope A, advances the persona's clock past the idle window,
  fires an event in scope B, and asserts scope A persisted with
  ``REASON_IDLE_GAP`` while scope B opens independently.  This is the
  production hot path the PR description names.

* **PR-3 review #13** — the cross-scope idle-flush loop sat inside the
  outer ``try/except`` of ``_store_event_episode``.  If
  ``_persist_closed_interaction`` raised past its own inner try (rare —
  ``asyncio.CancelledError`` or a programming error in ctx-construction),
  the outer handler logged the *current* event's ``event_type`` — not
  the stale scope's identity — making the failure unattributable.
  This module pins the corrected contract: per-scope flush failures log
  the *failed* scope's identity, not the in-flight event.

Split off from the original multi-turn suite so each module stays under
the 500-line file-size cap (``scripts/checks/file_size.py --strict``).
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.clock import FrozenClock
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.memory.boundary_detectors import REASON_IDLE_GAP
from agents.memory.interactions import scope_for_dm
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import AgentEvent, EventType
from agents.tools.registry import clear_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


_TEST_IDLE_TIMEOUT_SEC: float = 5.0


_PERSONA_CONFIG: dict = {
    "id": "multi-turn-followups-persona",
    "model": "test-model",
    "role": "Multi-turn follow-ups test persona",
    "type": "persona",
    "max_llm_calls": 5,
    "max_tokens": 1024,
    "tools": [],
    "persona": {
        "name": "Multi-Turn Follow-Ups Agent",
        "background": "RFC 0020 PR 6 slice 5 follow-ups.",
        "behavior": {
            "directness": "balanced",
            "formality": "professional",
            "risk_tolerance": "moderate",
        },
    },
    "autonomy": {
        "level": "semi-autonomous",
        "tick_interval_seconds": 1,
        "max_actions_per_tick": 3,
        "idle_after_ticks": 5,
    },
    "memory": {
        "db_path": ":memory:",
        "working": {"max_tokens": 50000},
        "interaction_idle_timeout_sec": _TEST_IDLE_TIMEOUT_SEC,
    },
    "relationships": [],
}


def _do_nothing_client() -> LLMClient:
    mock_provider = AsyncMock()
    mock_provider.create_message = AsyncMock(
        return_value=LLMResponse(
            text='```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
            stop_reason=StopReason.END_TURN,
            usage=Usage(10, 5),
        ),
    )
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: msgs,
    )
    return LLMClient(mock_provider)


async def _make_agent_with_clock(clock: FrozenClock) -> _LLMPersonaAgent:
    agent = create_persona_agent(
        agent_id=_PERSONA_CONFIG["id"],
        config=_PERSONA_CONFIG,
        llm_client=_do_nothing_client(),
        clock=clock,
    )
    await agent.initialize_memory()
    return agent


async def _all_episodes(agent: _LLMPersonaAgent) -> list[dict]:
    db = agent._episodic_memory._ensure_db()
    async with db.execute(
        """
        SELECT summary, interaction_id, started_at, closed_at,
               turn_count, scope, context_json
        FROM episodes
        WHERE agent_id = ?
        ORDER BY created_at
        """,
        (agent.agent_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        {
            "summary": r[0],
            "interaction_id": r[1],
            "started_at": r[2],
            "closed_at": r[3],
            "turn_count": r[4],
            "scope": r[5],
            "context_json": r[6],
        }
        for r in rows
    ]


# ─── PR-3 review #16: Clock seam wired to tracker ──────────────


@pytest.mark.asyncio
class TestTrackerClockSeam:
    """Pin the contract that InteractionTracker reads the persona's clock.

    PR 3 added a ``Clock`` Protocol seam at the tracker level
    (``InteractionTracker(clock=...)``) so tests inject a deterministic
    clock once at construction time.  But ``_LLMPersonaAgent`` constructed
    its tracker without forwarding ``clock=`` — production code was
    locked to ``time.time`` and tests had to inject via per-call
    ``now=`` overrides at every call site.  Slice 5 forwards
    ``self._clock.now`` (the persona's :class:`agents.clock.Clock`
    instance) to the tracker so a single ``FrozenClock`` injection at
    ``create_persona_agent(clock=...)`` flows through both the prompt
    layer and the interaction tracker.
    """

    async def test_tracker_uses_persona_clock(self):
        """Tracker's default-now reads the persona's clock, not time.time()."""
        clock = FrozenClock(at=1_000.0)
        agent = await _make_agent_with_clock(clock)
        # No ``now=`` override → tracker default-now should equal the
        # FrozenClock's pinned epoch.
        peer = "iron-fox"
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hello"},
            sender_id=peer,
        ))
        scope = scope_for_dm(agent.agent_id, peer)
        interaction = agent._interaction_tracker.get(scope)
        assert interaction is not None
        # The turn's timestamp is read from the tracker's clock; pinning
        # this to the FrozenClock value proves the seam is wired.
        assert interaction.last_turn_at == 1_000.0
        assert interaction.started_at == 1_000.0

    async def test_tracker_clock_advances_with_persona_clock(self):
        """``clock.advance`` shifts the tracker's default-now lock-step.

        The ``Clock`` Protocol in ``agents.memory.interactions`` is a
        bare ``() -> float`` callable; ``self._clock.now`` (a bound
        method on :class:`agents.clock.Clock`) satisfies it.  Verifying
        the wiring by advancing the FrozenClock and observing the tracker
        read the new value pins the runtime contract — a future regression
        that re-defaults the tracker to ``time.time`` would break this
        test before it shipped.
        """
        clock = FrozenClock(at=1_000.0)
        agent = await _make_agent_with_clock(clock)
        peer = "iron-fox"
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hello"},
            sender_id=peer,
        ))
        clock.advance(120.0)
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "still here"},
            sender_id=peer,
        ))
        scope = scope_for_dm(agent.agent_id, peer)
        interaction = agent._interaction_tracker.get(scope)
        assert interaction is not None
        assert interaction.last_turn_at == 1_120.0


# ─── PR-3 review #14: Cross-scope idle flush via on_event ──────


@pytest.mark.asyncio
class TestCrossScopeIdleFlushViaOnEvent:
    """The PR description's "production hot path" — pinned end-to-end.

    The original PR-3 suite drove ``InteractionTracker.idle_check`` and
    ``_persist_closed_interaction`` separately (see
    ``test_idle_gap_closes_interaction_and_next_turn_opens_new_one``).
    No test asserted that an event arriving in scope B flushes a stale
    scope A through ``_store_event_episode``'s top-of-handler idle
    sweep.  Slice 5 closes that gap by driving the full ``on_event``
    path with the new clock seam (PR-3 review #16) so no per-call
    ``now=`` plumbing is required.
    """

    async def test_event_in_scope_b_flushes_stale_scope_a(self):
        clock = FrozenClock(at=1_000.0)
        agent = await _make_agent_with_clock(clock)
        peer_a = "iron-fox"
        peer_b = "ember-owl"
        scope_a = scope_for_dm(agent.agent_id, peer_a)
        scope_b = scope_for_dm(agent.agent_id, peer_b)

        # Open scope A.
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hello A"},
            sender_id=peer_a,
        ))
        first_a = agent._interaction_tracker.get(scope_a)
        assert first_a is not None
        first_a_id = first_a.interaction_id

        # Advance the persona clock past the idle window.  Tracker reads
        # the same clock (PR-3 review #16) so its idle_check at the top
        # of the next event sees scope A as expired.
        clock.advance(_TEST_IDLE_TIMEOUT_SEC + 1.0)

        # Fire an event in scope B (different peer).  The runtime flushes
        # scope A in the same call BEFORE handling B.
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hello B"},
            sender_id=peer_b,
        ))
        await agent.drain_pending_summaries()

        # Scope A is persisted with REASON_IDLE_GAP.
        episodes = await _all_episodes(agent)
        assert len(episodes) == 1
        ep = episodes[0]
        assert ep["interaction_id"] == first_a_id
        assert ep["scope"] == scope_a
        # PR-3 review #14 deep-review Should-Fix (PR 299): assert the
        # *reason* of the close, not just that summarise+update ran.
        # The two-phase persistence path writes ``close_reason`` to
        # ``context_json`` at the sync INSERT (see
        # ``_persist_closed_interaction``), so reading it from the row
        # is the lowest-coupling way to pin the close-reason contract:
        # a regression that mis-attributed the close to ``structural``
        # or ``shutdown`` would leave the summary non-empty (the
        # background summariser still runs) and slip past the prior
        # ``summary != SUMMARY_UNAVAILABLE_TEXT`` check.
        ctx = json.loads(ep["context_json"])
        assert ctx["close_reason"] == REASON_IDLE_GAP
        # Complementary check: the two-phase summariser ran to
        # completion (UPDATE replaced the SUMMARY_PENDING_TEXT
        # placeholder with a non-empty fallback or LLM-generated text).
        from agents.memory.interactions import SUMMARY_UNAVAILABLE_TEXT
        assert ep["summary"]
        assert ep["summary"] != SUMMARY_UNAVAILABLE_TEXT

        # Scope B opens independently — fresh interaction, one turn.
        open_b = agent._interaction_tracker.get(scope_b)
        assert open_b is not None
        assert open_b.interaction_id != first_a_id
        assert open_b.turn_count == 1
        # Scope A is not in the tracker any more (closed-and-popped).
        assert agent._interaction_tracker.get(scope_a) is None


# ─── PR-3 review #13: Idle-flush failure logs the failed scope ──


@pytest.mark.asyncio
class TestIdleFlushFailureLogsCorrectScope:
    """Pin the corrected exception-handler attribution.

    Before slice 5 the cross-scope idle-flush loop sat inside the outer
    ``try/except`` of ``_store_event_episode``.  If
    ``_persist_closed_interaction`` raised past its own inner try (rare;
    ``asyncio.CancelledError`` or a programming error in
    ctx-construction), the outer handler logged
    ``event_type=<current event>`` — but the current event is *not* the
    event that owned the failed flush scope.  Operators reading the
    log saw a misattributed failure tied to an unrelated event_type.

    Slice 5 pulls the flush loop out from under the outer ``except``
    and gives it a per-iteration ``try/except`` that logs the failed
    scope's identity (scope label + interaction_id).
    """

    async def test_flush_failure_warning_names_failed_scope_not_current_event(
        self, caplog, monkeypatch,
    ):
        clock = FrozenClock(at=1_000.0)
        agent = await _make_agent_with_clock(clock)
        peer_a = "iron-fox"
        peer_b = "ember-owl"
        scope_a = scope_for_dm(agent.agent_id, peer_a)

        # Open scope A.
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hello A"},
            sender_id=peer_a,
        ))
        a_id = agent._interaction_tracker.get(scope_a).interaction_id

        # Advance past idle window so the next event triggers the flush.
        clock.advance(_TEST_IDLE_TIMEOUT_SEC + 1.0)

        # Patch ``_persist_closed_interaction`` to raise an exception
        # the outer handler would otherwise mis-attribute.  Simulates
        # a programming error in ctx-construction or a transient
        # failure mode that escapes the inner ``try``.
        async def _boom(_interaction):
            raise RuntimeError("simulated flush failure")

        monkeypatch.setattr(agent, "_persist_closed_interaction", _boom)

        with caplog.at_level(
            logging.WARNING,
            logger="agents.persona_runtime.episode_routing",
        ):
            # Fire event in scope B; the top-of-handler flush loop
            # should attempt scope A and fail.
            await agent.on_event(AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": "hello B"},
                sender_id=peer_b,
            ))

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        # The contract: a flush-attributed warning naming scope A and
        # its interaction_id, not a generic ``event_type=channel_message``
        # message that points at the in-flight event B.
        flush_warnings = [
            r for r in warnings if scope_a in r.getMessage()
        ]
        assert flush_warnings, (
            "must emit a warning naming the failed scope "
            f"({scope_a!r}); got messages: "
            f"{[r.getMessage() for r in warnings]}"
        )
        # The interaction_id of the failed scope must appear so
        # operators can correlate to the persisted ``closing`` row.
        assert any(
            a_id in r.getMessage() for r in flush_warnings
        ), (
            "flush-failure warning must include the failed "
            "interaction_id for log correlation"
        )

    async def test_flush_failure_does_not_block_current_event(
        self, monkeypatch,
    ):
        """A failed flush of scope A must not swallow scope B's open turn.

        The outer handler used to wrap *both* the flush loop and the
        new event's processing in a single try/except, so a flush failure
        could be confused with a current-event failure.  After slice 5
        the new event's path is unaffected by flush failures: scope B's
        first turn must still open the interaction in the tracker.
        """
        clock = FrozenClock(at=1_000.0)
        agent = await _make_agent_with_clock(clock)
        peer_a = "iron-fox"
        peer_b = "ember-owl"
        scope_b = scope_for_dm(agent.agent_id, peer_b)

        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hello A"},
            sender_id=peer_a,
        ))
        clock.advance(_TEST_IDLE_TIMEOUT_SEC + 1.0)

        async def _boom(_interaction):
            raise RuntimeError("simulated flush failure")

        monkeypatch.setattr(agent, "_persist_closed_interaction", _boom)

        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hello B"},
            sender_id=peer_b,
        ))
        # Scope B opened normally despite the flush failure.
        open_b = agent._interaction_tracker.get(scope_b)
        assert open_b is not None
        assert open_b.turn_count == 1
