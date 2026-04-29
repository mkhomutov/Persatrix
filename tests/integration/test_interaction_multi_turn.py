"""
RFC 0020 PR 3 \u2014 multi-turn aggregation integration tests.

Pins the PR 3 deliverables called out in
``docs/rfcs/0020-pr-plan.md`` \u00a7PR 3:

* Ten turns from the same chat session collapse into one interaction
  and produce a single closed-interaction episode on session end.
* Idle-gap closure: a clock-advance past the configured idle timeout
  produces a closed interaction; the next turn opens a fresh one.
* DM scope keying is symmetric: A\u2192B and B\u2192A in a DM accumulate into
  the same interaction.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

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


# Short idle timeout keeps the idle-gap test cheap \u2014 the production
# default is 600s (RFC 0020 \u00a7B); tests use 5s so a fake clock-advance
# of 10s is unambiguously past the threshold without making the test
# wait for real wall-clock time.
_TEST_IDLE_TIMEOUT_SEC: float = 5.0


_PERSONA_CONFIG: dict = {
    "id": "multi-turn-persona",
    "model": "test-model",
    "role": "Multi-turn aggregation test persona",
    "type": "persona",
    "max_llm_calls": 5,
    "max_tokens": 1024,
    "tools": [],
    "persona": {
        "name": "Multi-Turn Agent",
        "background": "A persona used by the RFC 0020 PR 3 multi-turn test.",
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
    """Mock LLM client whose every reply parses to a single DO_NOTHING.

    Multi-turn aggregation does not depend on action shape \u2014 only on
    interaction-tracker state and the persisted episode column values.
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


# \u2500\u2500\u2500 Multi-turn aggregation \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


