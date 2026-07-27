"""L1 cross-room episodic recall — SHADOW mode (RFC 0049 Phase 1 PR 3).

Pins the RFC 0049 L1 amendment's shadow implementation
(:mod:`agents.persona_runtime.episodes_shadow`), the L1 sibling of
``test_facts_shadow.py``:

* the cross-room delta — an episode from room A is a shadow candidate
  on a room-B turn, in boosted-rank order with each candidate's widened
  ``rank`` position, while the live recall stays room-walled;
* the RFC 0037 §D gate on every candidate — a ``restricted``-stamped
  episode is *withheld* (counted, never listed) when the turn acts
  below its level, split by cause (``withheld`` vs ``unknown_label``);
* the absolute walls — ``epoch`` and ``principal`` rows never appear in
  a shadow trace;
* the no-prompt-leak property — shadow candidates never reach the
  working memory, the §G manifest, or any reinforcement (the widened
  read never bumps ``access_count``); and
* the cost guards — tick-shaped events and ``mode="off"`` issue zero
  DB round-trips.
"""

from __future__ import annotations

import logging

import pytest

from agents.epoch_id import epoch_scope
from agents.memory.episodic import EpisodicMemory
from agents.persona_runtime import episodes_shadow
from agents.persona_runtime.episodes_shadow import (
    DEFAULT_EPISODIC_CROSS_ROOM,
    SHADOW_LOGGER_NAME,
    SHADOW_TRACE_ATTR,
    emit_episodes_shadow,
    resolve_episodic_cross_room,
)
from agents.persona_runtime.facts_shadow import (
    CROSS_ROOM_OFF,
    CROSS_ROOM_SHADOW,
)
from agents.persona_types import AgentEvent, EventType
from agents.principal_id import principal_scope

_asyncio = pytest.mark.asyncio

_QUERY = "atlas deployment retro"


# ─── Fixtures / helpers ─────────────────────────────────────


@pytest.fixture
async def episodic():
    mem = EpisodicMemory(agent_id="shadow-test-agent", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


def _channel_event(
    content: str = _QUERY,
    *,
    classification: str | None = "internal",
) -> AgentEvent:
    metadata = (
        {"channel_classification": classification}
        if classification is not None else {}
    )
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": content},
        channel_id="group:room-b",
        sender_id="bob",
        metadata=metadata,
    )


async def _seed_episode(
    memory: EpisodicMemory,
    summary: str = _QUERY,
    *,
    session_id: str = "room-a",
    importance: float = 0.5,
    **kwargs,
) -> str:
    return await memory.store_episode(
        summary, {"k": "v"}, importance=importance,
        session_id=session_id, **kwargs,
    )


async def _emit(
    memory: EpisodicMemory | None,
    event: AgentEvent,
    *,
    query: str = _QUERY,
    live_episode_ids: set[str] | None = None,
    mode: str = CROSS_ROOM_SHADOW,
) -> None:
    await emit_episodes_shadow(
        memory, event, query=query,
        live_episode_ids=live_episode_ids or set(),
        agent_id="shadow-test-agent", mode=mode,
    )


def _traces(caplog: pytest.LogCaptureFixture) -> list[dict]:
    return [
        getattr(r, SHADOW_TRACE_ATTR)
        for r in caplog.records
        if hasattr(r, SHADOW_TRACE_ATTR)
    ]


@pytest.fixture
def shadow_log(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.INFO, logger=SHADOW_LOGGER_NAME):
        yield caplog


# ─── Config resolution ──────────────────────────────────────


