"""
RFC 0020 PR 4 — two-phase write + failure modes + self-DM guard.

Split out of ``test_summarize_on_close.py`` to keep that file under
the 500-line cap enforced by ``scripts/checks/file_size.py --strict``
after PR #229 review fixes added Must-Fix #1 (sentinel-visible-mid-
flight) and Should-Fix #3 / #4 coverage.

Pins:

* :class:`TestTwoPhaseWrite` — PR #229 review Must-Fix #1: the row
  must exist with the ``[summary pending]`` sentinel between Phase 1
  (synchronous INSERT) and Phase 2 (background ``UPDATE``) so a
  process crash mid-LLM leaves the janitor a real row to sweep.
* :class:`TestSummarisationFailureModes` — PR #229 review Should-Fix
  #3: timeout and empty-text branches of
  :func:`summarize_closed_interaction` fall back to
  :data:`SUMMARY_UNAVAILABLE_TEXT`.
* :func:`test_extract_peer_self_dm_returns_none` — PR #229 review
  Should-Fix #4: a self-DM scope must not return the agent as its
  own peer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.memory.interactions import (
    SUMMARY_PENDING_TEXT,
    SUMMARY_UNAVAILABLE_TEXT,
)
from agents.persona_runtime.summarize_close import SUMMARIZATION_MAX_OUTPUT_TOKENS
from agents.persona_types import AgentEvent, EventType
from agents.principal_id import principal_scope
from agents.tools.registry import clear_registry

from ._summarize_close_helpers import (
    LLM_SUMMARY_TEXT,
    drain,
    episode_summary,
    make_agent,
    make_gated_summary_client,
    make_summary_client,
    send_n_turns,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


@pytest.mark.asyncio
class TestTwoPhaseWrite:
    """The close path commits ``[summary pending]`` *before* the LLM call.

    Pins PR #229 review Must-Fix #1: the row must exist with the
    pending sentinel between Phase 1 (synchronous INSERT) and Phase 2
    (background ``UPDATE``) so that a process crash mid-LLM leaves
    the janitor a real row to sweep.  Without this guarantee the
    sentinel + janitor are dead code.
    """

    async def test_pending_sentinel_visible_before_drain(self):
        """Between ``chat_end`` and ``drain``, the row carries the sentinel.

        PR 6 review #30: uses an :class:`asyncio.Event` set from the
        mock provider's first await rather than ``asyncio.sleep(0)`` so
        the test deterministically observes Phase-2 parked on the gate.
        """
        gated = make_gated_summary_client()
        agent = await make_agent(client=gated.client)
        peer = "iron-fox"
        await send_n_turns(agent, peer, 3)
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "bye"}, sender_id=peer,
            metadata={"chat_end": True},
        ))

        # Wait until the summariser actually entered (gate parked).
        await gated.started.wait()

        summary_before = await episode_summary(agent)
        assert summary_before == SUMMARY_PENDING_TEXT, (
            "Phase 1 must commit the pending sentinel before the LLM call"
        )

        gated.gate.set()
        await drain(agent)

        summary_after = await episode_summary(agent)
        assert summary_after == LLM_SUMMARY_TEXT


@pytest.mark.asyncio
class TestPhase2JanitorRace:
    """PR 6 review #20 + #26 — janitor's decision is final.

    If the janitor sweeps a ``[summary pending]`` row mid-flight (writing
    :data:`SUMMARY_UNAVAILABLE_TEXT`), a late-successful Phase-2
    completion must NOT overwrite it: the row stays at
    :data:`SUMMARY_UNAVAILABLE_TEXT` and the per-interaction side
    effects (relationship bump, auto-reflect tick) are skipped so the
    failure counter cannot double-increment.
    """

    async def test_janitor_wins_against_late_phase2(self):
        gated = make_gated_summary_client()
        agent = await make_agent(client=gated.client)
        peer = "iron-fox"
        await send_n_turns(agent, peer, 3)
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "bye"}, sender_id=peer,
            metadata={"chat_end": True},
        ))

        await gated.started.wait()
        # Janitor wins — sweeps the pending row before Phase 2 returns.
        upgraded = await agent.cleanup_closing_interactions(grace_sec=0.0)
        assert upgraded == 1
        assert await episode_summary(agent) == SUMMARY_UNAVAILABLE_TEXT

        # Auto-reflect counter snapshot before the late Phase-2 returns;
        # the janitor's verdict must keep this stable across the drain.
        before = await agent._episodic_memory.get_interaction_count()

        gated.gate.set()
        await drain(agent)

        # Row stayed on the janitor's verdict (the LLM text MUST NOT
        # overwrite ``SUMMARY_UNAVAILABLE_TEXT``).
        assert await episode_summary(agent) == SUMMARY_UNAVAILABLE_TEXT
        # Auto-reflect did not double-tick on the no-op UPDATE branch.
        after = await agent._episodic_memory.get_interaction_count()
        assert after == before
        # PR-266 review N2: pin the *second* gated side effect — the
        # relationship row.  The race-loss path must skip
        # ``record_closed_interaction`` (which is what would otherwise
        # bump ``relationships.interaction_count`` and append an
        # ``interactions`` row).  Both this and the auto-reflect tick
        # are gated by the same ``if not updated: return`` in
        # ``finalize_closed_interaction``; asserting them separately
        # locks each pin against a future refactor that splits the
        # gate.  ``get_relationship_summary`` returns
        # ``interaction_count=0`` for a missing row, which is the state
        # we expect because the close-path is the only writer for
        # DM-scoped interactions in this test.
        rel_after = await agent._relationship_memory.get_relationship_summary(peer)
        assert rel_after.interaction_count == 0


@pytest.mark.asyncio
class TestCloseMemoryDrainsImplicitly:
    """PR 6 review #27 — :meth:`close_memory` drains pending summaries.

    Pins the contract that callers do not need to call
    ``drain_pending_summaries`` explicitly before ``close_memory``;
    if they did, a clean shutdown would leave the row on
    :data:`SUMMARY_PENDING_TEXT` because the background Phase-2 task
    would be cancelled by event-loop teardown before its UPDATE.
    """

    async def test_close_memory_finalises_summary_without_explicit_drain(self):
        # The agent factory uses ``:memory:`` (per-connection SQLite
        # state).  Snapshot the row *just before* close_memory so we
        # can compare against the post-drain value through the same
        # handle — close_memory closes the connection at the end, but
        # the drain runs *before* that close (per the in-method order
        # in ``_StatePersistenceMixin.close_memory``).
        agent = await make_agent()
        peer = "iron-fox"
        await send_n_turns(agent, peer, 3)
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "bye"}, sender_id=peer,
            metadata={"chat_end": True},
        ))

        # Patch close_memory's drain step to capture the row state
        # at the exact moment after the drain but before tier close.
        captured: list[str] = []
        original_drain = agent.drain_pending_summaries

        async def _capturing_drain():
            await original_drain()
            db = agent._episodic_memory._ensure_db()
            async with db.execute(
                "SELECT summary FROM episodes WHERE agent_id = ? "
                "ORDER BY created_at", (agent.agent_id,),
            ) as cursor:
                rows = await cursor.fetchall()
            captured.extend(row[0] for row in rows)

        agent.drain_pending_summaries = _capturing_drain  # type: ignore[method-assign]
        await agent.close_memory()

        assert len(captured) == 1
        assert captured[0] == LLM_SUMMARY_TEXT, (
            "close_memory must drain Phase 2 before closing the DB so the "
            "LLM-generated summary lands rather than the [summary pending] "
            "sentinel"
        )


@pytest.mark.asyncio
class TestSummarisationFailureModes:
    """The non-generic-exception branches must also fall back cleanly.

    The original PR only exercised the generic ``RuntimeError`` branch
    of :func:`summarize_closed_interaction`.  These tests pin the
    ``TimeoutError`` and empty-text branches so a future refactor of
    the LLM call site cannot silently drop either.
    """

    async def test_timeout_falls_back_to_unavailable(self, monkeypatch):
        """A summariser hang past ``SUMMARIZATION_TIMEOUT_SEC`` → fallback."""
        # Shrink the timeout to keep the test fast.  Patching the
        # module-level constant is safe because the symbol is read
        # at call time, not captured at import.
        from agents.persona_runtime import summarize_close as _sc
        monkeypatch.setattr(_sc, "SUMMARIZATION_TIMEOUT_SEC", 0.05)

        async def _hang(*args, **kwargs):
            import asyncio as _a
            await _a.sleep(10.0)
            raise AssertionError("should have timed out")

        mock_provider = AsyncMock()

        async def _route(*, model, messages, system, tools, max_tokens, temperature):
            if max_tokens == SUMMARIZATION_MAX_OUTPUT_TOKENS:
                return await _hang()
            return LLMResponse(
                text='```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
                stop_reason=StopReason.END_TURN,
                usage=Usage(10, 5),
            )

        mock_provider.create_message = AsyncMock(side_effect=_route)
        mock_provider.format_tool_definitions = MagicMock(return_value=[])
        mock_provider.append_tool_round = MagicMock(
            side_effect=lambda msgs, resp, results: msgs,
        )

        agent = await make_agent(client=LLMClient(mock_provider))
        peer = "iron-fox"
        await send_n_turns(agent, peer, 3)
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "bye"},
            sender_id=peer,
            metadata={"chat_end": True},
        ))
        await drain(agent)

        summary = await episode_summary(agent)
        assert summary == SUMMARY_UNAVAILABLE_TEXT

    async def test_empty_response_falls_back_to_unavailable(self):
        """An LLM reply with empty / whitespace-only text → fallback."""
        agent = await make_agent(client=make_summary_client(text="   "))
        peer = "iron-fox"
        await send_n_turns(agent, peer, 3)
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "bye"},
            sender_id=peer,
            metadata={"chat_end": True},
        ))
        await drain(agent)

        summary = await episode_summary(agent)
        assert summary == SUMMARY_UNAVAILABLE_TEXT


def test_extract_peer_self_dm_returns_none():
    """``dm:<id>:<id>`` (self-DM) must not return the agent as its own peer.

    ``scope_for_dm`` sorts but does not de-duplicate, so a future
    caller passing ``self.agent_id`` as ``other_id`` (intentional
    self-talk or a routing bug) would otherwise produce a scope that
    extracts ``(agent_id, ...)`` and let ``record_interaction`` write
    a self-relationship row.  The guard keeps the relationship-memory
    invariant ``other_id != agent_id`` defensive.
    """
    from agents.memory.interactions import Interaction, Turn
    from agents.persona_runtime.record_close import (
        extract_peer_from_interaction,
    )

    interaction = Interaction(
        interaction_id="abc",
        scope="dm:agent:agent",
        started_at=0.0,
        closed_at=1.0,
        close_reason="structural",
        turns=[Turn(at=0.0, payload={})],
    )
    peer, peer_type = extract_peer_from_interaction("agent", interaction)
    assert peer is None
    assert peer_type == "agent"


@pytest.mark.asyncio
class TestConversationLevelEffectsFirePerCloseEvent:
    """PR #846 review — the finalize's two conversation-level effects
    (the RFC 0020 §H auto-reflect tick, the DM relationship bump) fire
    once per CLOSE EVENT: a room-close fan of N records designates one
    ``conversation_lead``, so neither effect inflates by room size."""

    async def test_room_close_ticks_reflect_counter_once(self):
        agent = await make_agent()
        for speaker in ("alice", "bob", "cara"):
            await agent._store_event_episode(AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": f"hi from {speaker}",
                         "channel_type": "group"},
                channel_id="group:planning",
                sender_id=speaker,
            ), [])

        ticks = 0
        real_increment = agent._episodic_memory.increment_interaction_count

        async def counting_increment():
            nonlocal ticks
            ticks += 1
            await real_increment()

        agent._episodic_memory.increment_interaction_count = counting_increment
        end = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "wrapping up", "channel_type": "group"},
            channel_id="group:planning",
            sender_id="alice",
            metadata={"chat_end": True},
        )
        await agent._store_event_episode(end, [])
        await drain(agent)

        assert ticks == 1, (
            "one room close is one conversation — the reflect counter "
            "must not tick once per (principal, speaker) record"
        )

    async def test_principal_split_dm_close_bumps_relationship_once(self):
        agent = await make_agent()
        peer = "human-pal"
        # The same DM peer under two tenants → two records, one scope
        # (the ISSUE-0123 principal axis).
        for principal in ("alice-person", "bob-person"):
            with principal_scope(principal):
                await agent._store_event_episode(AgentEvent(
                    event_type=EventType.CHANNEL_MESSAGE,
                    payload={"content": f"hello as {principal}"},
                    sender_id=peer,
                ), [])

        bumps: list[dict] = []
        real_record = agent._memory_ns.relationship.record_interaction

        async def counting_record(**kwargs):
            bumps.append(kwargs)
            return await real_record(**kwargs)

        agent._memory_ns.relationship.record_interaction = counting_record
        end = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "bye"},
            sender_id=peer,
            metadata={"chat_end": True},
        )
        await agent._store_event_episode(end, [])
        await drain(agent)

        assert len(bumps) == 1, (
            "one DM conversation ending must bump the peer relationship "
            "once, however many principals split its records"
        )
        assert bumps[0]["other_id"] == peer
