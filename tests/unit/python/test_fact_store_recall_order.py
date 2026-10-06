"""Deterministic fact-recall ordering — :class:`agents.memory.facts.FactStore`.

Guards the ``rowid`` insertion-order tiebreak on ``recall``'s ``asserted_at DESC``
sort (``agents/memory/facts.py``). Without it, facts sharing an ``asserted_at``
recall in SQLite-implementation-defined order, which would make a recorded RFC 0044
golden's assembled prompt non-portable between the record host and CI.

Split out of :mod:`tests.unit.python.test_fact_store` to keep that file under the
500-line size gate; mirrors its ``fact_store`` fixture.
"""

from __future__ import annotations

from typing import get_args

import pytest

from agents.memory.facts import FactStore

# ─── Fixture ────────────────────────────────────────────────


@pytest.fixture
async def fact_store():
    """FactStore against an in-memory SQLite DB (mirrors ``test_fact_store``)."""
    store = FactStore(agent_id="test-agent", db_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


# ─── Deterministic tiebreak ─────────────────────────────────


async def test_recall_order_deterministic_for_equal_asserted_at(
    fact_store: FactStore,
):
    """Two facts asserted in the same instant recall in a stable, defined order —
    the ``rowid`` (insertion-order) tiebreak on ``asserted_at DESC``.

    Without the tiebreak, SQLite's row order among equal ``asserted_at`` keys is
    implementation-defined and can differ across engine versions, which would make
    a recorded RFC 0044 golden's assembled prompt non-portable between the record
    host and CI. Distinct predicates keep both rows live (supersede is per
    ``subject`` + ``predicate``); ``fact_id`` is a random uuid4, so it could not
    serve as the tiebreak."""
    first = await fact_store.store(
        subject="bob", predicate="has_name", object="Bob",
        source_interaction_id="ix-1", asserted_at=1000.0,
    )
    second = await fact_store.store(
        subject="bob", predicate="lives_in", object="Berlin",
        source_interaction_id="ix-2", asserted_at=1000.0,  # identical instant
    )

    results = await fact_store.recall(subject="bob")
    assert [f.fact_id for f in results] == [second, first], (
        "equal-asserted_at facts must recall most-recently-inserted first "
        "(rowid DESC tiebreak), not in SQLite's implementation-defined order"
    )
    # Stable across repeated calls — the property a golden's request hash needs.
    again = await fact_store.recall(subject="bob")
    assert [f.fact_id for f in again] == [f.fact_id for f in results]


# ─── Both ends (ISSUE-0180) ─────────────────────────────────
#
# ``order="both_ends"`` lists a subject's facts from the newest and the
# oldest alternately, so a ``limit`` or a token budget applied afterwards
# drops the middle of a long list.  The facts tier reads the room's own
# subject this way: what a room was first told about it must not be the
# first thing a later, busier room pushes out.


async def _room_fact(store: FactStore, n: int, *, at: float | None = None) -> str:
    """One live fact about ``atlas`` from room ``n`` (its own session)."""
    return await store.store(
        subject="atlas", predicate="topic.has_status", object=f"fact {n}",
        source_interaction_id=f"ix-{n}", session_id=f"room-{n}",
        asserted_at=1000.0 + n if at is None else at,
    )


async def _objects(store: FactStore, **kwargs) -> list[str]:
    rows = await store.recall(subject="atlas", sessions="*", **kwargs)
    return [f.object for f in rows]


async def test_both_ends_alternates_newest_and_oldest(fact_store: FactStore):
    for n in range(1, 6):
        await _room_fact(fact_store, n)
    assert await _objects(fact_store, order="both_ends") == [
        "fact 5", "fact 1", "fact 4", "fact 2", "fact 3",
    ]


async def test_newest_first_stays_the_default(fact_store: FactStore):
    for n in range(1, 6):
        await _room_fact(fact_store, n)
    newest_first = ["fact 5", "fact 4", "fact 3", "fact 2", "fact 1"]
    assert await _objects(fact_store) == newest_first
    assert await _objects(fact_store, order="newest") == newest_first


async def test_both_ends_limit_drops_the_middle(fact_store: FactStore):
    """The row cap keeps the first-told facts.  Newest-first with the same
    cap returns facts 7 to 4 and loses every one of the oldest three."""
    for n in range(1, 8):
        await _room_fact(fact_store, n)
    assert await _objects(fact_store, order="both_ends", limit=4) == [
        "fact 7", "fact 1", "fact 6", "fact 2",
    ]
    assert await _objects(fact_store, limit=4) == [
        "fact 7", "fact 6", "fact 5", "fact 4",
    ]


async def test_both_ends_keeps_one_extractions_facts_in_listed_order(
    fact_store: FactStore,
):
    """A briefing's three facts share one instant and one source (they stay
    live together, ISSUE-0181).  From the old end they come back in the
    order the extraction listed them, which is their ``rowid`` order."""
    for obj in ("hall booked on saturdays in may", "tickets capped at $25",
                "lighting crew down to two"):
        await fact_store.store(
            subject="atlas", predicate="topic.has_status", object=obj,
            source_interaction_id="briefing", session_id="room-0",
            asserted_at=1000.0,
        )
    for n in range(1, 5):
        await _room_fact(fact_store, n, at=2000.0 + n)
    assert await _objects(fact_store, order="both_ends", limit=6) == [
        "fact 4", "hall booked on saturdays in may",
        "fact 3", "tickets capped at $25",
        "fact 2", "lighting crew down to two",
    ]


async def test_both_ends_with_one_row_and_with_none(fact_store: FactStore):
    assert await _objects(fact_store, order="both_ends") == []
    await _room_fact(fact_store, 1)
    assert await _objects(fact_store, order="both_ends") == ["fact 1"]


async def test_both_ends_honours_the_predicate_and_live_filters(
    fact_store: FactStore,
):
    """The order changes nothing about which rows are read: superseded rows
    stay out, and a predicate filter still narrows the read."""
    await fact_store.store(
        subject="atlas", predicate="topic.has_status", object="old status",
        source_interaction_id="ix-a", asserted_at=1000.0,
    )
    await fact_store.store(
        subject="atlas", predicate="topic.has_status", object="new status",
        source_interaction_id="ix-b", asserted_at=2000.0,
    )
    await fact_store.store(
        subject="atlas", predicate="topic.has_deadline", object="friday",
        source_interaction_id="ix-c", asserted_at=3000.0,
    )
    assert await _objects(fact_store, order="both_ends") == ["friday", "new status"]
    assert await _objects(
        fact_store, order="both_ends", predicates={"topic.has_status"},
    ) == ["new status"]
    assert await _objects(
        fact_store, order="both_ends", include_superseded=True,
    ) == ["friday", "old status", "new status"]


async def test_unknown_order_is_refused(fact_store: FactStore):
    with pytest.raises(ValueError, match="order"):
        await fact_store.recall(subject="atlas", order="oldest")  # type: ignore[arg-type]


async def test_every_declared_order_has_a_read_of_its_own(fact_store: FactStore):
    """A value added to ``RecallOrder`` must get its own branch in ``recall``
    rather than read newest-first under another name: every declared value
    reads, and no two of them give the same order."""
    from agents.memory.facts import RecallOrder

    for n in range(1, 4):
        await _room_fact(fact_store, n)
    orders = get_args(RecallOrder)
    reads = {tuple(await _objects(fact_store, order=order)) for order in orders}
    assert len(reads) == len(orders) == 2