@pytest.mark.asyncio
class TestMultiTurnAggregation:
    """RFC 0020 PR 3 \u00a7\"Multi-Turn for Human-Chat + DM\"."""

    async def test_ten_turn_session_collapses_into_one_interaction(self):
        """Ten ``MESSAGE_RECEIVED`` turns from the same peer aggregate."""
        agent = await _make_agent()
        peer = "iron-fox"

        for i in range(10):
            await agent.on_event(AgentEvent(
                event_type=EventType.MESSAGE_RECEIVED,
                payload={"content": f"turn {i}"},
                sender_id=peer,
            ))

        # No episode persisted yet \u2014 the interaction is still open.
        assert await _all_episodes(agent) == []

        # One open scope, keyed symmetrically on (agent, peer).
        expected_scope = scope_for_dm(agent.agent_id, peer)
        open_scopes = agent._interaction_tracker.open_scopes()
        assert open_scopes == [expected_scope]

        interaction = agent._interaction_tracker.get(expected_scope)
        assert interaction is not None
        assert interaction.turn_count == 10
        assert interaction.is_open

        # Session end \u2014 explicit ``chat_end`` metadata flag (RFC 0016
        # surface lands in a follow-up; the runtime accepts the marker
        # today so PR 5 / channel hooks can emit it).
        await agent.on_event(AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "thanks, bye"},
            sender_id=peer,
            metadata={"chat_end": True},
        ))
        # PR #229 review Must-Fix #1: the close path is now two-phase
        # (sync INSERT [summary pending] → background LLM → UPDATE).
        # Drain the background task so the assertions below see the
        # post-LLM ``summary`` column rather than racing the sentinel.
        await agent.drain_pending_summaries()

        episodes = await _all_episodes(agent)
        assert len(episodes) == 1
        ep = episodes[0]
        assert ep["interaction_id"]
        assert ep["turn_count"] == 11
        assert ep["scope"] == expected_scope
        assert ep["closed_at"] is not None
        assert ep["closed_at"] >= ep["started_at"]
        # RFC 0020 PR 4 swapped the deterministic placeholder summary
        # (which carried ``REASON_STRUCTURAL``) for an LLM-generated
        # summary.  The mock LLM returns the DO_NOTHING JSON blob for
        # every call site; the close_reason now lives in ``context_json``
        # (asserted in ``test_summarize_on_close.py``).  Here we only
        # assert that *some* non-empty, non-fallback summary was written.
        from agents.memory.interactions import SUMMARY_UNAVAILABLE_TEXT
        assert ep["summary"]
        assert ep["summary"] != SUMMARY_UNAVAILABLE_TEXT

        # Tracker is empty \u2014 the closed scope was popped per RFC 0020
        # \u00a7C "do not reopen".
        assert agent._interaction_tracker.open_scopes() == []

    async def test_idle_gap_closes_interaction_and_next_turn_opens_new_one(self):
        """A clock-advance past idle_timeout closes the open interaction.

        The runtime calls ``InteractionTracker.idle_check`` at the top of
        every event, so the next turn arriving after the timeout
        triggers the close before its own ``add_turn``.  The fresh turn
        then opens a new interaction with a different ``interaction_id``.
        """
        agent = await _make_agent()
        peer = "iron-fox"
        scope = scope_for_dm(agent.agent_id, peer)

        # First turn opens the interaction.
        await agent.on_event(AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "hello"},
            sender_id=peer,
        ))
        first = agent._interaction_tracker.get(scope)
        assert first is not None
        first_id = first.interaction_id

        # Advance the tracker's clock past the idle window and run
        # idle_check explicitly \u2014 the production hot path runs this on
        # every event, but exercising it directly keeps the test free of
        # ``time.sleep`` and decouples the assertion from event ordering.
        future = first.last_turn_at + _TEST_IDLE_TIMEOUT_SEC + 1.0
        closed_list = agent._interaction_tracker.idle_check(now=future)
        assert len(closed_list) == 1
        assert closed_list[0].close_reason == REASON_IDLE_GAP

        # Persist the idle-closed interaction the way the runtime would
        # on the next event \u2014 then verify the persisted row carries the
        # idle-gap reason.
        await agent._persist_closed_interaction(closed_list[0])  # type: ignore[attr-defined]
        # PR #229 review Must-Fix #1: drain the two-phase background
        # task before reading the ``summary`` column.
        await agent.drain_pending_summaries()
        episodes = await _all_episodes(agent)
        assert len(episodes) == 1
        assert episodes[0]["interaction_id"] == first_id
        assert episodes[0]["turn_count"] == 1
        assert REASON_IDLE_GAP in episodes[0]["summary"]

        # Tracker is empty after idle close.
        assert agent._interaction_tracker.open_scopes() == []

        # The next turn opens a fresh interaction.
        await agent.on_event(AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "are you still there?"},
            sender_id=peer,
        ))
        second = agent._interaction_tracker.get(scope)
        assert second is not None
        assert second.interaction_id != first_id
        assert second.turn_count == 1

    async def test_dm_scope_is_symmetric_in_local_and_peer(self):
        """A\u2192B and B\u2192A in a DM accumulate under the same scope key.

        The runtime stamps ``scope_for_dm(agent_id, sender_id)`` for an
        inbound turn; an outbound turn that the agent's own action loop
        would later record (PR 4 \u2014 currently the runtime sees only the
        inbound side, but the scope key must already be symmetric so PR
        4's outbound recording does not split the interaction).  This
        test enforces symmetry directly on the helper rather than\n        relying on outbound-recording wiring that has not landed yet.
        """
        from agents.memory.interactions import scope_for_dm as _s

        a, b = "ember-owl", "iron-fox"
        assert _s(a, b) == _s(b, a)

        # And the tracker keys both directions to the same interaction.
        agent = await _make_agent()
        # Inbound A receives from B.
        await agent.on_event(AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "ping"},
            sender_id=b,
        ))
        # If a hypothetical inbound from the agent itself fired (e.g. an
        # echo), the scope key would still be the same.
        scope_inbound = scope_for_dm(agent.agent_id, b)
        scope_reversed = scope_for_dm(b, agent.agent_id)
        assert scope_inbound == scope_reversed
        assert agent._interaction_tracker.open_scopes() == [scope_inbound]


# \u2500\u2500\u2500 Single-turn parity sanity (no regression on PR 2) \u2500\u2500\u2500\u2500\u2500\u2500


@pytest.mark.asyncio
async def test_pr2_single_turn_parity_unchanged():
    """Smoke check: a TICK still produces exactly one closed episode.

    The full PR 2 parity matrix lives in
    ``test_interaction_single_turn_parity.py``.  This sentinel guards
    against the PR 3 multi-turn wiring accidentally re-routing the
    single-turn fast path through the open-interaction code.
    """
    agent = await _make_agent()
    agent._state.recent_context.append("prior turn context")
    await agent.on_tick()

    episodes = await _all_episodes(agent)
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep["scope"] == "tick"
    assert ep["turn_count"] == 1
    assert ep["closed_at"] is not None
    # No multi-turn scope leaked into the tracker.
    assert agent._interaction_tracker.open_scopes() == []