class TestResolveEpisodicCrossRoom:
    def test_absent_defaults_to_shadow(self):
        assert resolve_episodic_cross_room({}) == CROSS_ROOM_SHADOW
        assert DEFAULT_EPISODIC_CROSS_ROOM == CROSS_ROOM_SHADOW

    def test_explicit_off(self):
        cfg = {"memory": {"episodic": {"cross_room": "off"}}}
        assert resolve_episodic_cross_room(cfg) == CROSS_ROOM_OFF

    def test_explicit_shadow(self):
        cfg = {"memory": {"episodic": {"cross_room": "shadow"}}}
        assert resolve_episodic_cross_room(cfg) == CROSS_ROOM_SHADOW

    def test_null_collapses_to_default(self):
        cfg = {"memory": {"episodic": {"cross_room": None}}}
        assert resolve_episodic_cross_room(cfg) == DEFAULT_EPISODIC_CROSS_ROOM

    def test_facts_knob_does_not_leak_across(self):
        """The two knobs are independent blocks — a facts setting must
        not steer the episodic mode."""
        cfg = {"memory": {"facts": {"cross_room": "off"}}}
        assert resolve_episodic_cross_room(cfg) == CROSS_ROOM_SHADOW

    def test_unknown_mode_raises(self):
        """``"live"`` must fail loudly until the PR 4 promotion lands."""
        cfg = {"memory": {"episodic": {"cross_room": "live"}}}
        with pytest.raises(ValueError, match="cross_room"):
            resolve_episodic_cross_room(cfg)


# ─── The shadow pass ────────────────────────────────────────


