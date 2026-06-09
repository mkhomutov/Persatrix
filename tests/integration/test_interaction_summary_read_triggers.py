"""v0.3.8 interaction-summary surface — every close trigger is reachable
through the read API, carrying its trigger (PR-583 review follow-up).

The plan's PR 1 TDD matrix requires that *each* close trigger leaves a
``closed``/``summarized`` row reachable by the read API **with its close
trigger**, and that a ``turn_count == 1`` row returns the degenerate
per-event summary. The original suite only exercised the cost trigger
(``test_interaction_close_on_cost.py``) and the recency/scope/sentinel
query shape (``test_closed_interactions_read.py``) — it never drove an
*idle* or *structural* close through the real runtime and out the read
API, and it had no ``turn_count == 1`` case.

This module closes that gap by driving the production close paths
(``_LLMPersonaAgent.on_event`` / ``on_tick`` → ``InteractionTracker`` →
``_persist_closed_interaction``) and then reading the result back through
the real gRPC handler ``handle_get_closed_interactions``:

* **idle** — a stale scope flushed by the cross-scope idle sweep surfaces
  ``close_reason == "idle_gap"``.
* **structural** — a ``chat_end`` session close surfaces
  ``close_reason == "structural"`` and lists its participants.
* **single-turn** — a TICK surfaces ``turn_count == 1``, the deterministic
  ``Event: tick → Actions: [...]`` envelope, and (PR-583 review) its
  ``close_reason == "structural"`` rather than a blank — the single-turn
  episode-write path now persists the reason into the context blob like
  the multi-turn close path always has.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agents.clock import FrozenClock
from agents.generated import task_pb2
from agents.memory.scopes import scope_for_dm
from agents.persona_types import AgentEvent, EventType
from agents.tools.registry import clear_registry

from ._interaction_multi_turn_helpers import (
    TEST_IDLE_TIMEOUT_SEC,
    make_agent_with_clock,
    persona_config,
)
from ._persona_parity_helpers import make_agent as make_parity_agent


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


async def _read(agent) -> list[task_pb2.ClosedInteraction]:
    """Read closed interactions through the production gRPC handler.

    Importing the handler lazily keeps this module's import graph small
    and mirrors how the servicer reaches it.
    """
    from agents.closed_interactions_read import handle_get_closed_interactions

    resp = await handle_get_closed_interactions(
        {agent.agent_id: agent},
        task_pb2.ClosedInteractionsRequest(agent_id=agent.agent_id),
        MagicMock(),
    )
    return list(resp.interactions)


async def test_idle_close_is_reachable_with_trigger():
    """An idle-flushed interaction surfaces ``close_reason == "idle_gap"``."""
    clock = FrozenClock(at=1_000.0)
    agent = await make_agent_with_clock(clock)
    try:
        peer_a, peer_b = "peer-a", "peer-b"
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hello"}, sender_id=peer_a,
        ))
        # Advance past the idle window so the next event flushes scope A.
        clock.advance(TEST_IDLE_TIMEOUT_SEC + 1.0)
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hi"}, sender_id=peer_b,
        ))
        await agent.drain_pending_summaries()

        scope_a = scope_for_dm(agent.agent_id, peer_a)
        rows = await _read(agent)
        idle_row = next(r for r in rows if r.scope == scope_a)
        assert idle_row.close_reason == "idle_gap"
        assert idle_row.summary
        assert list(idle_row.participants) == [peer_a]
    finally:
        await agent.close_memory()


async def test_structural_close_is_reachable_with_trigger_and_participants():
    """A ``chat_end`` close surfaces ``structural`` + its participants."""
    clock = FrozenClock(at=2_000.0)
    agent = await make_agent_with_clock(clock, config=persona_config(
        agent_id="structural-read-persona",
    ))
    try:
        peer = "iron-fox"
        for i in range(3):
            await agent.on_event(AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": f"turn {i}"}, sender_id=peer,
            ))
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "thanks, bye"}, sender_id=peer,
            metadata={"chat_end": True},
        ))
        await agent.drain_pending_summaries()

        rows = await _read(agent)
        assert len(rows) == 1
        assert rows[0].close_reason == "structural"
        assert rows[0].turn_count == 4
        assert list(rows[0].participants) == [peer]
    finally:
        await agent.close_memory()


async def test_single_turn_close_surfaces_trigger_and_degenerate_summary():
    """PR-583 review: a single-turn TICK row carries its trigger, not a blank.

    Plan's ``turn_count == 1`` contract: the row is returnable and carries
    the deterministic per-event summary. The single-turn write path used to
    omit ``close_reason`` from the persisted context (only the multi-turn
    close path persisted it), so the read API reported ``close_reason == ""``
    for every tick/task/approval. The fix mirrors the multi-turn path.
    """
    agent = await make_parity_agent()
    try:
        # Defeat the RFC 0017 §F empty-context TICK short-circuit so the
        # tick reaches the episode-store path (same trick as the parity suite).
        agent._state.recent_context.append("prior turn context")
        await agent.on_tick()
        await agent.drain_pending_summaries()

        rows = await _read(agent)
        assert len(rows) == 1
        row = rows[0]
        assert row.turn_count == 1
        assert row.scope == "tick"
        assert row.summary.startswith("Event: tick → Actions:")
        assert row.close_reason == "structural"
    finally:
        await agent.close_memory()