# ─── Session-end metadata truthiness (PR-216 review High #3) ─────


@pytest.mark.asyncio
class TestSessionEndMetadataTruthiness:
    """Pin the strict-truthy contract for ``chat_end`` / ``session_end``.

    A bare ``bool(meta.get(k))`` accepted any non-empty string — so a
    channel adapter that stringifies booleans (``"false"``, ``"0"``,
    ``"no"``) would have closed every multi-turn interaction
    unexpectedly.  This suite locks the allowlist behaviour from
    PR-216 review High #3.
    """

    @pytest.mark.parametrize("flag_value", [
        True,
        "true",
        "True",
        "TRUE",
        "  yes  ",
        "1",
        "on",
        1,
        2.5,
    ])
    async def test_truthy_values_close_interaction(self, flag_value):
        agent = await _make_agent()
        peer = "iron-fox"
        scope = scope_for_dm(agent.agent_id, peer)

        await agent.on_event(AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "hi"},
            sender_id=peer,
        ))
        assert agent._interaction_tracker.open_scopes() == [scope]

        await agent.on_event(AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "bye"},
            sender_id=peer,
            metadata={"chat_end": flag_value},
        ))
        episodes = await _all_episodes(agent)
        assert len(episodes) == 1
        assert episodes[0]["turn_count"] == 2
        assert agent._interaction_tracker.open_scopes() == []

    @pytest.mark.parametrize("flag_value", [
        False,
        "false",
        "False",
        "0",
        "no",
        "",
        0,
        0.0,
        None,
    ])
    async def test_falsy_values_keep_interaction_open(self, flag_value):
        agent = await _make_agent()
        peer = "iron-fox"
        scope = scope_for_dm(agent.agent_id, peer)

        await agent.on_event(AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "hi"},
            sender_id=peer,
        ))
        await agent.on_event(AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "still here"},
            sender_id=peer,
            metadata={"chat_end": flag_value},
        ))
        assert await _all_episodes(agent) == []
        interaction = agent._interaction_tracker.get(scope)
        assert interaction is not None
        assert interaction.turn_count == 2

    async def test_session_end_alias_also_honoured(self):
        """Both ``chat_end`` and ``session_end`` keys must trigger close."""
        agent = await _make_agent()
        peer = "iron-fox"

        await agent.on_event(AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "hi"},
            sender_id=peer,
        ))
        await agent.on_event(AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "bye"},
            sender_id=peer,
            metadata={"session_end": True},
        ))
        episodes = await _all_episodes(agent)
        assert len(episodes) == 1
        assert episodes[0]["turn_count"] == 2


# ─── Per-turn payload data minimisation (PR-216 review High #1) ──


@pytest.mark.asyncio
async def test_closed_interaction_context_does_not_embed_message_body():
    """RFC 0020 §D — per-turn message text is not stored in episodes.

    The runtime stashes only the structural envelope per turn
    (``event_type`` / ``sender`` / ``channel_id`` / ``timestamp`` /
    ``summary``).  Embedding ``event.payload`` (which carries the
    inbound message body) on the turn would (a) violate §D and (b)
    grow ``context_json`` linearly with conversation length.  This
    test reads the persisted ``context_json`` after a structural close
    and asserts no message body leaks through.
    """
    agent = await _make_agent()
    peer = "iron-fox"
    secret_body = "super-secret-message-body-xyzzy"

    await agent.on_event(AgentEvent(
        event_type=EventType.MESSAGE_RECEIVED,
        payload={"content": secret_body},
        sender_id=peer,
    ))
    await agent.on_event(AgentEvent(
        event_type=EventType.MESSAGE_RECEIVED,
        payload={"content": "bye"},
        sender_id=peer,
        metadata={"chat_end": True},
    ))
    # PR #229 review Must-Fix #1: drain the two-phase background task
    # so its aiosqlite write completes before the test loop tears
    # down.  Without this, the worker thread can race the loop close
    # and surface a ``Event loop is closed`` warning.
    await agent.drain_pending_summaries()

    db = agent._episodic_memory._ensure_db()
    async with db.execute(
        "SELECT context_json FROM episodes WHERE agent_id = ?",
        (agent.agent_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    assert len(rows) == 1
    context_blob = rows[0][0]
    assert secret_body not in context_blob, (
        "Per-turn message body leaked into context_json; "
        "violates RFC 0020 §D."
    )
