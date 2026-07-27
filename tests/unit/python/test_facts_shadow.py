"""L2 cross-room fact recall — SHADOW mode (RFC 0049 Phase 1 PR 2).

Pins the RFC 0031 fact-scope amendment's shadow implementation
(:mod:`agents.persona_runtime.facts_shadow`):

* the cross-room delta — a fact (person *and* topic) taught in room A
  is a shadow candidate on a room-B turn, while the live recall stays
  room-scoped;
* the RFC 0037 §D gate on every candidate — a ``restricted``-stamped
  fact is *withheld* (counted, never listed) when the turn acts below
  its level;
* the absolute walls — ``epoch`` and ``principal`` rows never appear
  in a shadow trace (cross-room is never cross-run / cross-tenant);
* the no-prompt-leak property — shadow candidates never reach the
  working memory, the §G manifest, or the reinforcement write; and
* the cost guards — sender-less events and ``mode="off"`` issue zero
  DB round-trips.
"""

from __future__ import annotations

import logging

import pytest

from agents.epoch_id import epoch_scope
from agents.memory.facts import FactStore
from agents.persona_runtime.facts_shadow import (
    CROSS_ROOM_OFF,
    CROSS_ROOM_SHADOW,
    DEFAULT_FACTS_CROSS_ROOM,
    SHADOW_LOGGER_NAME,
    SHADOW_TRACE_ATTR,
    emit_facts_shadow,
    resolve_facts_cross_room,
)
from agents.persona_types import AgentEvent, EventType
from agents.principal_id import principal_scope

_asyncio = pytest.mark.asyncio


# ─── Fixtures / helpers ─────────────────────────────────────


