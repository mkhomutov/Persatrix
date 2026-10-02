"""ISSUE-0163 — the live episodic read reinforces only what reached the prompt.

``access_count`` multiplies an episode's recall ranking, so counting a use
of a row the prompt never carried lets it climb on every later turn. The
live read (``cross_room: live``, the default) counts no use; once the
prompt is assembled, each episode the budget admitted gains one, the rule
the facts tier follows. Split from ``test_cross_room_live.py`` at the
size cap; self-contained like it (unit test modules don't cross-import).
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.facts import FactStore
from agents.persona_runtime.cross_room import CROSS_ROOM_LIVE, CROSS_ROOM_OFF
from agents.persona_types import AgentEvent, EventType

_asyncio = pytest.mark.asyncio

#: Rows seeded in ``room-a`` are cross-room relative to every turn here.
ROOM_A = "room-a"
ROOM_B = "group:room-b"
#: A DM the agent held with Alice alone.  Room B adds Bob, so the
#: audience check withholds a row that came from this DM on a room-B turn.
DM = "dm:alice:live-test-agent"

#: The two ways the gate withholds an episode on a room-B turn: its
#: classification ranks above the turn's, or its source room did not hold
#: everyone in room B.
WITHHOLD_CAUSES: dict[str, dict[str, Any]] = {
    "classification": {"protection_level": "restricted"},
    "audience": {"source_channel_id": DM},
}


def _channel_event(
    content: str = "atlas deployment retro",
    *,
    event_type: EventType = EventType.CHANNEL_MESSAGE,
) -> AgentEvent:
    return AgentEvent(
        event_type=event_type,
        payload={"content": content},
        channel_id=ROOM_B,
        sender_id="bob",
        metadata={"channel_classification": "internal"},
    )


class _Rooms:
    """A channel-roster fetcher over fixed memberships: room B holds Alice
    and Bob, the DM holds Alice alone.  Without one, every audience verdict
    is unknown, and ``live`` admits unknowns."""

    _MEMBERS: dict[str, list[str]] = {
        ROOM_B: ["alice", "bob", "live-test-agent"],
        DM: ["alice", "live-test-agent"],
    }

    async def fetch_members(self, channel_id: str) -> dict[str, Any] | None:
        members = self._MEMBERS.get(channel_id)
        if members is None:
            return None
        return {
            "id": channel_id,
            "name": channel_id,
            "members": [{"id": member} for member in members],
        }

    async def fetch_directory(self) -> list[dict[str, Any]] | None:
        return None


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
    mixin._facts_cross_room = CROSS_ROOM_LIVE
    mixin._episodic_cross_room = CROSS_ROOM_LIVE
    return mixin


def _rendered(mixin) -> str:
    return "\n".join(s.content for s in mixin._working_memory._sections)


@_asyncio
class TestOnlyWhatReachedThePromptIsReinforced:
    """ISSUE-0163.  ``access_count`` multiplies an episode's ranking score,
    so reinforcing a row it did not use would let the persona rank it
    higher on every later turn.  The live read used to bump every row it
    returned before the §D gate and the audience check ran; now the rows
    the budget admits are bumped after the prompt is assembled, the rule
    the facts tier already follows."""

    @pytest.mark.parametrize("cause", sorted(WITHHOLD_CAUSES))
    async def test_withheld_episode_is_not_reinforced(
        self, fact_store: FactStore, episodic: EpisodicMemory, cause: str,
    ):
        """A row the gate withholds, for either reason, stays unreinforced
        after a turn it ranked into."""
        withheld = await episodic.store_episode(
            "atlas deployment retro", {"k": "v"}, importance=0.5,
            session_id=ROOM_A, **WITHHOLD_CAUSES[cause],
        )
        mixin = _build_mixin(fact_store, episodic)
        mixin.set_roster_fetcher(_Rooms())
        result = await mixin._inject_memory_context(_channel_event())

        assert "atlas deployment retro" not in _rendered(mixin)
        assert withheld not in {e.entry_id for e in result.manifest}
        row = await episodic.get_episode(withheld)
        assert row is not None
        assert row.access_count == 0
        assert row.last_accessed_at is None

    async def test_episode_the_budget_drops_is_not_reinforced(
        self, fact_store: FactStore, episodic: EpisodicMemory,
    ):
        """Passing the gate is not enough: the budget has room for the
        first line (8 tokens) but not the second (18), and only the first
        is reinforced."""
        kept = await episodic.store_episode(
            "atlas deployment retro", {"k": "v"}, importance=0.9,
            session_id=ROOM_A,
        )
        dropped = await episodic.store_episode(
            "atlas deployment retro follow-up: owners, dates and the "
            "rollback plan", {"k": "v"}, importance=0.1, session_id=ROOM_A,
        )
        mixin = _build_mixin(fact_store, episodic)
        mixin._memory_budget_tokens = 12
        result = await mixin._inject_memory_context(_channel_event())

        assert [e.entry_id for e in result.manifest] == [kept]
        kept_row = await episodic.get_episode(kept)
        dropped_row = await episodic.get_episode(dropped)
        assert kept_row is not None and kept_row.access_count == 1
        assert dropped_row is not None and dropped_row.access_count == 0

    async def test_withheld_episode_does_not_hold_a_recall_slot(
        self, fact_store: FactStore, episodic: EpisodicMemory,
    ):
        """What the unearned bumps cost.  The withheld row is the only
        match on two turns; if those turns reinforced it, its score
        (importance 0.4, ×(1 + ln 3) ≈ 2.1) would beat the five admissible
        rows' (importance 0.5, ×1) on the third turn, and it would take one
        of the five recall slots while never reaching the prompt."""
        withheld = await episodic.store_episode(
            "atlas zephyr retro", {"k": "v"}, importance=0.4,
            session_id=ROOM_A, protection_level="restricted",
        )
        admissible = [
            await episodic.store_episode(
                f"atlas retro note-{i}", {"k": "v"}, importance=0.5,
                session_id="room-c",
            )
            for i in range(5)
        ]
        mixin = _build_mixin(fact_store, episodic)
        for _ in range(2):
            await mixin._inject_memory_context(_channel_event("zephyr"))
        result = await mixin._inject_memory_context(_channel_event("atlas"))

        episodic_ids = {e.entry_id for e in result.manifest if e.tier == "episodic"}
        assert episodic_ids == set(admissible)
        row = await episodic.get_episode(withheld)
        assert row is not None and row.access_count == 0

    async def test_walled_recall_still_reinforces_once(
        self, fact_store: FactStore, episodic: EpisodicMemory,
    ):
        """``off`` keeps the walled read, which bumps what it returns; the
        post-budget bump is for the live read only, so an admitted row is
        not counted twice.  A mention turn, because on a channel message
        the channel-history recall bumps the same row as well."""
        ep_id = await episodic.store_episode(
            "atlas deployment retro", {"k": "v"}, importance=0.5,
        )
        mixin = _build_mixin(fact_store, episodic)
        mixin._episodic_cross_room = CROSS_ROOM_OFF
        result = await mixin._inject_memory_context(
            _channel_event(event_type=EventType.MENTION),
        )

        assert ep_id in {e.entry_id for e in result.manifest}
        row = await episodic.get_episode(ep_id)
        assert row is not None and row.access_count == 1