@_asyncio
class TestEmitEpisodesShadow:
    async def test_cross_room_episode_is_candidate(
        self, episodic: EpisodicMemory, shadow_log,
    ):
        ep_id = await _seed_episode(episodic)
        await _emit(episodic, _channel_event())
        (trace,) = _traces(shadow_log)
        assert trace["tier"] == "episodic"
        assert trace["acting"] == "internal"
        assert [c["episode_id"] for c in trace["candidates"]] == [ep_id]
        candidate = trace["candidates"][0]
        assert candidate["session_id"] == "room-a"
        assert candidate["rank"] == 0
        assert candidate["protection_level"] == "internal"
        assert trace["withheld"] == 0

    async def test_live_rows_are_not_delta(
        self, episodic: EpisodicMemory, shadow_log,
    ):
        """A row the live (room-walled) recall already returned is not a
        cross-room candidate — and an empty delta emits nothing, so
        single-room turns stay log-quiet."""
        ep_id = await _seed_episode(episodic, session_id="legacy")
        await _emit(episodic, _channel_event(), live_episode_ids={ep_id})
        assert _traces(shadow_log) == []

    async def test_candidates_ride_in_boosted_rank_order(
        self, episodic: EpisodicMemory, shadow_log,
    ):
        """The trace preserves the widened read's ranking (the PR 4
        displacement signal): a higher-importance cross-room row leads,
        and each candidate carries its widened ``rank`` position."""
        low = await _seed_episode(episodic, importance=0.2)
        high = await _seed_episode(episodic, importance=0.9)
        await _emit(episodic, _channel_event())
        (trace,) = _traces(shadow_log)
        assert [c["episode_id"] for c in trace["candidates"]] == [high, low]
        assert [c["rank"] for c in trace["candidates"]] == [0, 1]

    async def test_rank_positions_include_live_rows(
        self, episodic: EpisodicMemory, shadow_log,
    ):
        """``rank`` is the position in the WIDENED result, live rows
        included — "this row would have been the prompt's #N line", not
        its index inside the delta."""
        live = await _seed_episode(
            episodic, session_id="legacy", importance=0.9,
        )
        cross = await _seed_episode(episodic, importance=0.2)
        await _emit(episodic, _channel_event(), live_episode_ids={live})
        (trace,) = _traces(shadow_log)
        assert [c["episode_id"] for c in trace["candidates"]] == [cross]
        assert trace["candidates"][0]["rank"] == 1

    async def test_restricted_episode_withheld_acting_internal(
        self, episodic: EpisodicMemory, shadow_log,
    ):
        await _seed_episode(episodic, protection_level="restricted")
        await _emit(episodic, _channel_event(classification="internal"))
        (trace,) = _traces(shadow_log)
        assert trace["candidates"] == []
        assert trace["withheld"] == 1
        assert trace["unknown_label"] == 0

    async def test_restricted_episode_admitted_acting_restricted(
        self, episodic: EpisodicMemory, shadow_log,
    ):
        ep_id = await _seed_episode(
            episodic, protection_level="restricted",
        )
        await _emit(
            episodic, _channel_event(classification="restricted"),
        )
        (trace,) = _traces(shadow_log)
        assert [c["episode_id"] for c in trace["candidates"]] == [ep_id]
        assert trace["candidates"][0]["protection_level"] == "restricted"

    async def test_unstamped_turn_floors_public_and_withholds(
        self, episodic: EpisodicMemory, shadow_log,
    ):
        """Rule (b): a channel turn with no wire stamp acts ``public`` —
        an ``internal``-default episode is withheld in shadow exactly as
        it would be live."""
        await _seed_episode(episodic)
        await _emit(episodic, _channel_event(classification=None))
        (trace,) = _traces(shadow_log)
        assert trace["candidates"] == []
        assert trace["withheld"] == 1
        assert trace["acting"] is None

    async def test_unknown_label_counted_separately(
        self, episodic: EpisodicMemory, shadow_log,
    ):
        """Rule (c): a row whose stored label fails to parse is withheld
        under ``unknown_label`` — the PR 4 measurement reads the same
        two-field split off L1 traces as off L2's."""
        await _seed_episode(episodic, protection_level="mystery")
        await _emit(episodic, _channel_event())
        (trace,) = _traces(shadow_log)
        assert trace["candidates"] == []
        assert trace["withheld"] == 0
        assert trace["unknown_label"] == 1

    async def test_epoch_wall_absolute(
        self, episodic: EpisodicMemory, shadow_log,
    ):
        with epoch_scope("other-epoch"):
            await _seed_episode(episodic)
        await _emit(episodic, _channel_event())
        assert _traces(shadow_log) == []

    async def test_principal_wall_absolute(
        self, episodic: EpisodicMemory, shadow_log,
    ):
        with principal_scope("other-tenant"):
            await _seed_episode(episodic)
        await _emit(episodic, _channel_event())
        assert _traces(shadow_log) == []

    async def test_widened_read_never_reinforces(
        self, episodic: EpisodicMemory, shadow_log,
    ):
        """The shadow read is side-effect-free: no ``access_count`` bump
        — a reinforcing shadow would perturb live ranking (the composite
        score reads ``access_count``) and shift the landed RFC 0044
        goldens off their cassettes."""
        ep_id = await _seed_episode(episodic)
        await _emit(episodic, _channel_event())
        row = await episodic.get_episode(ep_id)
        assert row is not None
        assert row.access_count == 0
        assert row.last_accessed_at is None

    async def test_tick_shaped_event_issues_no_recall(
        self, episodic: EpisodicMemory, shadow_log, monkeypatch,
    ):
        """The cheap-idle guard: a tick-shaped (non-channel-anchored)
        event costs zero DB round-trips — it floors to rule-(b)
        ``public`` and is not the measurement's target."""
        calls: list[str] = []

        async def _spy(*args, **kwargs):
            calls.append("recall")
            return []

        monkeypatch.setattr(episodes_shadow, "recall_room_ranked", _spy)
        event = AgentEvent(
            event_type=EventType.TICK, payload={}, sender_id=None,
        )
        await _emit(episodic, event)
        assert calls == []
        assert _traces(shadow_log) == []

    async def test_mode_off_issues_no_recall(
        self, episodic: EpisodicMemory, shadow_log, monkeypatch,
    ):
        calls: list[str] = []

        async def _spy(*args, **kwargs):
            calls.append("recall")
            return []

        monkeypatch.setattr(episodes_shadow, "recall_room_ranked", _spy)
        await _seed_episode(episodic)  # would be a candidate if on
        await _emit(episodic, _channel_event(), mode=CROSS_ROOM_OFF)
        assert calls == []
        assert _traces(shadow_log) == []

    async def test_missing_store_is_noop(self, shadow_log):
        await _emit(None, _channel_event())
        assert _traces(shadow_log) == []

    async def test_backend_failure_never_raises(
        self, episodic: EpisodicMemory, shadow_log, monkeypatch,
    ):
        async def _boom(*args, **kwargs):
            raise RuntimeError("db exploded")

        monkeypatch.setattr(episodes_shadow, "recall_room_ranked", _boom)
        await _emit(episodic, _channel_event())  # must not raise
        assert _traces(shadow_log) == []

    async def test_summary_text_never_logged(
        self, episodic: EpisodicMemory, shadow_log,
    ):
        """Log-egress bound: the trace names ids / levels / provenance /
        rank but never the episode summary — the process log must not
        become the leak the §D gate closes at the prompt."""
        secret = "atlas deployment retro secret-payload-marker"
        await _seed_episode(episodic, secret)
        await _emit(episodic, _channel_event())
        (trace,) = _traces(shadow_log)
        assert trace["candidates"], "expected the seeded candidate"
        assert "secret-payload-marker" not in repr(trace)
        assert all(
            "secret-payload-marker" not in r.getMessage()
            for r in shadow_log.records
        )


