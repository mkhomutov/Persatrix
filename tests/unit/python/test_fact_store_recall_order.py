"""Deterministic fact-recall ordering — :class:`agents.memory.facts.FactStore`.

Guards the ``rowid`` insertion-order tiebreak on ``recall``'s ``asserted_at DESC``
sort (``agents/memory/facts.py``). Without it, facts sharing an ``asserted_at``
recall in SQLite-implementation-defined order, which would make a recorded RFC 0044
golden's assembled prompt non-portable between the record host and CI.

Split out of :mod:`tests.unit.python.test_fact_store` to keep that file under the
500-line size gate; mirrors its ``fact_store`` fixture.
"""

from __future__ import annotations

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
