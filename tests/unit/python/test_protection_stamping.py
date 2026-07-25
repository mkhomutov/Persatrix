"""RFC 0037 §C (v0.3.12 PR 3) — interaction-open classification capture +
close-consolidation stamping onto the episodic and facts tiers.

The capture is the ``session_id`` precedent: the acting channel's wire
classification (and channel id) is frozen onto the
:class:`~agents.memory.interaction_types.Interaction` when it OPENS, held
verbatim in memory until close, and applied — through the §A rule-(a)
owner ``normalize_for_stamp`` — by the two stamp sites:

* the Phase-1 closing-row insert
  (:func:`agents.persona_runtime.close_path.persist_closed_interaction`);
* the facts-extraction dispatch
  (:func:`agents.persona_runtime.fact_extractor.store_extracted_facts`).

Everything here is DARK substrate: the stamped columns are read by
nothing until the RFC 0037 PR 4 §D gate.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.facts import FactStore
from agents.memory.interactions import InteractionTracker
from agents.memory.migrations import PROTECTION_LEVEL_DEFAULT
from agents.persona_runtime.classification import DEFAULT_CLASSIFICATION
from agents.persona_runtime.close_path import persist_closed_interaction

# ─── Tracker capture (frozen-at-open, the session_id rule) ──


class TestTrackerCapturesClassificationAtOpen:
    def test_add_turn_captures_pair_at_open(self) -> None:
        tracker = InteractionTracker()
        inter = tracker.add_turn(
            "scope-x", classification="restricted",
            source_channel_id="grp-ops",
        )
        assert inter.classification == "restricted"
        assert inter.source_channel_id == "grp-ops"

    def test_pair_frozen_after_open(self) -> None:
        # A later turn must not relabel the open record — mirrors the
        # sibling-mislabel guard the session capture pins, and implements
        # §C's "classification is read once per interaction".
        tracker = InteractionTracker()
        tracker.add_turn(
            "scope-x", classification="restricted",
            source_channel_id="grp-ops",
        )
        inter = tracker.add_turn(
            "scope-x", classification="public", source_channel_id="grp-other",
        )
        assert inter.classification == "restricted"
        assert inter.source_channel_id == "grp-ops"

    def test_default_capture_is_none(self) -> None:
        # Verbatim capture: an opening event with no wire classification
        # freezes ``None`` — the ``internal`` label is applied at the
        # STAMP site (rule (a)), never coerced into the tracker record.
        tracker = InteractionTracker()
        inter = tracker.add_turn("scope-y")
        assert inter.classification is None
        assert inter.source_channel_id is None

    def test_start_captures_pair(self) -> None:
        tracker = InteractionTracker()
        inter = tracker.start(
            "scope-z", classification="secret", source_channel_id="grp-war",
        )
        assert inter.classification == "secret"
        assert inter.source_channel_id == "grp-war"


# ─── Close-path episode stamping ────────────────────────────


class _StubLLM:
    """Summariser stub — Phase 2 is best-effort and top-level guarded, so
    an attribute-less stub simply leaves the pending row for the janitor;
    the Phase-1 insert under test is synchronous and unaffected."""


class _StubNS:
    facts = None
    relationship = None


def _closed_interaction(
    tracker: InteractionTracker,
    *,
    classification: str | None = None,
    source_channel_id: str | None = None,
):
    tracker.add_turn(
        "group:grp-ops", payload={"summary": "s"},
        classification=classification, source_channel_id=source_channel_id,
    )
    from agents.memory.boundary_detectors import REASON_STRUCTURAL

    closed = tracker.close("group:grp-ops", reason=REASON_STRUCTURAL)
    assert closed is not None
    return closed


class TestClosePathStampsEpisode:
    async def _persist(self, memory: EpisodicMemory, interaction) -> None:
        pending: set[asyncio.Task[None]] = set()

        async def _noop() -> None:
            return None

        await persist_closed_interaction(
            episodic=memory,
            # Stub stand-ins — Phase 2 is top-level guarded (best-effort),
            # so the Phase-1 insert under test never touches these.
            llm_client=cast("Any", _StubLLM()),
            memory_ns=cast("Any", _StubNS()),
            agent_id="test-agent", interaction=interaction,
            pending_tasks=pending, on_finalized=_noop,
        )
        # Drain the Phase-2 background task (it fails fast on the stub).
        for task in list(pending):
            await task

    async def _stamp_row(self, memory: EpisodicMemory, interaction):
        async with memory._ensure_db().execute(
            "SELECT protection_level, source_channel_id FROM episodes "
            "WHERE interaction_id = ?",
            (interaction.interaction_id,),
        ) as cursor:
            return await cursor.fetchone()

    async def test_restricted_interaction_stamps_restricted(
        self, memory: EpisodicMemory,
    ):
        tracker = InteractionTracker()
        closed = _closed_interaction(
            tracker, classification="restricted", source_channel_id="grp-ops",
        )
        await self._persist(memory, closed)
        assert await self._stamp_row(memory, closed) == (
            "restricted", "grp-ops",
        )

    async def test_uncaptured_interaction_stamps_internal(
        self, memory: EpisodicMemory,
    ):
        # Rule (a) at the stamp site: a ``None`` capture (tick /
        # pre-v0.3.12 producer) labels ``internal``, never ``public``.
        tracker = InteractionTracker()
        closed = _closed_interaction(tracker)
        await self._persist(memory, closed)
        assert await self._stamp_row(memory, closed) == (
            DEFAULT_CLASSIFICATION, None,
        )

    async def test_garbage_capture_stamps_internal(
        self, memory: EpisodicMemory,
    ):
        # The capture is verbatim (no seed-side allowlist), so the stamp
        # site must own the unknown→internal coercion.
        tracker = InteractionTracker()
        closed = _closed_interaction(
            tracker, classification="ultra-mega-secret",
            source_channel_id="grp-ops",
        )
        await self._persist(memory, closed)
        assert await self._stamp_row(memory, closed) == (
            DEFAULT_CLASSIFICATION, "grp-ops",
        )


# ─── Facts-extraction stamping ──────────────────────────────


class TestFactExtractionStamps:
    async def _facts(self) -> FactStore:
        store = FactStore(agent_id="test-agent", db_path=":memory:")
        await store.initialize()
        return store

    async def _levels(self, store: FactStore) -> list[tuple[str, str | None]]:
        async with store._ensure_db().execute(
            "SELECT protection_level, source_channel_id FROM facts",
        ) as cursor:
            return [(row[0], row[1]) for row in await cursor.fetchall()]

    async def test_batch_stamped_with_interaction_capture(self):
        from agents.persona_runtime.fact_extractor import store_extracted_facts

        store = await self._facts()
        try:
            stored = await store_extracted_facts(
                store,
                facts=[
                    {"subject": "alice", "predicate": "has_name",
                     "object": "Alice"},
                    {"subject": "alice", "predicate": "works_at",
                     "object": "the-lab"},
                ],
                source_interaction_id="ix-1", asserted_at=1000.0,
                session_id="legacy",
                protection_level="restricted", source_channel_id="grp-ops",
            )
            assert stored == 2
            assert await self._levels(store) == [
                ("restricted", "grp-ops"),
                ("restricted", "grp-ops"),
            ]
        finally:
            await store.close()

    async def test_unconditional_stamp_defaults_internal(self):
        # "There is no path that writes a fact without a protection
        # level" — an uncaptured interaction (None) stamps ``internal``.
        from agents.persona_runtime.fact_extractor import store_extracted_facts

        store = await self._facts()
        try:
            stored = await store_extracted_facts(
                store,
                facts=[{"subject": "bob", "predicate": "has_name",
                        "object": "Bob"}],
                source_interaction_id="ix-2", asserted_at=1000.0,
                session_id="legacy",
            )
            assert stored == 1
            assert await self._levels(store) == [
                (PROTECTION_LEVEL_DEFAULT, None),
            ]
        finally:
            await store.close()


# ─── The default-constant drift pin ─────────────────────────


class TestStampDefaultDriftPin:
    def test_storage_default_equals_lattice_stamp_default(self) -> None:
        """The §A rule-(a) stamping default is spelled twice — once in the
        lattice module (the rule owner) and once storage-side (the memory
        package must not import the persona subpackage).  This pin makes a
        drift on either side a test failure, the same discipline as the
        Go↔Python rank-table pins."""
        assert PROTECTION_LEVEL_DEFAULT == DEFAULT_CLASSIFICATION


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