@pytest.fixture
async def fact_store():
    store = FactStore(agent_id="shadow-test-agent", db_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


def _channel_event(
    content: str = "hello bob",
    *,
    sender: str = "bob",
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
        sender_id=sender,
        metadata=metadata,
    )


async def _seed_fact(
    store: FactStore,
    *,
    subject: str = "bob",
    predicate: str = "works_at",
    object_: str = "atlas-labs",
    session_id: str = "room-a",
    asserted_at: float = 1000.0,
    **kwargs,
) -> str:
    return await store.store(
        subject=subject,
        predicate=predicate,
        object=object_,
        source_interaction_id="int-1",
        asserted_at=asserted_at,
        session_id=session_id,
        **kwargs,
    )


async def _emit(
    store: FactStore | None,
    event: AgentEvent,
    *,
    stimulus: str | None = "hello bob",
    live_fact_ids: set[str] | None = None,
    mode: str = CROSS_ROOM_SHADOW,
) -> None:
    await emit_facts_shadow(
        store, event, stimulus=stimulus,
        live_fact_ids=live_fact_ids or set(),
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


class TestResolveFactsCrossRoom:
    def test_absent_defaults_to_shadow(self):
        assert resolve_facts_cross_room({}) == CROSS_ROOM_SHADOW
        assert DEFAULT_FACTS_CROSS_ROOM == CROSS_ROOM_SHADOW

    def test_explicit_off(self):
        cfg = {"memory": {"facts": {"cross_room": "off"}}}
        assert resolve_facts_cross_room(cfg) == CROSS_ROOM_OFF

    def test_explicit_shadow(self):
        cfg = {"memory": {"facts": {"cross_room": "shadow"}}}
        assert resolve_facts_cross_room(cfg) == CROSS_ROOM_SHADOW

    def test_null_collapses_to_default(self):
        cfg = {"memory": {"facts": {"cross_room": None}}}
        assert resolve_facts_cross_room(cfg) == DEFAULT_FACTS_CROSS_ROOM

    def test_unknown_mode_raises(self):
        """``"live"`` must fail loudly until the PR 4 promotion lands —
        silently degrading a requested live widening to shadow would
        misreport what the deployment is doing."""
        cfg = {"memory": {"facts": {"cross_room": "live"}}}
        with pytest.raises(ValueError, match="cross_room"):
            resolve_facts_cross_room(cfg)


# ─── Widened topic enumeration (FactStore.topic_subjects) ───


@_asyncio
class TestWidenedTopicSubjects:
    async def test_star_enumerates_foreign_session_topic(
        self, fact_store: FactStore,
    ):
        await _seed_fact(
            fact_store, subject="atlas", predicate="topic.has_deadline",
            object_="friday", session_id="room-a",
        )
        assert await fact_store.topic_subjects() == []
        assert await fact_store.topic_subjects(sessions="*") == ["atlas"]


# ─── The shadow pass ────────────────────────────────────────


@_asyncio
class TestEmitFactsShadow:
    async def test_person_fact_from_other_room_is_candidate(
        self, fact_store: FactStore, shadow_log,
    ):
        fact_id = await _seed_fact(fact_store)
        await _emit(fact_store, _channel_event())
        (trace,) = _traces(shadow_log)
        assert [c["fact_id"] for c in trace["candidates"]] == [fact_id]
        candidate = trace["candidates"][0]
        assert candidate["session_id"] == "room-a"
        assert candidate["subject"] == "bob"
        assert trace["withheld"] == 0
        assert trace["acting"] == "internal"

    async def test_topic_fact_from_other_room_is_candidate(
        self, fact_store: FactStore, shadow_log,
    ):
        """The scenario-2 pair: a topic taught in room A seeds — and
        surfaces — on a room-B turn that mentions it, via the widened
        enumeration + widened recall."""
        fact_id = await _seed_fact(
            fact_store, subject="atlas", predicate="topic.has_deadline",
            object_="friday", session_id="room-a",
        )
        await _emit(
            fact_store,
            _channel_event("how is atlas going?"),
            stimulus="how is atlas going?",
        )
        (trace,) = _traces(shadow_log)
        assert [c["fact_id"] for c in trace["candidates"]] == [fact_id]
        assert trace["candidates"][0]["predicate"] == "topic.has_deadline"

    async def test_live_rows_are_not_delta(
        self, fact_store: FactStore, shadow_log,
    ):
        """A row the live (room-scoped) recall already returned is not a
        cross-room candidate — and an empty delta emits nothing, so
        single-room turns stay log-quiet."""
        fact_id = await _seed_fact(fact_store, session_id="legacy")
        await _emit(fact_store, _channel_event(), live_fact_ids={fact_id})
        assert _traces(shadow_log) == []

    async def test_restricted_fact_withheld_acting_internal(
        self, fact_store: FactStore, shadow_log,
    ):
        """§D on the shadow path: the candidate list carries only rows
        the gate admits at the turn's acting level; the withheld count
        records the rest."""
        await _seed_fact(fact_store, protection_level="restricted")
        await _emit(fact_store, _channel_event(classification="internal"))
        (trace,) = _traces(shadow_log)
        assert trace["candidates"] == []
        assert trace["withheld"] == 1

    async def test_restricted_fact_admitted_acting_restricted(
        self, fact_store: FactStore, shadow_log,
    ):
        fact_id = await _seed_fact(
            fact_store, protection_level="restricted",
        )
        await _emit(
            fact_store, _channel_event(classification="restricted"),
        )
        (trace,) = _traces(shadow_log)
        assert [c["fact_id"] for c in trace["candidates"]] == [fact_id]
        assert trace["candidates"][0]["protection_level"] == "restricted"

    async def test_unstamped_turn_floors_public_and_withholds(
        self, fact_store: FactStore, shadow_log,
    ):
        """Rule (b): a channel turn with no wire stamp acts ``public`` —
        an ``internal``-default fact is withheld in shadow exactly as it
        would be live."""
        await _seed_fact(fact_store)
        await _emit(fact_store, _channel_event(classification=None))
        (trace,) = _traces(shadow_log)
        assert trace["candidates"] == []
        assert trace["withheld"] == 1
        assert trace["acting"] is None

    async def test_epoch_wall_absolute(
        self, fact_store: FactStore, shadow_log,
    ):
        with epoch_scope("other-epoch"):
            await _seed_fact(fact_store)
        await _emit(fact_store, _channel_event())
        assert _traces(shadow_log) == []

    async def test_principal_wall_absolute(
        self, fact_store: FactStore, shadow_log,
    ):
        with principal_scope("other-tenant"):
            await _seed_fact(fact_store)
        await _emit(fact_store, _channel_event())
        assert _traces(shadow_log) == []

    async def test_senderless_event_issues_no_recall(
        self, fact_store: FactStore, shadow_log, monkeypatch,
    ):
        """The PR-5 empty-context cost guard holds on the shadow path:
        a TICK-shaped (sender-less) event costs zero DB round-trips."""
        calls: list[str] = []

        async def _spy(**kwargs):
            calls.append("recall")
            return []

        monkeypatch.setattr(fact_store, "recall", _spy)
        monkeypatch.setattr(fact_store, "topic_subjects", _spy)
        event = AgentEvent(
            event_type=EventType.TICK, payload={}, sender_id=None,
        )
        await _emit(fact_store, event)
        assert calls == []
        assert _traces(shadow_log) == []

    async def test_mode_off_issues_no_recall(
        self, fact_store: FactStore, shadow_log, monkeypatch,
    ):
        calls: list[str] = []

        async def _spy(**kwargs):
            calls.append("recall")
            return []

        monkeypatch.setattr(fact_store, "recall", _spy)
        monkeypatch.setattr(fact_store, "topic_subjects", _spy)
        await _seed_fact(fact_store)  # would be a candidate if on
        await _emit(fact_store, _channel_event(), mode=CROSS_ROOM_OFF)
        assert calls == []
        assert _traces(shadow_log) == []

    async def test_missing_store_is_noop(self, shadow_log):
        await _emit(None, _channel_event())
        assert _traces(shadow_log) == []

    async def test_backend_failure_never_raises(
        self, fact_store: FactStore, shadow_log, monkeypatch,
    ):
        async def _boom(**kwargs):
            raise RuntimeError("db exploded")

        monkeypatch.setattr(fact_store, "recall", _boom)
        await _emit(fact_store, _channel_event())  # must not raise
        assert _traces(shadow_log) == []

    async def test_non_str_stimulus_never_raises(
        self, fact_store: FactStore, shadow_log,
    ):
        """A bridge may hand a non-str content through — the shadow pass
        shares ``_inject_memory_context``'s never-fail contract; person
        seeds still produce the delta, topic seeding degrades to none."""
        fact_id = await _seed_fact(fact_store)
        await _emit(fact_store, _channel_event(), stimulus=12345)  # type: ignore[arg-type]
        (trace,) = _traces(shadow_log)
        assert [c["fact_id"] for c in trace["candidates"]] == [fact_id]

    async def test_object_text_never_logged(
        self, fact_store: FactStore, shadow_log,
    ):
        """Log-egress bound: the trace names ids / subjects / levels /
        provenance but never the fact object — the process log must not
        become the leak the §D gate closes at the prompt."""
        secret = "the-secret-object-payload"
        await _seed_fact(fact_store, object_=secret)
        await _emit(fact_store, _channel_event())
        (trace,) = _traces(shadow_log)
        assert trace["candidates"], "expected the seeded candidate"
        assert secret not in repr(trace)
        assert all(secret not in r.getMessage() for r in shadow_log.records)


# ─── End-to-end: shadow never enters the prompt ─────────────


class _ConcreteMemoryMixin:
    """Minimal concrete host for ``_MemoryContextMixin`` (the
    ``test_memory_context_channel_history`` harness shape)."""


def _build_mixin(fact_store: FactStore):
    from unittest.mock import AsyncMock

    from agents.clock import WallClock
    from agents.memory.episodic import EpisodicMemory
    from agents.memory.working import WorkingMemory
    from agents.persona_runtime.memory_context import _MemoryContextMixin

    class _Host(_MemoryContextMixin):
        def _format_event(self, event):  # type: ignore[override]
            payload = getattr(event, "payload", {}) or {}
            return str(payload.get("content", ""))

    mixin = _Host()
    mixin.agent_id = "shadow-test-agent"
    mixin._working_memory = WorkingMemory(max_tokens=8192)
    mixin._episodic_memory = EpisodicMemory(
        agent_id="shadow-test-agent", db_path=":memory:",
    )
    mixin._relationship_memory = AsyncMock()
    mixin._relationship_memory.get_relationship_summary.return_value = None
    mixin._fact_store = fact_store
    mixin._clock = WallClock()
    mixin._timezone = "UTC"
    return mixin


@_asyncio
class TestShadowNeverEntersPrompt:
    async def test_cross_room_fact_shadowed_but_not_injected(
        self, fact_store: FactStore, shadow_log,
    ):
        """The full ``_inject_memory_context`` pass: the cross-room row
        is traced, while the prompt, the §G manifest, and the
        reinforcement write see only the live (room-scoped) row."""
        cross_id = await _seed_fact(
            fact_store, object_="atlas-labs-cross-room",
        )
        live_id = await _seed_fact(
            fact_store, predicate="prefers", object_="coffee-live-row",
            session_id="legacy",
        )
        mixin = _build_mixin(fact_store)
        await mixin._episodic_memory.initialize()
        try:
            result = await mixin._inject_memory_context(_channel_event())
        finally:
            await mixin._episodic_memory.close()

        rendered = "\n".join(
            s.content for s in mixin._working_memory._sections
        )
        assert "coffee-live-row" in rendered
        assert "atlas-labs-cross-room" not in rendered
        manifest_ids = {e.entry_id for e in result.manifest}
        assert live_id in manifest_ids
        assert cross_id not in manifest_ids
        # Reinforcement targets only what reached the prompt.
        (cross_row,) = [
            f for f in await fact_store.recall(
                subject="bob", sessions="*",
            )
            if f.fact_id == cross_id
        ]
        assert cross_row.last_recalled_at is None
        # And the shadow trace recorded the cross-room row end-to-end.
        (trace,) = _traces(shadow_log)
        assert [c["fact_id"] for c in trace["candidates"]] == [cross_id]

    async def test_mode_off_on_mixin_emits_nothing(
        self, fact_store: FactStore, shadow_log,
    ):
        await _seed_fact(fact_store)
        mixin = _build_mixin(fact_store)
        mixin._facts_cross_room = CROSS_ROOM_OFF
        await mixin._episodic_memory.initialize()
        try:
            await mixin._inject_memory_context(_channel_event())
        finally:
            await mixin._episodic_memory.close()
        assert _traces(shadow_log) == []

    async def test_construction_knob_reaches_agent(self):
        """``memory.facts.cross_room`` resolves at agent construction
        (the ``_LLMPersonaAgent.__init__`` wiring)."""
        import copy

        from agents.persona import create_persona_agent

        from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

        default_agent = create_persona_agent(
            agent_id="ember-owl", config=copy.deepcopy(_PERSONA_CONFIG),
            llm_client=_make_client(),
        )
        assert default_agent._facts_cross_room == DEFAULT_FACTS_CROSS_ROOM

        config = copy.deepcopy(_PERSONA_CONFIG)
        config.setdefault("memory", {}).setdefault("facts", {})[
            "cross_room"
        ] = "off"
        off_agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=_make_client(),
        )
        assert off_agent._facts_cross_room == CROSS_ROOM_OFF