# ─── End-to-end: shadow never enters the prompt ─────────────


def _build_mixin(episodic: EpisodicMemory):
    from unittest.mock import AsyncMock

    from agents.clock import WallClock
    from agents.memory.working import WorkingMemory
    from agents.persona_runtime.memory_context import _MemoryContextMixin

    class _Host(_MemoryContextMixin):
        def _format_event(self, event):  # type: ignore[override]
            payload = getattr(event, "payload", {}) or {}
            return str(payload.get("content", ""))

    mixin = _Host()
    mixin.agent_id = "shadow-test-agent"
    mixin._working_memory = WorkingMemory(max_tokens=8192)
    mixin._episodic_memory = episodic
    mixin._relationship_memory = AsyncMock()
    mixin._relationship_memory.get_relationship_summary.return_value = None
    mixin._fact_store = None
    mixin._clock = WallClock()
    mixin._timezone = "UTC"
    return mixin


@_asyncio
class TestShadowNeverEntersPrompt:
    async def test_cross_room_episode_shadowed_but_not_injected(
        self, episodic: EpisodicMemory, shadow_log,
    ):
        """The full ``_inject_memory_context`` pass: the cross-room row
        is traced, while the prompt, the §G manifest, and reinforcement
        (``access_count``) see only the live (room-walled) row."""
        cross_id = await _seed_episode(
            episodic, "atlas deployment retro cross-room-marker",
        )
        live_id = await _seed_episode(
            episodic, "atlas deployment retro went-well-live",
            session_id="legacy",
        )
        mixin = _build_mixin(episodic)
        result = await mixin._inject_memory_context(_channel_event())

        rendered = "\n".join(
            s.content for s in mixin._working_memory._sections
        )
        assert "went-well-live" in rendered
        assert "cross-room-marker" not in rendered
        manifest_ids = {e.entry_id for e in result.manifest}
        assert live_id in manifest_ids
        assert cross_id not in manifest_ids
        # Reinforcement asymmetry: the live tiers bumped their row (the
        # channel-history and episodic recalls each count an access),
        # while the shadow read left the cross-room row untouched.
        live_row = await episodic.get_episode(live_id)
        cross_row = await episodic.get_episode(cross_id)
        assert live_row is not None and live_row.access_count >= 1
        assert cross_row is not None and cross_row.access_count == 0
        # And the shadow trace recorded the cross-room row end-to-end.
        (trace,) = _traces(shadow_log)
        assert trace["tier"] == "episodic"
        assert [c["episode_id"] for c in trace["candidates"]] == [cross_id]

    async def test_mode_off_on_mixin_emits_nothing(
        self, episodic: EpisodicMemory, shadow_log,
    ):
        await _seed_episode(episodic)
        mixin = _build_mixin(episodic)
        mixin._episodic_cross_room = CROSS_ROOM_OFF
        await mixin._inject_memory_context(_channel_event())
        assert _traces(shadow_log) == []

    async def test_construction_knob_reaches_agent(self):
        """``memory.episodic.cross_room`` resolves at agent construction
        (the ``_LLMPersonaAgent.__init__`` wiring)."""
        import copy

        from agents.persona import create_persona_agent

        from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

        default_agent = create_persona_agent(
            agent_id="ember-owl", config=copy.deepcopy(_PERSONA_CONFIG),
            llm_client=_make_client(),
        )
        assert default_agent._episodic_cross_room == DEFAULT_EPISODIC_CROSS_ROOM

        config = copy.deepcopy(_PERSONA_CONFIG)
        config.setdefault("memory", {}).setdefault("episodic", {})[
            "cross_room"
        ] = "off"
        off_agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=_make_client(),
        )
        assert off_agent._episodic_cross_room == CROSS_ROOM_OFF
