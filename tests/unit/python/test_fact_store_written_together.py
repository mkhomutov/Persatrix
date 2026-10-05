"""
Facts written together by one extraction stay live together
(:class:`agents.memory.facts.FactStore`, ISSUE-0181).

One interaction close stamps every fact it extracts with one source
interaction and one ``asserted_at``.  The equal-timestamp tie rule used
to fire inside every such batch, so only the last-listed fact stayed
live: a briefing that set three constraints on one topic left one.

:mod:`tests.unit.python.test_fact_store_supersede` pins the
latest-asserted-wins rule across different sources.  This file pins its
one exception under each write ordering, and the cases the exception
must leave alone.  :mod:`tests.unit.python.test_facts_written_together`
follows the same facts from the extractor to the prompt.
"""

from __future__ import annotations

import pytest

from agents.memory.facts import FactStore


@pytest.fixture
async def fact_store():
    """FactStore against an in-memory SQLite DB.

    Mirrors the fixture in :mod:`tests.unit.python.test_fact_store_supersede`.
    """
    store = FactStore(agent_id="test-agent", db_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


# ─── Helpers ────────────────────────────────────────────────

_TOPIC = "event planning"


async def _store_together(
    store: FactStore,
    objects: list[str],
    *,
    source: str | None,
    at: float,
    session_id: str = "run-a",
) -> list[str]:
    """Store one ``topic.decided`` fact per object, the way one extraction does.

    Every row gets the same source interaction and the same
    ``asserted_at``: the extractor stamps a whole batch with the close
    time of the conversation it came from.  Returns the ids in write
    order.
    """
    return [
        await store.store(
            subject=_TOPIC,
            predicate="topic.decided",
            object=obj,
            source_interaction_id=source,
            asserted_at=at,
            session_id=session_id,
        )
        for obj in objects
    ]


async def _superseded_by(store: FactStore) -> dict[str, str | None]:
    """Map every stored fact id to the id that superseded it, if any."""
    rows = await store.recall(
        subject=_TOPIC, include_superseded=True, sessions="*",
    )
    return {r.fact_id: r.superseded_by for r in rows}


async def _live_objects(store: FactStore) -> list[str]:
    """Objects of the live rows, in recall order (newest first)."""
    rows = await store.recall(subject=_TOPIC, sessions="*")
    return [r.object for r in rows]


class TestFactsFromOneExtractionCoexist:
    """Facts one conversation taught together must not overwrite each other.

    One interaction close stamps every fact it extracts with one source
    interaction and one ``asserted_at``.  The tie rule used to fire
    inside every such batch, so only the last-listed fact stayed live
    (ISSUE-0181: a briefing's three constraints became one).  Rows with
    the same source and the same instant now stay live together unless
    they repeat each other word for word.  Everything else keeps the
    latest-asserted-wins rule pinned in
    :mod:`tests.unit.python.test_fact_store_supersede`.
    """

    async def test_distinct_facts_from_one_extraction_all_stay_live(
        self, fact_store: FactStore,
    ):
        """The issue's case: three constraints from one briefing."""
        ids = await _store_together(
            fact_store,
            [
                "Saturdays in May are unavailable",
                "ticket price ceiling is $25",
                "lighting crew limited to two people",
            ],
            source="ix-1", at=1000.0,
        )
        pointers = await _superseded_by(fact_store)
        assert [pointers[i] for i in ids] == [None, None, None]
        assert await _live_objects(fact_store) == [
            "lighting crew limited to two people",
            "ticket price ceiling is $25",
            "Saturdays in May are unavailable",
        ]

    async def test_later_conversation_supersedes_every_earlier_fact(
        self, fact_store: FactStore,
    ):
        """A later conversation still replaces the whole earlier set.

        Its first fact retires every earlier row at once; its own facts
        then stay live together.
        """
        a, b, c = await _store_together(
            fact_store, ["a", "b", "c"], source="ix-1", at=1000.0,
        )
        d, e = await _store_together(
            fact_store, ["d", "e"], source="ix-2", at=2000.0,
        )
        pointers = await _superseded_by(fact_store)
        assert [pointers[a], pointers[b], pointers[c]] == [d, d, d]
        assert [pointers[d], pointers[e]] == [None, None]
        assert await _live_objects(fact_store) == ["e", "d"]

    async def test_out_of_order_older_writes_point_at_the_latest_fact(
        self, fact_store: FactStore,
    ):
        """An older batch arriving late is superseded row by row.

        Several rows can now be live at the newest instant, so the
        pointer goes to the last one inserted, the row recall lists
        first.  The newer rows stay live.
        """
        a, b = await _store_together(
            fact_store, ["a", "b"], source="ix-new", at=2000.0,
        )
        p, q = await _store_together(
            fact_store, ["p", "q"], source="ix-old", at=1000.0,
        )
        pointers = await _superseded_by(fact_store)
        assert [pointers[a], pointers[b]] == [None, None]
        assert [pointers[p], pointers[q]] == [b, b]
        assert await _live_objects(fact_store) == ["b", "a"]

    async def test_repeated_fact_replaces_only_its_twin(
        self, fact_store: FactStore,
    ):
        """A word-for-word repeat inside one batch leaves one row.

        The second ``a`` supersedes the first ``a`` and leaves ``b``
        alone, so the prompt never prints one fact twice.
        """
        first_a, b, second_a = await _store_together(
            fact_store, ["a", "b", "a"], source="ix-1", at=1000.0,
        )
        pointers = await _superseded_by(fact_store)
        assert pointers[first_a] == second_a
        assert [pointers[b], pointers[second_a]] == [None, None]
        assert await _live_objects(fact_store) == ["a", "b"]

    async def test_active_session_facts_absorb_a_legacy_row_once(
        self, fact_store: FactStore,
    ):
        """The legacy carve-out still works for a batch.

        The first fact of a named session's batch retires the older
        ``legacy`` row; the second leaves the first alone.
        """
        (legacy,) = await _store_together(
            fact_store, ["old"], source="ix-0", at=500.0,
            session_id="legacy",
        )
        x, y = await _store_together(
            fact_store, ["x", "y"], source="ix-1", at=1000.0,
            session_id="run-a",
        )
        pointers = await _superseded_by(fact_store)
        assert pointers[legacy] == x
        assert [pointers[x], pointers[y]] == [None, None]

    async def test_legacy_session_facts_coexist(
        self, fact_store: FactStore,
    ):
        """A batch written in the ``legacy`` session coexists too."""
        ids = await _store_together(
            fact_store, ["a", "b"], source="ix-1", at=1000.0,
            session_id="legacy",
        )
        pointers = await _superseded_by(fact_store)
        assert [pointers[i] for i in ids] == [None, None]

    async def test_another_source_at_the_same_instant_replaces_them_all(
        self, fact_store: FactStore,
    ):
        """Pins a known limit: the exception is per source, not per instant.

        Two records closed at one instant (a room-close fan) have
        different source ids, so the tie rule still decides between
        them: the later arrival retires every row the other one wrote.
        Widening the exception to the instant must be a deliberate
        change to this test.
        """
        a, b = await _store_together(
            fact_store, ["a", "b"], source="ix-1", at=1000.0,
        )
        (c,) = await _store_together(
            fact_store, ["c"], source="ix-2", at=1000.0,
        )
        pointers = await _superseded_by(fact_store)
        assert [pointers[a], pointers[b]] == [c, c]
        assert await _live_objects(fact_store) == ["c"]

    async def test_same_source_at_a_later_time_still_supersedes(
        self, fact_store: FactStore,
    ):
        """Pin (passes before the fix): source alone is not "together".

        A fact re-derived from the same source at a later time is an
        update, so it replaces the earlier one.
        """
        (a,) = await _store_together(
            fact_store, ["a"], source="replay-1", at=1000.0,
        )
        (b,) = await _store_together(
            fact_store, ["b"], source="replay-1", at=2000.0,
        )
        pointers = await _superseded_by(fact_store)
        assert pointers[a] == b
        assert await _live_objects(fact_store) == ["b"]

    async def test_unsourced_writes_keep_the_tie_rule(
        self, fact_store: FactStore,
    ):
        """Pin (passes before the fix): no source means no batch.

        Two rows with no source interaction were not written together
        by any extraction, so at one instant the later arrival wins.
        """
        a, b = await _store_together(
            fact_store, ["a", "b"], source=None, at=1000.0,
        )
        pointers = await _superseded_by(fact_store)
        assert pointers[a] == b
        assert await _live_objects(fact_store) == ["b"]

    async def test_unsourced_write_supersedes_a_sourced_tie(
        self, fact_store: FactStore,
    ):
        """Pin (passes before the fix): an unsourced write still wins a tie.

        Guards the source comparison against SQL's three-valued logic:
        comparing a stored id with a missing one must read as
        "different source", not as "unknown".
        """
        (a,) = await _store_together(
            fact_store, ["a"], source="ix-1", at=1000.0,
        )
        (b,) = await _store_together(
            fact_store, ["b"], source=None, at=1000.0,
        )
        pointers = await _superseded_by(fact_store)
        assert pointers[a] == b
        assert await _live_objects(fact_store) == ["b"]
