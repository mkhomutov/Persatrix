"""
RFC 0020 PR 2 — single-turn routing parity test.

Verifies that TICK and tool-only events route through the
:class:`~agents.memory.interactions.InteractionTracker` and produce
exactly one closed-interaction episode each, with the same per-event
summary text the pre-RFC store would have written (RFC 0020 §C "Phase 1"
behavioral parity).

What "parity" means here:

* **Episode count** — N TICK events still produce N episodes.  No
  aggregation, no batching.  This guards against PR 3's multi-turn
  aggregation accidentally collapsing single-turn paths.
* **Summary text** — unchanged from the pre-RFC ``Event: <type> →
  Actions: [...]`` shape.  PR 4 introduces LLM-generated summaries for
  multi-turn interactions; single-turn rows keep the cheap deterministic
  text per RFC 0020 §C summary-text-by-phase table.
* **Interaction columns** — ``interaction_id`` / ``started_at`` /
  ``closed_at`` are populated, ``turn_count == 1``, and ``scope`` is
  the event-type label (``"tick"`` for ``EventType.TICK``,
  ``"task_assigned"`` for ``EventType.TASK_ASSIGNED``, etc., per
  PR-215 review Should-Fix #1).
  Multi-turn paths (``MESSAGE_RECEIVED`` / ``MENTION``) are deferred to
  PR 3 and continue to land with NULL interaction columns; that legacy
  shape is asserted here to keep the PR 3 boundary explicit.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import AgentEvent, EventType
from agents.tools.registry import clear_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


_PERSONA_CONFIG: dict = {
    "id": "parity-persona",
    "model": "test-model",
    "role": "Parity test persona",
    "type": "persona",
    "max_llm_calls": 5,
    "max_tokens": 1024,
    "tools": [],
    "persona": {
        "name": "Parity Agent",
        "background": "A persona used by the RFC 0020 PR 2 parity test.",
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
    },
    "relationships": [],
}


def _do_nothing_client() -> LLMClient:
    """Mock client whose every reply parses to a single DO_NOTHING action.

    Single-turn parity does not depend on action shape — only on episode
    count and column population — so the cheapest possible response is
    used to keep the test deterministic.
    """
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


async def _make_agent() -> _LLMPersonaAgent:
    agent = create_persona_agent(
        agent_id=_PERSONA_CONFIG["id"],
        config=_PERSONA_CONFIG,
        llm_client=_do_nothing_client(),
    )
    await agent.initialize_memory()
    return agent


async def _all_episodes(agent: _LLMPersonaAgent) -> list[dict]:
    """Read every episode row directly so we can assert on the new
    interaction columns without going through the recall scorer (which
    filters NULL-summary rows and applies the §I boost).
    """
    db = agent._episodic_memory._ensure_db()
    async with db.execute(
        """
        SELECT summary, interaction_id, started_at, closed_at,
               turn_count, scope
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
        }
        for r in rows
    ]


# ─── Single-turn parity ──────────────────────────────────────


