"""RFC 0021 PR 2 — temporal prompt-shape integration tests.

Pins the PR 2 deliverables called out in
``docs/rfcs/0021-pr-plan.md`` §PR 2:

* The system prompt unconditionally carries the now-anchor block in the
  expected position (between the persona-config sections and the
  user-message-delimiters safety snippet).
* Episodes recalled into the prompt carry a recency prefix derived from
  ``closed_at`` when available, falling back to ``created_at`` for legacy
  rows.  Multi-turn episodes also carry a duration prefix.
* Relationship summaries surface ``last_interaction_at`` as a recency
  tag and a coarse cadence bucket.
* Token cost of the temporal additions stays well under 100 tokens for
  a typical prompt — the budget invariant from the PR plan.

Telemetry counter accuracy (PR #260 review M-1) is in
``test_temporal_metrics.py``, which exercises the ``InMemoryMetricReader``
path without adding metric fixtures to every test in this file.

All tests use a :class:`FrozenClock` so the rendered strings are
byte-stable across CI runs.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from agents.clock import FrozenClock
from agents.memory.working import estimate_tokens
from agents.persona_types import AgentEvent, EventType
from agents.tools.registry import clear_registry

from ._temporal_test_helpers import FROZEN_EPOCH as _FROZEN_EPOCH
from ._temporal_test_helpers import PERSONA_CONFIG as _PERSONA_CONFIG_BASE
from ._temporal_test_helpers import make_agent as _make_agent_base


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


async def _make_agent(*, clock: FrozenClock | None = None, config: dict | None = None):
    return await _make_agent_base(clock=clock, config=config)


# ─── Now-anchor block ──────────────────────────────────────


class TestNowAnchor:
    async def test_now_anchor_renders_in_system_prompt(self) -> None:
        agent = await _make_agent()
        try:
            prompt = agent._build_system_prompt()
            # The frozen-clock instant is 14:32 UTC on a Friday — assert
            # both the ISO-8601 absolute time and the human form land
            # exactly where the composer placed them.
            assert "Current time: 2025-04-25T14:32:00+00:00 (Friday afternoon)." in prompt
            # Position contract: the now-anchor sits between current-state
            # (the last persona-config section) and the user-message-
            # delimiter safety snippet.  Asserting on the *order* of the
            # three substrings catches a future regression that drifts the
            # anchor into the wrong position without breaking the
            # byte-identity golden in test_persona_section_composer.py.
            assert prompt.index("Current state:") \
                < prompt.index("Current time:") \
                < prompt.index("Messages from human users")
        finally:
            await agent.close_memory()

    async def test_now_anchor_uses_persona_timezone(self) -> None:
        cfg = deepcopy(_PERSONA_CONFIG_BASE)
        cfg["persona"]["timezone"] = "America/Los_Angeles"
        agent = await _make_agent(
            clock=FrozenClock(_FROZEN_EPOCH, tz="America/Los_Angeles"),
            config=cfg,
        )
        try:
            prompt = agent._build_system_prompt()
            # Same epoch, different zone — the offset and weekday must
            # follow the configured zone, not UTC.  14:32Z is 07:32-07
            # in PT during DST, still Friday morning.
            assert "Current time: 2025-04-25T07:32:00-07:00 (Friday morning)." in prompt
        finally:
            await agent.close_memory()

    async def test_now_anchor_omitted_timezone_defaults_to_utc(self) -> None:
        cfg = deepcopy(_PERSONA_CONFIG_BASE)
        cfg["persona"].pop("timezone", None)
        agent = await _make_agent(config=cfg)
        try:
            prompt = agent._build_system_prompt()
            assert "+00:00 (Friday afternoon)" in prompt
        finally:
            await agent.close_memory()


# ─── Episode recency rendering ─────────────────────────────


class TestEpisodeRecency:
    async def _store_episode(
        self,
        agent,
        *,
        summary: str,
        importance: float = 0.9,
        created_at_offset_sec: float = 0.0,
        closed_at_offset_sec: float | None = None,
        started_at_offset_sec: float | None = None,
        turn_count: int | None = None,
    ) -> None:
        # Inject the desired ``created_at`` / ``closed_at`` directly via
        # SQL — ``store_episode`` always stamps ``created_at = time.time()``,
        # which would defeat the FrozenClock-based assertions.
        # ``public`` protection — the TICK turns these tests drive take
        # the RFC 0037 §D acting floor (rule (b)), which would withhold
        # the ``internal`` default; this file's subject is recency
        # rendering, not the gate.
        ep_id = await agent._episodic_memory.store_episode(
            summary=summary,
            context={},
            importance=importance,
            protection_level="public",
            interaction_id=("int-" + summary[:8]) if turn_count else None,
            started_at=(
                (_FROZEN_EPOCH + started_at_offset_sec)
                if started_at_offset_sec is not None
                else None
            ),
            closed_at=(
                (_FROZEN_EPOCH + closed_at_offset_sec)
                if closed_at_offset_sec is not None
                else None
            ),
            turn_count=turn_count,
        )
        if created_at_offset_sec:
            db = agent._episodic_memory._ensure_db()  # noqa: SLF001 — test-only
            await db.execute(
                "UPDATE episodes SET created_at = ? WHERE id = ?",
                (_FROZEN_EPOCH + created_at_offset_sec, ep_id),
            )
            await db.commit()

    async def test_recent_episode_renders_with_minutes_recency(self) -> None:
        agent = await _make_agent()
        try:
            await self._store_episode(
                agent,
                summary="Discussed roadmap with the leads",
                created_at_offset_sec=-180,
            )
            await agent._inject_memory_context(
                AgentEvent(event_type=EventType.TICK), query="roadmap",
            )
            section = agent._working_memory.get_section("episodic_recall")
            assert section is not None
            # Three-minute-old episode renders as "3 min ago".
            assert "[3 min ago]" in section.content
            assert "Discussed roadmap with the leads" in section.content
        finally:
            await agent.close_memory()

    async def test_multi_turn_episode_renders_duration_tag(self) -> None:
        agent = await _make_agent()
        try:
            # Multi-turn episode that started 47 min before close, closed
            # 3 days ago — both prefixes (recency + duration) render.
            three_days = -3 * 86_400
            await self._store_episode(
                agent,
                summary="Negotiated the API contract with Bob",
                created_at_offset_sec=three_days,
                closed_at_offset_sec=three_days,
                started_at_offset_sec=three_days - 47 * 60,
                turn_count=12,
            )
            await agent._inject_memory_context(
                AgentEvent(event_type=EventType.TICK), query="API contract",
            )
            section = agent._working_memory.get_section("episodic_recall")
            assert section is not None
            assert "[3 days ago, over 47 min]" in section.content
        finally:
            await agent.close_memory()

    async def test_legacy_episode_falls_back_to_created_at(self) -> None:
        agent = await _make_agent()
        try:
            await self._store_episode(
                agent,
                summary="Legacy single-turn episode without closed_at",
                created_at_offset_sec=-2 * 86_400,
                # closed_at left unset — recall must fall back to created_at.
            )
            await agent._inject_memory_context(
                AgentEvent(event_type=EventType.TICK), query="Legacy",
            )
            section = agent._working_memory.get_section("episodic_recall")
            assert section is not None
            assert "[2 days ago]" in section.content
        finally:
            await agent.close_memory()


# ─── Relationship recency + cadence ────────────────────────


class TestRelationshipTemporal:
    async def test_last_seen_recency_renders(self) -> None:
        agent = await _make_agent()
        try:
            # Seed three interactions spread across three weeks so the
            # cadence math has something to chew on.  ``record_interaction``
            # stamps ``created_at`` from ``time.time()``, so we patch the
            # values via SQL after the fact.
            for i in range(3):
                await agent._relationship_memory.record_interaction(
                    "alice", "chat", outcome="ok",
                )
            db = agent._relationship_memory._ensure_db()  # noqa: SLF001 — test-only
            # ISSUE-0080 PR 5 follow-up: ``last_interaction_at`` is now
            # derived from ``MAX(created_at)`` over the (session-filtered)
            # ``interactions`` rows, not read from the ``relationships``
            # column.  Rewrite *every* alice interaction to a coherent
            # timestamp — baseline all three to 3 days ago, then push the
            # oldest back to 21 days ago — so ``MAX`` = 3 days ago and
            # ``MIN`` = 21 days ago.  (Pre-fix this test left the middle
            # row at real wall-clock time and leaned on the column.)
            await db.execute(
                "UPDATE interactions SET created_at = ? "
                "WHERE other_participant_id = 'alice'",
                (_FROZEN_EPOCH - 3 * 86_400,),
            )
            await db.execute(
                "UPDATE interactions SET created_at = ? "
                "WHERE other_participant_id = 'alice' "
                "AND rowid = (SELECT MIN(rowid) FROM interactions "
                "WHERE other_participant_id='alice')",
                (_FROZEN_EPOCH - 21 * 86_400,),
            )
            await db.commit()

            event = AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": "hi"},
                sender_id="alice",
                metadata={"sender_participant_type": "agent"},
            )
            await agent._inject_memory_context(event, query="hi")
            section = agent._working_memory.get_section("relationship_context")
            assert section is not None
            assert "Last seen: 3 days ago" in section.content
        finally:
            await agent.close_memory()

    async def test_cadence_bucket_renders_when_history_qualifies(self) -> None:
        agent = await _make_agent()
        try:
            # Six interactions across six days = "frequent".  The
            # ``interaction_count > 5`` threshold in ``format_cadence``
            # means a sixth interaction is exactly the smallest case the
            # bucket activates — so this loop pins the boundary the
            # comment claims, not one above it.
            # (PR #260 review L-3: prior test ran 7 iterations.)
            for _ in range(6):
                await agent._relationship_memory.record_interaction(
                    "bob", "chat", outcome="ok",
                )
            db = agent._relationship_memory._ensure_db()  # noqa: SLF001 — test-only
            # Spread the six interactions across six days ending 1h ago.
            for i in range(6):
                offset = -6 * 86_400 + i * 86_400
                await db.execute(
                    "UPDATE interactions SET created_at = ? "
                    "WHERE rowid IN ("
                    "  SELECT rowid FROM interactions "
                    "  WHERE other_participant_id='bob' "
                    "  ORDER BY rowid LIMIT 1 OFFSET ?"
                    ")",
                    (_FROZEN_EPOCH + offset, i),
                )
            await db.execute(
                "UPDATE relationships SET last_interaction_at = ? "
                "WHERE other_participant_id = 'bob'",
                (_FROZEN_EPOCH - 3600,),
            )
            await db.commit()

            event = AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": "hi"},
                sender_id="bob",
                metadata={"sender_participant_type": "agent"},
            )
            await agent._inject_memory_context(event, query="hi")
            section = agent._working_memory.get_section("relationship_context")
            assert section is not None
            assert "Cadence: frequent" in section.content
        finally:
            await agent.close_memory()


# ─── Token-budget invariant ────────────────────────────────


class TestTokenCost:
    async def test_temporal_additions_under_100_tokens(self) -> None:
        # Construct a baseline prompt without temporal additions by
        # comparing two builds — one with the now-anchor stripped, one
        # with it intact — and assert the delta is < 100 tokens (PR plan
        # invariant).
        agent = await _make_agent()
        try:
            full_prompt = agent._build_system_prompt()
            anchor_line = "Current time: 2025-04-25T14:32:00+00:00 (Friday afternoon)."
            baseline = full_prompt.replace("\n\n" + anchor_line, "")
            full_tokens = estimate_tokens(full_prompt, accurate=True)
            baseline_tokens = estimate_tokens(baseline, accurate=True)
            delta = full_tokens - baseline_tokens
            assert 0 < delta < 100, (
                f"now-anchor token cost {delta} exceeds the 100-token "
                f"PR-plan invariant"
            )
        finally:
            await agent.close_memory()
