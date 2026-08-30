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
from agents.memory.interaction_types import ROOM_CLOSE_TURN_KEY
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
    governance_interaction_id: str | None = None,
) -> None:
    # PR 2: the governance id is now a queryable column, not a context key.
    await mem.store_episode(
        summary=summary,
        context={"scope": scope, "close_reason": close_reason},
        interaction_id=interaction_id,
        governance_interaction_id=governance_interaction_id,
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


async def test_interaction_id_filter_matches_governance_id(memory):
    # ISSUE-0102 PR 2: the natural diagnostic move — paste the end-vote-closed
    # governance id into --interaction-id — must return the episode, even
    # though that id never equals the agent-side episode id.
    await _store_closed(
        memory, interaction_id="ep-1", scope="group:a", summary="converged",
        close_reason="structural", started_at=1.0, closed_at=2.0,
        governance_interaction_id="gov-4b332af1",
    )
    by_gov = await closed_interactions(
        memory, limit=10, interaction_id="gov-4b332af1",
    )
    assert [ep.interaction_id for ep in by_gov] == ["ep-1"]


async def test_governance_id_filter_returns_every_episode_of_one_arc(memory):
    # One governance interaction maps to several agent-side episodes (an idle
    # split inside the arc); filtering by the governance id returns them all,
    # newest-first — the cardinality the single-column design rests on.
    await _store_closed(
        memory, interaction_id="ep-early", scope="group:a", summary="part 1",
        close_reason="idle_gap", started_at=1.0, closed_at=2.0,
        governance_interaction_id="gov-X",
    )
    await _store_closed(
        memory, interaction_id="ep-late", scope="group:a", summary="part 2",
        close_reason="structural", started_at=3.0, closed_at=4.0,
        governance_interaction_id="gov-X",
    )
    by_gov = await closed_interactions(memory, limit=10, interaction_id="gov-X")
    assert [ep.interaction_id for ep in by_gov] == ["ep-late", "ep-early"]


async def test_agent_side_id_still_matches_when_governance_present(memory):
    # The OR must not break the original agent-side lookup: filtering by the
    # episode id returns exactly that episode, not its whole arc.
    await _store_closed(
        memory, interaction_id="ep-1", scope="group:a", summary="a",
        close_reason="structural", started_at=1.0, closed_at=2.0,
        governance_interaction_id="gov-X",
    )
    await _store_closed(
        memory, interaction_id="ep-2", scope="group:a", summary="b",
        close_reason="structural", started_at=3.0, closed_at=4.0,
        governance_interaction_id="gov-X",
    )
    by_ep = await closed_interactions(memory, limit=10, interaction_id="ep-1")
    assert [ep.interaction_id for ep in by_ep] == ["ep-1"]


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


async def test_recall_min_turns_excludes_single_turn(memory):
    """``min_turns`` lets a caller drop the degenerate single-turn rows.

    Single-turn closes (tick / task / approval) are legitimately closed
    interactions and stay retrievable by default (``min_turns`` unset → 1,
    per the plan's ``turn_count=1`` contract), but an unscoped list would
    otherwise be flooded by their per-event envelopes. ``min_turns=2``
    restricts the list to genuine multi-turn interactions.
    """
    await _store_closed(
        memory, interaction_id="i-single", scope="group:a", summary="tick env",
        close_reason="structural", started_at=1.0, closed_at=2.0, turn_count=1,
    )
    await _store_closed(
        memory, interaction_id="i-multi", scope="group:a", summary="brainstorm",
        close_reason="cost", started_at=3.0, closed_at=4.0, turn_count=4,
    )
    # Default (min_turns unset → 1): both rows, the single-turn included.
    assert {ep.interaction_id for ep in await closed_interactions(memory, limit=10)} == {
        "i-single", "i-multi",
    }
    # min_turns=2: only the multi-turn interaction.
    only_multi = await closed_interactions(memory, limit=10, min_turns=2)
    assert [ep.interaction_id for ep in only_multi] == ["i-multi"]


async def test_recall_excludes_empty_summary(memory):
    """A blank summary is excluded per the §D ``summary != ''`` filter.

    ``store_episode`` itself rejects an empty summary, so this filter is
    defense-in-depth against a raw row that bypasses that guard (a future
    write path / a migration). We force the condition with a direct
    ``UPDATE`` to prove the read query drops it. The failure sentinel
    (non-empty) still surfaces (SS3); only a blank — which carries no
    information and is not a result — is dropped.
    """
    await _store_closed(
        memory, interaction_id="i-blank", scope="group:a", summary="placeholder",
        close_reason="cost", started_at=1.0, closed_at=2.0,
    )
    await _store_closed(
        memory, interaction_id="i-real", scope="group:a", summary="real",
        close_reason="cost", started_at=3.0, closed_at=4.0,
    )
    db = memory._ensure_db()
    await db.execute(
        "UPDATE episodes SET summary = '' WHERE interaction_id = ?", ("i-blank",),
    )
    await db.commit()

    rows = await closed_interactions(memory, limit=10)
    assert [ep.interaction_id for ep in rows] == ["i-real"]


# ─── gRPC handler layer ───────────────────────────────────────────────────────


def _fake_agent(mem: EpisodicMemory) -> MagicMock:
    agent = MagicMock()
    agent.memory.episodic = mem
    return agent


async def test_handler_projects_summary_and_trigger(memory):
    await _store_closed(
        memory, interaction_id="i-1", scope="group:room-7", summary="converged",
        close_reason="cost", started_at=10.0, closed_at=20.0, turn_count=5,
        governance_interaction_id="gov-4b332af1",
    )
    agents = {"agent-x": _fake_agent(memory)}
    ctx = MagicMock()

    resp = await handle_get_closed_interactions(
        agents, task_pb2.ClosedInteractionsRequest(agent_id="agent-x"), ctx,
    )
    assert len(resp.interactions) == 1
    it = resp.interactions[0]
    assert it.interaction_id == "i-1"
    # ISSUE-0102 PR 2: the RFC 0030 governance id is read from its queryable
    # column and surfaced as a distinct field from the agent-side interaction_id.
    assert it.governance_interaction_id == "gov-4b332af1"
    assert it.scope == "group:room-7"
    assert it.summary == "converged"
    assert it.close_reason == "cost"
    assert it.turn_count == 5
    assert it.started_at == 10.0
    assert it.closed_at == 20.0


async def test_handler_governance_id_empty_for_legacy_row(memory):
    """ISSUE-0102: a pre-fix row persisted before the field existed has the
    key *absent* from the context blob — the handler's ``.get`` default
    surfaces an empty string, not an error or a missing-key crash."""
    await _store_closed(
        memory, interaction_id="i-legacy", scope="group:room-7", summary="wrap-up",
        close_reason="structural", started_at=1.0, closed_at=2.0,
        # governance_interaction_id omitted → key absent from context (a row
        # written before close_path.py started stamping the field).
    )
    agents = {"agent-x": _fake_agent(memory)}
    resp = await handle_get_closed_interactions(
        agents, task_pb2.ClosedInteractionsRequest(agent_id="agent-x"), MagicMock(),
    )
    assert resp.interactions[0].governance_interaction_id == ""


async def test_handler_governance_id_empty_for_real_dm_close(memory):
    """ISSUE-0102: a DM / thread / non-channel close is the production shape
    the legacy test missed — ``close_path.py`` *always* writes the key, but
    ``wire_interaction_id`` is "" for a scope that carried no governance id, so
    the persisted context holds the key *present with an empty value*. That
    distinct shape (present-but-empty, not absent) must also surface ""."""
    await _store_closed(
        memory, interaction_id="i-dm", scope="dm:agent-x:peer", summary="wrap-up",
        close_reason="structural", started_at=1.0, closed_at=2.0,
        governance_interaction_id="",  # key present, value "" — the real DM close.
    )
    agents = {"agent-x": _fake_agent(memory)}
    resp = await handle_get_closed_interactions(
        agents, task_pb2.ClosedInteractionsRequest(agent_id="agent-x"), MagicMock(),
    )
    assert resp.interactions[0].governance_interaction_id == ""


async def test_handler_falls_back_to_context_when_column_null(memory):
    """ISSUE-0102 PR 2 mixed-version safety: a row whose governance-id
    *column* is NULL but whose context blob still carries the id (a PR-1-shaped
    row written by an older agent process after the v15 schema landed, before
    the backfill or a fresh write populated the column) still surfaces the id
    via the context fallback — the column is preferred, context is the safety
    net."""
    await memory.store_episode(
        summary="converged", interaction_id="i-ctx", scope="group:room-7",
        started_at=1.0, closed_at=2.0, turn_count=3,
        # Column left NULL (param omitted); id only in the context blob.
        context={
            "scope": "group:room-7", "close_reason": "structural",
            "governance_interaction_id": "gov-ctx-only",
        },
    )
    agents = {"agent-x": _fake_agent(memory)}
    resp = await handle_get_closed_interactions(
        agents, task_pb2.ClosedInteractionsRequest(agent_id="agent-x"), MagicMock(),
    )
    assert resp.interactions[0].governance_interaction_id == "gov-ctx-only"


async def test_handler_projects_participants_from_multi_turn_context(memory):
    """Participants are the distinct turn senders, first-seen order, deduped."""
    await memory.store_episode(
        summary="brainstorm", interaction_id="i-1", scope="group:room-7",
        started_at=1.0, closed_at=2.0, turn_count=3,
        context={
            "scope": "group:room-7", "close_reason": "cost",
            "turns": [
                {"at": 1.0, "payload": {"sender": "alice"}},
                {"at": 1.5, "payload": {"sender": "bob"}},
                {"at": 1.9, "payload": {"sender": "alice"}},  # dup
            ],
        },
    )
    agents = {"agent-x": _fake_agent(memory)}
    resp = await handle_get_closed_interactions(
        agents, task_pb2.ClosedInteractionsRequest(agent_id="agent-x"), MagicMock(),
    )
    assert list(resp.interactions[0].participants) == ["alice", "bob"]


async def test_handler_excludes_the_room_close_turn_from_participants(memory):
    """PR #846 review: the room-close fan lands ONE closing message as the
    final turn of EVERY sibling record, so without this every record in a
    closed room named its own speaker plus whoever closed the room — a
    participant of that record's conversation only in the sense that it
    ended it.  Keyed off the producer's ``room_close`` stamp, not a
    re-derivation of ``sender`` != the record's speaker."""
    await memory.store_episode(
        summary="brainstorm", interaction_id="i-1", scope="group:room-7",
        started_at=1.0, closed_at=2.0, turn_count=2,
        context={
            "scope": "group:room-7", "close_reason": "structural",
            "turns": [
                {"at": 1.0, "payload": {"sender": "alice"}},
                {"at": 1.9, "payload": {
                    "sender": "iron-fox", ROOM_CLOSE_TURN_KEY: True,
                }},
            ],
        },
    )
    agents = {"agent-x": _fake_agent(memory)}
    resp = await handle_get_closed_interactions(
        agents, task_pb2.ClosedInteractionsRequest(agent_id="agent-x"), MagicMock(),
    )
    assert list(resp.interactions[0].participants) == ["alice"]


async def test_handler_projects_participants_from_single_turn_context(memory):
    """Single-turn rows carry the bare ``sender`` (no ``turns`` list)."""
    await memory.store_episode(
        summary="Event: mention → Actions: ['reply']", interaction_id="i-1",
        scope="mention", started_at=1.0, closed_at=2.0, turn_count=1,
        context={"event": {}, "sender": "carol", "close_reason": "structural"},
    )
    agents = {"agent-x": _fake_agent(memory)}
    resp = await handle_get_closed_interactions(
        agents, task_pb2.ClosedInteractionsRequest(agent_id="agent-x"), MagicMock(),
    )
    assert list(resp.interactions[0].participants) == ["carol"]


async def test_handler_threads_min_turns(memory):
    await _store_closed(
        memory, interaction_id="i-single", scope="group:a", summary="env",
        close_reason="structural", started_at=1.0, closed_at=2.0, turn_count=1,
    )
    await _store_closed(
        memory, interaction_id="i-multi", scope="group:a", summary="brainstorm",
        close_reason="cost", started_at=3.0, closed_at=4.0, turn_count=4,
    )
    agents = {"agent-x": _fake_agent(memory)}
    resp = await handle_get_closed_interactions(
        agents,
        task_pb2.ClosedInteractionsRequest(agent_id="agent-x", min_turns=2),
        MagicMock(),
    )
    assert [it.interaction_id for it in resp.interactions] == ["i-multi"]


async def test_handler_threads_scope_interaction_id_and_default_limit(monkeypatch):
    """scope / interaction_id thread to the query; an omitted limit defaults.

    ``test_handler_threads_min_turns`` pins ``min_turns`` end-to-end, but the
    remaining request fields the handler forwards — ``scope``,
    ``interaction_id``, and the ``limit or DEFAULT`` substitution — are only
    covered Go-side. Capture the kwargs the handler hands ``closed_interactions``
    so the full request→query plumbing is pinned in Python too. The empty-string
    proto defaults must arrive as ``None`` (the query's "no filter" sentinel),
    not as literal empty-string filters that would match nothing.
    """
    import agents.closed_interactions_read as mod

    captured: dict[str, object] = {}

    async def _fake_closed_interactions(_episodic, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(mod, "closed_interactions", _fake_closed_interactions)
    agents = {"agent-x": _fake_agent(MagicMock())}

    # interaction_id + scope present, limit omitted → DEFAULT.
    await handle_get_closed_interactions(
        agents,
        task_pb2.ClosedInteractionsRequest(
            agent_id="agent-x", scope="group:room-7", interaction_id="i-42",
        ),
        MagicMock(),
    )
    assert captured["scope"] == "group:room-7"
    assert captured["interaction_id"] == "i-42"
    assert captured["limit"] == mod.DEFAULT_CLOSED_INTERACTION_LIMIT

    # Absent scope / interaction_id (proto empty string) → None, not "".
    captured.clear()
    await handle_get_closed_interactions(
        agents,
        task_pb2.ClosedInteractionsRequest(agent_id="agent-x", limit=7),
        MagicMock(),
    )
    assert captured["scope"] is None
    assert captured["interaction_id"] is None
    assert captured["limit"] == 7


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