@pytest.mark.asyncio
class TestSingleTurnParity:
    """RFC 0020 §C Phase 1 deliverable 4 — TICK + tool-only routed."""

    async def test_n_ticks_produce_n_closed_episodes(self):
        """Episode count after N TICKs equals N (parity vs. pre-RFC)."""
        agent = await _make_agent()
        # Bypass the RFC 0017 §F empty-context TICK short-circuit so the
        # tick actually reaches the LLM call + episode-store path the
        # parity invariant is asserting against.  ``recent_context`` is
        # the cheapest of the three short-circuit gates to defeat (no
        # LLM mock changes, no goal_progress bookkeeping).
        agent._state.recent_context.append("prior turn context")
        n = 5
        for _ in range(n):
            await agent.on_tick()

        episodes = await _all_episodes(agent)
        assert len(episodes) == n

        # Every TICK row must be a closed single-turn interaction.
        interaction_ids = set()
        for ep in episodes:
            assert ep["interaction_id"], (
                "TICK episode missing interaction_id — single-turn paths "
                "must route through InteractionTracker (RFC 0020 §G)"
            )
            assert ep["turn_count"] == 1
            assert ep["scope"] == "tick"
            assert ep["started_at"] is not None
            assert ep["closed_at"] is not None
            assert ep["closed_at"] >= ep["started_at"]
            assert ep["summary"].startswith("Event: tick → Actions:")
            interaction_ids.add(ep["interaction_id"])

        # Each TICK must produce a *fresh* interaction — the close path
        # removes the scope from the open map per RFC 0020 §C "do not
        # reopen", so subsequent ticks must allocate a new interaction_id.
        assert len(interaction_ids) == n

    async def test_tool_only_event_produces_closed_single_turn_episode(self):
        """A tool-only event (no inbound message) routes through the tracker.

        Per RFC 0020 §G, "Tool-only invocations (no inbound message)"
        share the TICK boundary policy — single-turn, structural close.
        ``TASK_ASSIGNED`` is the canonical tool-only event in v0.3.0:
        no ``sender_id``, no chat continuation expected.

        PR-215 review (Should-Fix #1): the persisted ``scope`` carries
        the event-type label (``"task_assigned"``) rather than the bare
        ``"tick"`` string.  ``SCOPE_TICK`` is reserved for actual
        ``EventType.TICK`` events so the column preserves provenance for
        analytics.
        """
        agent = await _make_agent()
        event = AgentEvent(
            event_type=EventType.TASK_ASSIGNED,
            payload={"task": "Fetch the build status."},
        )
        await agent.on_event(event)

        episodes = await _all_episodes(agent)
        assert len(episodes) == 1
        ep = episodes[0]
        assert ep["interaction_id"]
        assert ep["turn_count"] == 1
        assert ep["scope"] == "task_assigned"
        assert ep["closed_at"] is not None
        assert ep["summary"].startswith("Event: task_assigned → Actions:")

    async def test_multi_turn_events_keep_legacy_shape(self):
        """Multi-turn paths land with NULL interaction columns until PR 3.

        This pins the PR 2/PR 3 boundary: ``MESSAGE_RECEIVED`` and
        ``MENTION`` continue to write pre-RFC-shaped rows (no
        ``interaction_id``), so the PR 3 multi-turn aggregation has a
        clean before/after to compare against.

        PR-215 review nice-to-have #4: ``MENTION`` is exercised here
        alongside ``MESSAGE_RECEIVED`` so the symmetry of the
        ``_MULTI_TURN_EVENT_TYPES`` deny-list is enforced explicitly
        rather than implied by the membership check.
        """
        for event_type, payload, sender in (
            (EventType.MESSAGE_RECEIVED, {"content": "Quick question."}, "iron-fox"),
            (EventType.MENTION, {"content": "@parity-persona ping"}, "iron-fox"),
        ):
            agent = await _make_agent()
            await agent.on_event(AgentEvent(
                event_type=event_type,
                payload=payload,
                sender_id=sender,
            ))

            episodes = await _all_episodes(agent)
            assert len(episodes) == 1
            ep = episodes[0]
            assert ep["interaction_id"] is None, (
                f"{event_type.value} must keep the pre-RFC episode shape "
                "until PR 3 wires multi-turn aggregation (RFC 0020 PR plan §PR 2)"
            )
            assert ep["turn_count"] is None
            assert ep["scope"] is None
            assert ep["summary"].startswith(f"Event: {event_type.value} → Actions:")

    async def test_mixed_event_stream_preserves_per_event_count(self):
        """A mixed stream (TICK + TASK_ASSIGNED + MESSAGE_RECEIVED) yields
        one episode per event — the parity invariant the PR 2 plan
        commits to (§PR 2 "Episode count after N TICKs equals N")."""
        agent = await _make_agent()
        # See ``test_n_ticks_produce_n_closed_episodes`` for the
        # rationale on bypassing the empty-context TICK short-circuit.
        agent._state.recent_context.append("prior turn context")
        await agent.on_tick()
        await agent.on_event(AgentEvent(
            event_type=EventType.TASK_ASSIGNED,
            payload={"task": "noop"},
        ))
        await agent.on_event(AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "hi"},
            sender_id="peer-agent",
        ))
        await agent.on_tick()

        episodes = await _all_episodes(agent)
        assert len(episodes) == 4

        # TICK + TASK_ASSIGNED + TICK route through the tracker; the
        # MESSAGE_RECEIVED row stays legacy (NULL interaction_id).
        single_turn = [e for e in episodes if e["interaction_id"] is not None]
        legacy = [e for e in episodes if e["interaction_id"] is None]
        assert len(single_turn) == 3
        assert len(legacy) == 1
        assert all(e["turn_count"] == 1 for e in single_turn)

    async def test_store_episode_failure_is_swallowed_and_logged(self, caplog):
        """Persistence failure must not bubble; tracker must not leak.

        PR-215 review (Should-Fix #3) — the only meaningful coverage gap
        in the original parity suite was the exception path in
        ``_store_event_episode``.  PR 1 introduced
        ``interactions.closed.by_structural`` counter increments inside
        ``InteractionTracker.close``; if ``store_episode`` then fails,
        the counter has already fired and the scope has already been
        popped from the open map.  This test pins three contracts:

        1. The exception is swallowed (parity with pre-RFC behavior).
        2. The tracker has no dangling open scope after failure
           (``close`` ran before ``store_episode`` raised, so the scope
           must not appear in ``open_scopes``).
        3. A warning is emitted carrying the ``event_type`` so an
           operator can correlate the metric increment to the missing
           row (PR-215 review nice-to-have #3).
        """
        import logging

        agent = await _make_agent()

        async def _boom(**_kwargs):
            raise RuntimeError("simulated SQLite I/O failure")

        agent._episodic_memory.store_episode = _boom  # type: ignore[assignment]

        with caplog.at_level(logging.WARNING, logger="agents.persona_runtime.state_persistence"):
            # Must not raise.
            await agent.on_event(AgentEvent(
                event_type=EventType.TASK_ASSIGNED,
                payload={"task": "will fail to persist"},
            ))

        # Tracker contract: ``close`` ran before ``store_episode`` raised,
        # so the scope was popped — no dangling open interaction.
        assert agent._interaction_tracker.open_scopes() == []

        # Warning was emitted with the event_type for correlation.
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("task_assigned" in r.getMessage() for r in warnings), (
            "warning must include event_type so operators can correlate "
            "the interactions.closed.by_structural counter increment with "
            "the missing episode row"
        )
