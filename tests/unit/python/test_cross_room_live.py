"""RFC 0049 PR 4 — the promoted LIVE cross-room prompt path.

The live-mode counterpart of the two ``TestShadowNeverEntersPrompt``
suites: with ``memory.{facts,episodic}.cross_room: live`` (the shipped
default since the promotion), the widened recall IS the live recall.
This file pins the redefined F-3 bar the source scan in
``test_session_recall_default_path.py`` defers to — **no ungated
widening**:

* a cross-room fact / episode reaches the prompt, the §G manifest, and
  the reinforcement write (the promotion's whole point);
* every widened candidate still passes the RFC 0037 §D gate BEFORE the
  RFC 0017 budget — a ``restricted``-stamped row on an internal-acting
  turn is withheld from prompt and manifest alike;
* live mode emits NO shadow trace — the widened read happens once, on
  the live path (the #783 "fold live+widened into one query"
  follow-up: shadow mode's doubled episodic read is gone in live); and
* ``off`` keeps the pre-RFC-0049 room wall byte-for-byte.

Self-contained harness (unit test modules don't cross-import — the
``test_recall_tool_classification`` precedent); the mixin shape mirrors
``test_memory_context_channel_history``.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.facts import FactStore
from agents.persona_runtime.cross_room import (
    CROSS_ROOM_LIVE,
    CROSS_ROOM_OFF,
    CROSS_ROOM_SHADOW,
)
from agents.persona_runtime.episodes_shadow import (
    SHADOW_LOGGER_NAME as EPISODES_SHADOW_LOGGER,
)
from agents.persona_runtime.facts_shadow import (
    SHADOW_LOGGER_NAME as FACTS_SHADOW_LOGGER,
)
from agents.persona_types import AgentEvent, EventType

_asyncio = pytest.mark.asyncio

#: Every turn's query: the harness renders an event as its content.
_QUERY = "atlas deployment retro"

#: Every event in this file acts from room B; rows seeded in ``room-a``
#: are cross-room relative to it.
ROOM_A = "room-a"


def _channel_event(
    content: str = _QUERY,
    *,
    sender: str = "bob",
    classification: str = "internal",
) -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": content},
        channel_id="group:room-b",
        sender_id=sender,
        metadata={"channel_classification": classification},
    )


@pytest.fixture
async def fact_store():
    store = FactStore(agent_id="live-test-agent", db_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
async def episodic():
    mem = EpisodicMemory(agent_id="live-test-agent", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


def _build_mixin(fact_store: FactStore, episodic: EpisodicMemory):
    from unittest.mock import AsyncMock

    from agents.clock import WallClock
    from agents.memory.working import WorkingMemory
    from agents.persona_runtime.memory_context import _MemoryContextMixin

    class _Host(_MemoryContextMixin):
        def _format_event(self, event):  # type: ignore[override]
            payload = getattr(event, "payload", {}) or {}
            return str(payload.get("content", ""))

    mixin = _Host()
    mixin.agent_id = "live-test-agent"
    mixin._working_memory = WorkingMemory(max_tokens=8192)
    mixin._episodic_memory = episodic
    mixin._relationship_memory = AsyncMock()
    mixin._relationship_memory.get_relationship_summary.return_value = None
    mixin._fact_store = fact_store
    mixin._clock = WallClock()
    mixin._timezone = "UTC"
    # Live is the class-attribute default; pinned explicitly so this
    # suite keeps testing the promoted path even if the default moves.
    mixin._facts_cross_room = CROSS_ROOM_LIVE
    mixin._episodic_cross_room = CROSS_ROOM_LIVE
    return mixin


async def _seed_fact(store: FactStore, **kwargs: Any) -> str:
    params: dict[str, Any] = {
        "subject": "bob",
        "predicate": "works_at",
        "object": "atlas-labs-cross-room",
        "source_interaction_id": "int-1",
        "asserted_at": 1000.0,
        "session_id": ROOM_A,
    }
    params.update(kwargs)
    return await store.store(**params)


#: The words only the restricted room-A episode carries — what the gate
#: tests look for in the prompt.
RESTRICTED_EPISODE_FRAGMENT = "sealed minutes"


async def _seed_gate_rows(
    fact_store: FactStore, episodic: EpisodicMemory,
) -> tuple[str, str, str]:
    """Seed the rows both gate tests share, so their two turns differ only
    in the acting classification.  Returns the ids of a restricted fact, a
    restricted episode and an open fact, all from room A."""
    restricted_fact = await _seed_fact(
        fact_store, object="secret-cross-room-fact",
        protection_level="restricted",
    )
    # The summary opens with the whole query: the full-text search needs
    # every query word, and the fallback used without it needs the query
    # as one unbroken run of text.  The score floor also drops as the
    # store grows (ISSUE-0159), so the gate test checks the gate judged
    # the episode.  Real restricted episodes carry an interaction id (the
    # close path sets one); without it, a withheld episode skips the §E
    # projection lookup.
    restricted_ep = await episodic.store_episode(
        f"{_QUERY} {RESTRICTED_EPISODE_FRAGMENT}", {"k": "v"},
        importance=0.9, session_id=ROOM_A, protection_level="restricted",
        interaction_id="ix-restricted",
    )
    # Not ``works_at``: with the restricted fact's subject, predicate and
    # room it would supersede that fact, which would then never be recalled.
    open_fact = await _seed_fact(
        fact_store, predicate="prefers", object="open-cross-room-fact",
    )
    return restricted_fact, restricted_ep, open_fact


@pytest.fixture
def gates(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every §D gate a turn builds, kept so a test can read what it judged.

    A row missing from the prompt proves nothing alone: the recall may
    never have returned it.  The gate's decisions show it was a candidate
    on that very turn, and whether the gate let it through."""
    from agents.persona_runtime import memory_context

    built: list[Any] = []

    class _Recording(memory_context.TurnInjectionGate):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            built.append(self)

    monkeypatch.setattr(memory_context, "TurnInjectionGate", _Recording)
    return built


