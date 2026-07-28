"""Room-first-RANKED episodic recall (RFC 0049 L1 amendment, PR 3).

Pins :func:`agents.memory.episodic_room_ranked.recall_room_ranked` — the
gated cross-room episodic recall mode the L1 amendment names, where the
RFC 0031 §D session filter becomes a ranking cue instead of a wall:

* same-room first at equal relevance (the dementia-test continuity bar
  as a *ranking* property), with the boost — not insert order or a
  recency accident — proven load-bearing by the unboosted counter-run;
* other-room episodes admissible (the wall is gone) and yet demoted —
  but a clearly better cross-room row CAN outrank a weak same-room row
  (ranking, not a hard two-tier sort);
* the ``legacy`` carve-out rides the boost set exactly as it rides the
  live wall;
* ``epoch`` and ``principal`` stay hard walls on the widened read;
* the read is SIDE-EFFECT-FREE by default — no ``access_count`` bump —
  which is what lets the shadow pass observe without perturbing live
  ranking, while ``reinforce=True`` (the PR 4 live prompt path) applies
  exactly the :meth:`EpisodicMemory.recall` bump; and
* ``sessions`` / ``boost_sessions`` are mutually exclusive at the query
  helpers themselves (the #783 either-wall-or-boost follow-up).
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.epoch_id import epoch_scope
from agents.memory._session_filter import ROOM_BOOST_FACTOR
from agents.memory.episodic import EpisodicMemory
from agents.memory.episodic_room_ranked import recall_room_ranked
from agents.principal_id import principal_scope
from agents.session_id import session_scope

_asyncio = pytest.mark.asyncio

#: The active room every test recalls from (rows stored elsewhere are
#: "cross-room" relative to it).
ROOM = "room-b"
OTHER_ROOM = "room-a"


async def _seed(
    memory: EpisodicMemory,
    summary: str = "atlas deployment retro",
    *,
    session_id: str = OTHER_ROOM,
    importance: float = 0.5,
    **kwargs,
) -> str:
    return await memory.store_episode(
        summary, {"k": "v"}, importance=importance,
        session_id=session_id, **kwargs,
    )


async def _ranked(memory: EpisodicMemory, query: str = "atlas", **kwargs):
    with session_scope(ROOM):
        return await recall_room_ranked(memory, query, **kwargs)


@_asyncio
class TestRoomFirstRanking:
    async def test_cross_room_row_admissible(self, memory: EpisodicMemory):
        """The wall is gone: an other-room episode is a candidate."""
        ep_id = await _seed(memory, session_id=OTHER_ROOM)
        ids = [ep.id for ep in await _ranked(memory)]
        assert ids == [ep_id]

    async def test_same_room_first_at_equal_relevance(
        self, memory: EpisodicMemory,
    ):
        """The amendment's headline property: at equal relevance (same
        summary, same importance) the same-room row leads.  That the
        BOOST — not recency or insert order — is what decides is pinned
        by the counter-run test below — and by store order here: the
        same-room row is stored FIRST (recency-disadvantaged by the
        insert-time epsilon), so an unboosted read would lead with the
        cross-room row."""
        same = await _seed(memory, session_id=ROOM)
        cross = await _seed(memory, session_id=OTHER_ROOM)
        ids = [ep.id for ep in await _ranked(memory)]
        assert ids == [same, cross]

    async def test_boost_is_load_bearing_not_recency(
        self, memory: EpisodicMemory,
    ):
        """The counter-run: a cross-room row 1.65x better on the
        composite score (importance 0.9 vs 0.5 — inside the 2.0 boost)
        outranks the same-room row on the UNBOOSTED all-sessions read,
        and still loses to it under room-first ranking.  Order flipping
        with the boost proves the CASE multiplier — not insert order,
        recency, or relevance — decides."""
        same = await _seed(memory, session_id=ROOM, importance=0.5)
        cross = await _seed(
            memory, session_id=OTHER_ROOM, importance=0.9,
        )
        with session_scope(ROOM):
            unboosted = [
                ep.id for ep in await memory.recall("atlas", sessions="*")
            ]
        assert unboosted == [cross, same]
        ids = [ep.id for ep in await _ranked(memory)]
        assert ids == [same, cross]

    async def test_much_better_cross_room_row_outranks(
        self, memory: EpisodicMemory,
    ):
        """Ranking, not a two-tier sort: a cross-room row whose
        composite score clears the boost factor (importance 1.0 → 1.0
        vs 0.0 → 0.1, a 10x edge over ``ROOM_BOOST_FACTOR=2.0``) leads
        the same-room row."""
        assert ROOM_BOOST_FACTOR < 10.0  # the edge this test relies on
        same = await _seed(memory, session_id=ROOM, importance=0.0)
        cross = await _seed(
            memory, session_id=OTHER_ROOM, importance=1.0,
        )
        ids = [ep.id for ep in await _ranked(memory)]
        assert ids == [cross, same]

    async def test_legacy_carveout_rides_the_boost(
        self, memory: EpisodicMemory,
    ):
        """A ``legacy`` row is boosted like the room's own (the §D wall
        admits legacy everywhere; the ranking mode keeps that status):
        at equal relevance it leads a cross-room row (stored first =
        recency-disadvantaged, so the boost is what decides)."""
        legacy = await _seed(memory, session_id="legacy")
        cross = await _seed(memory, session_id=OTHER_ROOM)
        ids = [ep.id for ep in await _ranked(memory)]
        assert ids == [legacy, cross]

    async def test_empty_query_recency_path_boosted(
        self, memory: EpisodicMemory,
    ):
        """The recency branch (no query) applies the same room boost
        (same-room stored first = recency-disadvantaged)."""
        same = await _seed(memory, session_id=ROOM)
        cross = await _seed(memory, session_id=OTHER_ROOM)
        ids = [ep.id for ep in await _ranked(memory, query="")]
        assert ids == [same, cross]

    async def test_like_fallback_path_boosted(self, memory: EpisodicMemory):
        """The LIKE branch (FTS5 unavailable) applies the same boost
        (same-room stored first = recency-disadvantaged)."""
        same = await _seed(memory, session_id=ROOM)
        cross = await _seed(memory, session_id=OTHER_ROOM)
        memory._fts5 = False
        ids = [ep.id for ep in await _ranked(memory)]
        assert ids == [same, cross]

    async def test_session_id_projected_for_provenance(
        self, memory: EpisodicMemory,
    ):
        """The Episode rows carry their room (``session_id``) — the
        shadow trace's provenance field and the PR 4 same-room /
        cross-room split."""
        await _seed(memory, session_id=OTHER_ROOM)
        (ep,) = await _ranked(memory)
        assert ep.session_id == OTHER_ROOM


@_asyncio
class TestAbsoluteWalls:
    async def test_epoch_wall_holds(self, memory: EpisodicMemory):
        with epoch_scope("other-epoch"):
            await _seed(memory)
        assert await _ranked(memory) == []

    async def test_principal_wall_holds(self, memory: EpisodicMemory):
        with principal_scope("other-tenant"):
            await _seed(memory)
        assert await _ranked(memory) == []


@_asyncio
class TestRecallContract:
    async def test_side_effect_free_no_access_bump(
        self, memory: EpisodicMemory,
    ):
        """The load-bearing shadow property: the DEFAULT ranked read
        must not reinforce — ``access_count`` feeds the live composite
        score, so a bumping shadow would perturb live ranking on later
        turns and shift the landed goldens off their cassettes."""
        ep_id = await _seed(memory)
        await _ranked(memory)
        row = await memory.get_episode(ep_id)
        assert row is not None
        assert row.access_count == 0
        assert row.last_accessed_at is None

    async def test_reinforce_bumps_like_live_recall(
        self, memory: EpisodicMemory,
    ):
        """``reinforce=True`` (the PR 4 live prompt path) applies the
        :meth:`EpisodicMemory.recall` access bump to every returned row
        — cross-room included (a used episode is a used episode
        wherever it was formed) — and refreshes the in-memory objects."""
        same = await _seed(memory, session_id=ROOM)
        cross = await _seed(memory, session_id=OTHER_ROOM)
        with session_scope(ROOM):
            rows = await recall_room_ranked(memory, "atlas", reinforce=True)
        assert {ep.id for ep in rows} == {same, cross}
        assert all(ep.access_count == 1 for ep in rows)
        for ep_id in (same, cross):
            row = await memory.get_episode(ep_id)
            assert row is not None
            assert row.access_count == 1
            assert row.last_accessed_at is not None

    async def test_pending_summary_rows_dropped(
        self, memory: EpisodicMemory,
    ):
        """The RFC 0020 unfinalised-close filter applies at the same
        chokepoint as :meth:`EpisodicMemory.recall`."""
        from agents.memory.interactions import SUMMARY_PENDING_TEXT

        kept_id = await _seed(memory)
        await memory.store_episode(
            SUMMARY_PENDING_TEXT, {}, session_id=OTHER_ROOM,
        )
        ids = [ep.id for ep in await _ranked(memory, query="")]
        assert ids == [kept_id]

    async def test_limit_validated(self, memory: EpisodicMemory):
        with pytest.raises(ValueError, match="limit"):
            await recall_room_ranked(memory, "atlas", limit=0)

    async def test_min_score_validated(self, memory: EpisodicMemory):
        with pytest.raises(ValueError, match="min_score"):
            await recall_room_ranked(memory, "atlas", min_score=1.5)


@_asyncio
class TestWallBoostGuard:
    """The #783 either-wall-or-boost follow-up: the three query helpers
    refuse ``sessions`` + ``boost_sessions`` together — boosting a
    subset of an already-filtered set silently re-creates the wall the
    ranked mode drops.  The guard runs before any DB access, so a
    ``None`` connection proves it fires first."""

    _NO_DB: Any = None  # the guard must fire before the connection is touched

    async def test_fts5_rejects_wall_plus_boost(self):
        from agents.memory.episodic_queries import recall_fts5

        with pytest.raises(ValueError, match="mutually exclusive"):
            await recall_fts5(
                self._NO_DB, "a", "q", 5, 0.0,
                sessions=["room-b"], boost_sessions=["room-b"],
            )

    async def test_like_rejects_wall_plus_boost(self):
        from agents.memory.episodic_queries import recall_like

        with pytest.raises(ValueError, match="mutually exclusive"):
            await recall_like(
                self._NO_DB, "a", "q", 5, 0.0,
                sessions=["room-b"], boost_sessions=["room-b"],
            )

    async def test_recency_rejects_wall_plus_boost(self):
        from agents.memory.episodic_queries import recall_recency

        with pytest.raises(ValueError, match="mutually exclusive"):
            await recall_recency(
                self._NO_DB, "a", 5, 0.0,
                sessions=["room-b"], boost_sessions=["room-b"],
            )
