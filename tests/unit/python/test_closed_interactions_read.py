"""v0.3.8 interaction-summary surface — the closed-interaction read path.

Covers the two read-side seams PR 1 adds:

* :func:`agents.memory.episodic_closed.closed_interactions` /
  :func:`agents.memory.episodic_closed.recall_closed_interactions` — the
  query that returns persisted RFC 0020 per-interaction summaries
  (``closed_at`` populated) newest-first, with optional scope /
  interaction_id filters, including the failure sentinel (SS3).
* :func:`agents.closed_interactions_read.handle_get_closed_interactions`
  — the gRPC handler that projects those rows onto the
  ``ClosedInteraction`` wire message (summary + close trigger + metadata)
  and degrades gracefully on a missing / memory-less agent.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import grpc
import pytest

from agents.closed_interactions_read import handle_get_closed_interactions
from agents.generated import task_pb2
from agents.memory.episodic import EpisodicMemory
from agents.memory.episodic_closed import closed_interactions
from agents.memory.interactions import (
    SUMMARY_PENDING_TEXT,
    SUMMARY_UNAVAILABLE_TEXT,
)


async def _store_closed(
    mem: EpisodicMemory,
    *,
    interaction_id: str,
    scope: str,
    summary: str,
    close_reason: str,
    started_at: float,
    closed_at: float,
    turn_count: int = 3,
) -> None:
    await mem.store_episode(
        summary=summary,
        context={"scope": scope, "close_reason": close_reason},
        interaction_id=interaction_id,
        started_at=started_at,
        closed_at=closed_at,
        turn_count=turn_count,
        scope=scope,
    )


@pytest.fixture
async def memory():
    mem = EpisodicMemory(agent_id="agent-x", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


# ─── Query layer ──────────────────────────────────────────────────────────────


async def test_recall_returns_only_closed_in_recency_order(memory):
    await _store_closed(
        memory, interaction_id="i-old", scope="group:a", summary="old",
        close_reason="idle_gap", started_at=10.0, closed_at=100.0,
    )
    await _store_closed(
        memory, interaction_id="i-new", scope="group:a", summary="new",
        close_reason="cost", started_at=200.0, closed_at=300.0,
    )
    # An *open* interaction (closed_at NULL) must be excluded.
    await memory.store_episode(
        summary="still open", context={"scope": "group:a"},
        interaction_id="i-open", started_at=400.0, closed_at=None,
        scope="group:a",
    )

    rows = await closed_interactions(memory, limit=10)
    ids = [ep.interaction_id for ep in rows]
    assert ids == ["i-new", "i-old"]  # newest closed first; open excluded


async def test_recall_filters_by_scope_and_interaction_id(memory):
    await _store_closed(
        memory, interaction_id="i-1", scope="group:a", summary="a",
        close_reason="cost", started_at=1.0, closed_at=2.0,
    )
    await _store_closed(
        memory, interaction_id="i-2", scope="group:b", summary="b",
        close_reason="idle_gap", started_at=3.0, closed_at=4.0,
    )

    by_scope = await closed_interactions(memory, limit=10, scope="group:b")
    assert [ep.interaction_id for ep in by_scope] == ["i-2"]

    by_id = await closed_interactions(memory, limit=10, interaction_id="i-1")
    assert [ep.interaction_id for ep in by_id] == ["i-1"]


async def test_recall_excludes_unfinalised_pending_rows(memory):
    """An unfinalised Phase-1 ``closing`` row must not surface.

    The close path is a two-phase write: Phase 1 INSERTs the row with
    ``closed_at`` populated but ``summary == SUMMARY_PENDING_TEXT``
    ("[summary pending]"); Phase 2 UPDATEs the real summary in the
    background. ``SUMMARY_PENDING_TEXT`` is an internal placeholder, not
    a result — the normal recall chokepoint (``episodic.py``) drops it,
    and this read surface must too, or the web console / CLI would show
    "[summary pending]" during the (observable) summarise window and
    indefinitely on a crash-before-Phase-2. The *finalised* failure
    sentinel (``SUMMARY_UNAVAILABLE_TEXT``) stays visible (SS3) — that is
    the separate ``test_recall_surfaces_failure_sentinel`` contract.
    """
    await _store_closed(
        memory, interaction_id="i-pending", scope="group:a",
        summary=SUMMARY_PENDING_TEXT, close_reason="cost",
        started_at=1.0, closed_at=2.0,
    )
    await _store_closed(
        memory, interaction_id="i-done", scope="group:a",
        summary="real summary", close_reason="cost",
        started_at=3.0, closed_at=4.0,
    )
    rows = await closed_interactions(memory, limit=10)
    assert [ep.interaction_id for ep in rows] == ["i-done"]


async def test_recall_surfaces_failure_sentinel(memory):
    await _store_closed(
        memory, interaction_id="i-fail", scope="group:a",
        summary=SUMMARY_UNAVAILABLE_TEXT, close_reason="cost",
        started_at=1.0, closed_at=2.0,
    )
    rows = await closed_interactions(memory, limit=10)
    assert len(rows) == 1
    # SS3: a failed summary is surfaced honestly, not filtered out.
    assert rows[0].summary == SUMMARY_UNAVAILABLE_TEXT


# ─── gRPC handler layer ───────────────────────────────────────────────────────


def _fake_agent(mem: EpisodicMemory) -> MagicMock:
    agent = MagicMock()
    agent.memory.episodic = mem
    return agent


async def test_handler_projects_summary_and_trigger(memory):
    await _store_closed(
        memory, interaction_id="i-1", scope="group:room-7", summary="converged",
        close_reason="cost", started_at=10.0, closed_at=20.0, turn_count=5,
    )
    agents = {"agent-x": _fake_agent(memory)}
    ctx = MagicMock()

    resp = await handle_get_closed_interactions(
        agents, task_pb2.ClosedInteractionsRequest(agent_id="agent-x"), ctx,
    )
    assert len(resp.interactions) == 1
    it = resp.interactions[0]
    assert it.interaction_id == "i-1"
    assert it.scope == "group:room-7"
    assert it.summary == "converged"
    assert it.close_reason == "cost"
    assert it.turn_count == 5
    assert it.closed_at == 20.0


async def test_handler_missing_agent_is_not_found():
    ctx = MagicMock()
    resp = await handle_get_closed_interactions(
        {}, task_pb2.ClosedInteractionsRequest(agent_id="nope"), ctx,
    )
    ctx.set_code.assert_called_once_with(grpc.StatusCode.NOT_FOUND)
    assert list(resp.interactions) == []


async def test_handler_empty_agent_id_is_invalid_argument():
    ctx = MagicMock()
    resp = await handle_get_closed_interactions(
        {}, task_pb2.ClosedInteractionsRequest(agent_id=""), ctx,
    )
    ctx.set_code.assert_called_once_with(grpc.StatusCode.INVALID_ARGUMENT)
    assert list(resp.interactions) == []


async def test_handler_memory_less_agent_returns_empty():
    # A task agent: agent.memory has no `episodic` tier.
    agent = MagicMock()
    agent.memory = object()  # no `.episodic`
    ctx = MagicMock()
    resp = await handle_get_closed_interactions(
        {"t": agent}, task_pb2.ClosedInteractionsRequest(agent_id="t"), ctx,
    )
    assert list(resp.interactions) == []
    ctx.set_code.assert_not_called()