def _judged(
    gates: list[Any], tier: str, id_attr: str = "id",
) -> dict[str, bool]:
    """The turn's one gate: each ``tier`` candidate's id → admitted."""
    (gate,) = gates
    return {getattr(e, id_attr): ok for e, ok in gate.decisions(tier)}


def _rendered(mixin) -> str:
    return "\n".join(s.content for s in mixin._working_memory._sections)


@pytest.fixture
def shadow_logs(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.INFO, logger=FACTS_SHADOW_LOGGER):
        with caplog.at_level(logging.INFO, logger=EPISODES_SHADOW_LOGGER):
            yield caplog


def _shadow_traces(caplog: pytest.LogCaptureFixture) -> list[dict]:
    return [
        payload
        for r in caplog.records
        for attr in ("facts_shadow", "episodes_shadow")
        if isinstance(payload := getattr(r, attr, None), dict)
    ]


@_asyncio
class TestLiveCrossRoomInjection:
    async def test_cross_room_fact_injected_and_reinforced(
        self, fact_store: FactStore, episodic: EpisodicMemory, shadow_logs,
    ):
        """The promotion's point: a room-A fact reaches a room-B turn's
        prompt, §G manifest, and reinforcement write — and NO shadow
        trace is emitted (one widened read, on the live path)."""
        cross_id = await _seed_fact(fact_store)
        mixin = _build_mixin(fact_store, episodic)
        result = await mixin._inject_memory_context(_channel_event())

        assert "atlas-labs-cross-room" in _rendered(mixin)
        assert cross_id in {e.entry_id for e in result.manifest}
        (row,) = await fact_store.recall(subject="bob", sessions="*")
        assert row.last_recalled_at is not None
        assert _shadow_traces(shadow_logs) == []

    async def test_cross_room_episode_ranked_in_and_reinforced(
        self, fact_store: FactStore, episodic: EpisodicMemory, shadow_logs,
    ):
        """A room-A episode is admissible on a room-B turn (ranked, not
        walled), reinforced exactly like the pre-promotion live recall."""
        ep_id = await episodic.store_episode(
            "atlas deployment retro", {"k": "v"},
            importance=0.5, session_id=ROOM_A,
        )
        mixin = _build_mixin(fact_store, episodic)
        result = await mixin._inject_memory_context(_channel_event())

        assert "atlas deployment retro" in _rendered(mixin)
        assert ep_id in {e.entry_id for e in result.manifest}
        row = await episodic.get_episode(ep_id)
        assert row is not None and row.access_count >= 1
        assert _shadow_traces(shadow_logs) == []

    async def test_gate_withholds_restricted_on_internal_turn(
        self, fact_store: FactStore, episodic: EpisodicMemory, gates,
    ):
        """No ungated widening: a ``restricted``-stamped cross-room fact
        and episode are withheld from an internal-acting turn — prompt
        AND manifest — while same-level rows inject."""
        secret_fact, secret_ep, open_fact = await _seed_gate_rows(
            fact_store, episodic,
        )
        mixin = _build_mixin(fact_store, episodic)
        result = await mixin._inject_memory_context(
            _channel_event(classification="internal"),
        )

        # Both were recalled and withheld on this very turn, so the
        # absences below are the gate's doing, whatever the store holds.
        assert _judged(gates, "facts", "fact_id").get(secret_fact) is False
        assert _judged(gates, "episodic").get(secret_ep) is False
        rendered = _rendered(mixin)
        assert "open-cross-room-fact" in rendered
        assert "secret-cross-room-fact" not in rendered
        assert RESTRICTED_EPISODE_FRAGMENT not in rendered
        manifest_ids = {e.entry_id for e in result.manifest}
        assert open_fact in manifest_ids
        assert secret_fact not in manifest_ids
        assert secret_ep not in manifest_ids

    async def test_restricted_turn_receives_restricted_rows(
        self, fact_store: FactStore, episodic: EpisodicMemory,
    ):
        """The withhold above is the gate working — not the wall coming
        back, and not a row the recall never returned: the same fact and
        episode inject on a turn acting at their level."""
        secret_fact, secret_ep, _ = await _seed_gate_rows(fact_store, episodic)
        mixin = _build_mixin(fact_store, episodic)
        result = await mixin._inject_memory_context(
            _channel_event(classification="restricted"),
        )

        rendered = _rendered(mixin)
        manifest_ids = {e.entry_id for e in result.manifest}
        # One comparison, so a failure reports all four at once.
        seen = {
            "fact in prompt": "secret-cross-room-fact" in rendered,
            "episode in prompt": RESTRICTED_EPISODE_FRAGMENT in rendered,
            "fact in manifest": secret_fact in manifest_ids,
            "episode in manifest": secret_ep in manifest_ids,
        }
        assert seen == dict.fromkeys(seen, True)

    @pytest.mark.parametrize(
        ("mode", "reads"), [(CROSS_ROOM_LIVE, 0), (CROSS_ROOM_SHADOW, 1)],
    )
    async def test_live_mode_skips_the_shadow_episode_read(
        self, fact_store: FactStore, episodic: EpisodicMemory,
        monkeypatch: pytest.MonkeyPatch, mode: str, reads: int,
    ):
        """Live mode reads the widened episodes once, on the live path.  The
        no-trace checks above cannot see a second read — in live mode the
        shadow pass finds nothing new to log — so this counts the shadow
        pass's own reads; ``shadow`` mode shows the count moves."""
        from agents.persona_runtime import episodes_shadow

        calls: list[str] = []

        async def _spy(*args: Any, **kwargs: Any) -> list[Any]:
            calls.append("widened read")
            return []

        monkeypatch.setattr(episodes_shadow, "recall_room_ranked", _spy)
        mixin = _build_mixin(fact_store, episodic)
        mixin._episodic_cross_room = mode
        await mixin._inject_memory_context(_channel_event())
        assert len(calls) == reads

    async def test_off_mode_keeps_the_wall(
        self, fact_store: FactStore, episodic: EpisodicMemory, shadow_logs,
    ):
        """``off`` = the pre-RFC-0049 posture: cross-room rows stay
        outside the prompt and no trace is emitted."""
        await _seed_fact(fact_store)
        await episodic.store_episode(
            "atlas deployment retro", {"k": "v"},
            importance=0.5, session_id=ROOM_A,
        )
        mixin = _build_mixin(fact_store, episodic)
        mixin._facts_cross_room = CROSS_ROOM_OFF
        mixin._episodic_cross_room = CROSS_ROOM_OFF
        result = await mixin._inject_memory_context(_channel_event())

        rendered = _rendered(mixin)
        assert "atlas-labs-cross-room" not in rendered
        assert "atlas deployment retro" not in rendered
        assert result.manifest == ()
        assert _shadow_traces(shadow_logs) == []
