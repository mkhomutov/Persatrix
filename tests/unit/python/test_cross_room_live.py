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
)
from agents.persona_runtime.episodes_shadow import (
    SHADOW_LOGGER_NAME as EPISODES_SHADOW_LOGGER,
)
from agents.persona_runtime.facts_shadow import (
    SHADOW_LOGGER_NAME as FACTS_SHADOW_LOGGER,
)
from agents.persona_types import AgentEvent, EventType

_asyncio = pytest.mark.asyncio

#: Every event in this file acts from room B; rows seeded in ``room-a``
#: are cross-room relative to it.
ROOM_A = "room-a"


def _channel_event(
    content: str = "atlas deployment retro",
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
        self, fact_store: FactStore, episodic: EpisodicMemory,
    ):
        """No ungated widening: a ``restricted``-stamped cross-room fact
        and episode are withheld from an internal-acting turn — prompt
        AND manifest — while same-level rows inject."""
        secret_fact = await _seed_fact(
            fact_store, object="secret-cross-room-fact",
            protection_level="restricted",
        )
        secret_ep = await episodic.store_episode(
            "atlas secret retro", {"k": "v"}, importance=0.9,
            session_id=ROOM_A, protection_level="restricted",
        )
        open_fact = await _seed_fact(
            fact_store, predicate="prefers", object="open-cross-room-fact",
        )
        mixin = _build_mixin(fact_store, episodic)
        result = await mixin._inject_memory_context(
            _channel_event(classification="internal"),
        )

        rendered = _rendered(mixin)
        assert "open-cross-room-fact" in rendered
        assert "secret-cross-room-fact" not in rendered
        assert "atlas secret retro" not in rendered
        manifest_ids = {e.entry_id for e in result.manifest}
        assert open_fact in manifest_ids
        assert secret_fact not in manifest_ids
        assert secret_ep not in manifest_ids

    async def test_restricted_turn_receives_restricted_rows(
        self, fact_store: FactStore, episodic: EpisodicMemory,
    ):
        """The withhold above is the gate working, not the wall coming
        back: the same rows inject on a turn acting at their level."""
        secret_fact = await _seed_fact(
            fact_store, object="secret-cross-room-fact",
            protection_level="restricted",
        )
        mixin = _build_mixin(fact_store, episodic)
        result = await mixin._inject_memory_context(
            _channel_event(classification="restricted"),
        )
        assert "secret-cross-room-fact" in _rendered(mixin)
        assert secret_fact in {e.entry_id for e in result.manifest}

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
