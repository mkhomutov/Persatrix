"""ISSUE-0131 — the fact-tier half of the ``speaker_id`` projection.

Split out of :mod:`.test_speaker_projection` (PR #849 review) when that
file reached exactly the 500-line cap enforced by
``scripts/checks/file_size.py --strict`` — the
``test_close_notification_room_fan`` precedent: the sibling module keeps
the shared record builders, this one imports them.

Two layers, deliberately both:

* ``TestFactProjection`` pins the WRITE — ``store_extracted_facts``
  stamps every tuple in a batch with the speaker it was handed, ``None``
  for a speakerless source, and the speaker is NOT the ``subject`` (who
  SAID it versus who it is ABOUT).
* ``TestTheFactDispatchCarriesItThrough`` pins the WIRING — the Phase-2
  seam ``dispatch_facts_from_response`` projects the record key's frozen
  speaker onto the batch, so deleting its ``speaker_id=`` argument fails
  here even though the write tests pass an explicit speaker.
"""

from __future__ import annotations

import json

import pytest

from agents.memory.facts import FactStore
from agents.persona_runtime.fact_extractor import (
    dispatch_facts_from_response,
    store_extracted_facts,
)

from .test_speaker_projection import _record


@pytest.fixture
async def fact_store():
    store = FactStore(agent_id="test-agent", db_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


async def _fact_speakers(fact_store: FactStore) -> set[str | None]:
    db = fact_store._ensure_db()
    async with db.execute("SELECT speaker_id FROM facts") as cursor:
        return {row[0] for row in await cursor.fetchall()}


class TestFactProjection:
    async def test_every_tuple_in_the_batch_carries_the_speaker(self, fact_store):
        stored = await store_extracted_facts(
            fact_store,
            facts=[
                {"subject": "alice", "predicate": "prefers", "object": "tea"},
                {"subject": "alice", "predicate": "works_at", "object": "acme"},
            ],
            source_interaction_id="i-1", asserted_at=1_100.0,
            session_id="legacy", speaker_id="amber-lynx",
        )

        assert stored == 2
        assert await _fact_speakers(fact_store) == {"amber-lynx"}

    async def test_a_speakerless_source_is_null(self, fact_store):
        await store_extracted_facts(
            fact_store,
            facts=[{"subject": "alice", "predicate": "prefers", "object": "tea"}],
            source_interaction_id="i-1", asserted_at=1_100.0,
            session_id="legacy", speaker_id=None,
        )

        assert await _fact_speakers(fact_store) == {None}

    async def test_the_storage_boundary_normalizes_an_empty_speaker(
        self, fact_store,
    ):
        """``"" → NULL`` is enforced in ``insert_fact`` itself (PR #849
        review round 3), so a direct caller bypassing the projection
        sites' ``or None`` discipline cannot mint a third speaker state
        alongside NULL and a real id."""
        await fact_store.store(
            subject="alice", predicate="prefers", object="tea",
            source_interaction_id="i-1", asserted_at=1_100.0,
            speaker_id="",
        )

        assert await _fact_speakers(fact_store) == {None}

    async def test_speaker_is_not_the_subject(self, fact_store):
        """The two columns answer different questions — who SAID it
        versus who it is ABOUT — and a counterparty fact differs in
        both."""
        await store_extracted_facts(
            fact_store,
            facts=[{"subject": "alice", "predicate": "prefers", "object": "tea"}],
            source_interaction_id="i-1", asserted_at=1_100.0,
            session_id="legacy", speaker_id="amber-lynx",
        )

        db = fact_store._ensure_db()
        async with db.execute("SELECT subject, speaker_id FROM facts") as cur:
            rows = await cur.fetchall()
        assert [tuple(r) for r in rows] == [("alice", "amber-lynx")]


class TestTheFactDispatchCarriesItThrough:
    """The Phase-2 seam, not just the leaf.

    ``TestFactProjection`` above calls ``store_extracted_facts`` with an
    explicit speaker, so it pins the WRITE but not the WIRING: delete
    ``dispatch_facts_from_response``'s ``speaker_id=`` argument and every
    close-extracted fact silently reverts to NULL with that class still
    green.  This is the same standard
    ``test_interaction_classification_capture`` sets for the RFC 0037 §C
    capture, whose docstring calls the dispatch pass-through "the only
    coverage" of that seam — the speaker projection rides the same
    function and needs the same pin.
    """

    async def test_the_records_speaker_reaches_the_stored_tuples(
        self, fact_store,
    ):
        await dispatch_facts_from_response(
            fact_store=fact_store,
            facts_raw=json.dumps([
                {"subject": "alice", "predicate": "prefers", "object": "tea"},
                {"subject": "alice", "predicate": "works_at", "object": "acme"},
            ]),
            interaction=_record("amber-lynx"),
            agent_id="test-agent",
            session_id="legacy",
        )

        assert await _fact_speakers(fact_store) == {"amber-lynx"}

    async def test_a_speakerless_record_dispatches_null(self, fact_store):
        await dispatch_facts_from_response(
            fact_store=fact_store,
            facts_raw=json.dumps([
                {"subject": "alice", "predicate": "prefers", "object": "tea"},
            ]),
            interaction=_record(""),
            agent_id="test-agent",
            session_id="legacy",
        )

        assert await _fact_speakers(fact_store) == {None}
