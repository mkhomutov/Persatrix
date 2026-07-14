"""Deterministic notes-recall order — :mod:`agents.memory._notes_recall`.

Guards the ``rowid`` insertion-order tiebreak on the ``updated_at DESC`` sort in
both non-FTS recall paths — :func:`_recall_notes_recency` (empty query) and
:func:`_recall_notes_like` (the FTS-unavailable LIKE fallback). Without it, notes
sharing an ``updated_at`` recall in SQLite-implementation-defined order, and
because the recall is ``LIMIT``-ed a tie at the cutoff would change *which* notes
surface — which would make a recorded RFC 0044 golden's assembled prompt
non-portable. ``notes.id`` is a random uuid4, so it cannot serve as the tiebreak;
``notes`` is an external-content FTS5 source (``content_rowid=rowid``), so it
always carries a stable, portable ``rowid``.
"""

from __future__ import annotations

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.notes import NoteStore

# ─── Fixture ────────────────────────────────────────────────


@pytest.fixture
async def note_store():
    """A ``NoteStore`` with ``fts5=False`` so both non-FTS paths are reachable.

    Backed by an initialized ``EpisodicMemory`` connection (notes schema + FTS
    triggers + migrations). ``fts5=False`` routes ``recall_notes(query)`` through
    the LIKE fallback (``_recall_notes_like``); ``recall_notes("")`` always uses
    the recency path (``_recall_notes_recency``).
    """
    mem = EpisodicMemory(agent_id="notes-agent", db_path=":memory:")
    await mem.initialize()
    store = NoteStore("notes-agent", mem._ensure_db(), fts5=False)  # noqa: SLF001
    yield store
    await mem.close()


# ─── Deterministic tiebreak ─────────────────────────────────


@pytest.mark.parametrize("query", ["", "keyword"])
async def test_notes_recall_equal_updated_at_deterministic(note_store, query):
    """Notes sharing an ``updated_at`` recall most-recently-inserted first — the
    ``rowid`` (insertion-order) tiebreak on ``updated_at DESC`` — on both the
    recency path (``query=""``) and the LIKE fallback (``query="keyword"``).

    Without the tiebreak SQLite's order among equal ``updated_at`` is
    implementation-defined, and with ``LIMIT`` a tie at the cutoff would change
    which notes surface — non-portable for RFC 0044 goldens. ``updated_at`` is
    forged equal to mirror the eval driver's FrozenClock stamping both writes in
    one instant; insertion order is preserved by ``rowid``."""
    first = await note_store.store_note("alpha", "shared keyword one")
    second = await note_store.store_note("beta", "shared keyword two")
    await note_store._db.execute(  # noqa: SLF001 — forge an updated_at tie
        "UPDATE notes SET updated_at = 1000.0 WHERE agent_id = ?",
        ("notes-agent",),
    )
    await note_store._db.commit()  # noqa: SLF001

    results = await note_store.recall_notes(query, limit=10)
    ids = [n.id for n in results]
    assert ids == [second, first], (
        "equal-updated_at notes must recall most-recently-inserted first "
        "(rowid DESC tiebreak), not SQLite's implementation-defined order"
    )
    # Stable across repeated calls — the property a golden's request hash needs.
    again = await note_store.recall_notes(query, limit=10)
    assert [n.id for n in again] == ids


async def test_notes_recall_equal_fts_rank_deterministic():
    """FTS5 search — notes with identical BM25 rank recall most-recently-inserted
    first, the ``n.rowid`` tiebreak on ``ORDER BY fts.rank * -1 DESC``.

    Two notes with identical indexed content (topic/content/tags) score equally
    for a query, and the recall is ``LIMIT``-ed, so without the tiebreak *which*
    note surfaces is implementation-defined — non-portable for RFC 0044 goldens.
    ``notes.id`` is a random uuid4, so ``n.rowid`` (the FTS join key, always
    stable for an external-content FTS5 source) is the portable tiebreak. Uses a
    dedicated ``fts5=True`` store since the module fixture is ``fts5=False``."""
    mem = EpisodicMemory(agent_id="fts-agent", db_path=":memory:")
    await mem.initialize()
    store = NoteStore("fts-agent", mem._ensure_db(), fts5=True)  # noqa: SLF001
    try:
        # Identical indexed content → identical BM25 rank for "alpha".
        first = await store.store_note("dup", "shared alpha term")
        second = await store.store_note("dup", "shared alpha term")
        results = await store.recall_notes("alpha", limit=10)
        ids = [n.id for n in results]
        assert ids == [second, first], (
            "equal-BM25-rank notes must recall most-recently-inserted first "
            "(n.rowid DESC tiebreak), not SQLite's implementation-defined order"
        )
        again = await store.recall_notes("alpha", limit=10)
        assert [n.id for n in again] == ids
    finally:
        await mem.close()
