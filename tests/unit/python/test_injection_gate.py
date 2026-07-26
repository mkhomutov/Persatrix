"""RFC 0037 §D hard gate (PR 4) — the injection matrix, the acting floor,
the §A fail directions THROUGH the gate, and the §G manifest.

PR 1 pinned the three §A fail directions at the lattice helpers, where the
contract is defined; this file pins the same three through the gate, where
they are enforced (the PR-plan's two-sided discipline):

* at/below-rank entries inject, above-rank entries are withheld;
* an unknown/absent ACTING level floors to ``public`` (rule (b)) —
  including the whole tick-shaped event class;
* an unknown ENTRY level is withheld AND logged, aggregated once per turn
  with the entry's identity (rule (c) — the helpers are pinned pure, so
  the log obligation lives here).

The positive-list test asserts every ``EventType`` member resolves to a
defined acting class (the ``episode_routing`` frozenset precedent): a new
event type must be consciously added to exactly one of the two sets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest

from agents.persona_runtime.classification import (
    CLASSIFICATION_RANKS,
    injectable_levels,
    levels_below_stamp,
)
from agents.persona_runtime.injection_gate import (
    CHANNEL_ACTING_EVENT_TYPES,
    PUBLIC_FLOOR_EVENT_TYPES,
    InjectionManifestEntry,
    TurnInjectionGate,
    acting_classification_for_event,
)
from agents.persona_runtime.memory_budget import MemoryBudget
from agents.persona_types import AgentEvent, EventType

LEVELS = tuple(CLASSIFICATION_RANKS)  # rank-ascending by construction


@dataclass(frozen=True)
class _Entry:
    """Minimal candidate shape the gate reads via getattr."""

    id: str
    protection_level: str | None
    source_channel_id: str | None = None


# ─── The §D injection matrix ─────────────────────────────────────────────


class TestInjectionMatrix:
    @pytest.mark.parametrize("acting", LEVELS)
    @pytest.mark.parametrize("entry_level", LEVELS)
    def test_at_or_below_injects_above_withholds(
        self, acting: str, entry_level: str,
    ) -> None:
        """Exhaustive 4×4: ``rank(P) <= rank(L)`` → inject, else withhold."""
        gate = TurnInjectionGate(acting=acting, agent_id="a1")
        kept = gate.filter_entries(
            "episodic", [_Entry(id="e1", protection_level=entry_level)],
        )
        should_inject = (
            CLASSIFICATION_RANKS[entry_level] <= CLASSIFICATION_RANKS[acting]
        )
        assert (len(kept) == 1) is should_inject

    def test_mixed_batch_partitions_per_entry(self) -> None:
        gate = TurnInjectionGate(acting="internal", agent_id="a1")
        kept = gate.filter_entries("facts", [
            _Entry(id="pub", protection_level="public"),
            _Entry(id="int", protection_level="internal"),
            _Entry(id="res", protection_level="restricted"),
            _Entry(id="sec", protection_level="secret"),
        ])
        assert [e.id for e in kept] == ["pub", "int"]


# ─── Rule (b): the acting floor ──────────────────────────────────────────


class TestActingFloor:
    @pytest.mark.parametrize("acting", [None, "", "moonbeam"])
    def test_unknown_acting_floors_to_public(self, acting: str | None) -> None:
        """Unknown/absent acting level injects ONLY ``public`` entries —
        the least-disclosing view, never the ``internal`` stamp default."""
        gate = TurnInjectionGate(acting=acting, agent_id="a1")
        kept = gate.filter_entries("notes", [
            _Entry(id="pub", protection_level="public"),
            _Entry(id="int", protection_level="internal"),
        ])
        assert [e.id for e in kept] == ["pub"]

    def test_tick_event_resolves_no_acting_level(self) -> None:
        """The autonomous tick carries no channel — §D floors it even when
        a malformed producer attached classification metadata."""
        event = AgentEvent(
            event_type=EventType.TICK,
            metadata={"channel_classification": "secret"},
        )
        assert acting_classification_for_event(event) is None

    def test_channel_event_reads_wire_stamp(self) -> None:
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            channel_id="group:leadership",
            metadata={"channel_classification": "restricted"},
        )
        assert acting_classification_for_event(event) == "restricted"

    def test_unclassified_channel_event_resolves_none(self) -> None:
        """The version-skew window (proto3 ``""`` from an older
        orchestrator): the event resolves ``None`` and rule (b) floors it."""
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE, channel_id="group:planning",
        )
        assert acting_classification_for_event(event) is None


# ─── Rule (c): unknown entry → withheld + logged, aggregated ─────────────


class TestUnknownEntryWithheldAndLogged:
    @pytest.mark.parametrize("bad_level", [None, "", "xyzzy", "INTERNAL"])
    def test_corrupted_label_never_injects(self, bad_level: str | None) -> None:
        """Even a ``secret``-acting turn cannot see a corrupted label —
        it is treated as above-``secret``, never coerced onto the lattice."""
        gate = TurnInjectionGate(acting="secret", agent_id="a1")
        kept = gate.filter_entries(
            "episodic", [_Entry(id="bad", protection_level=bad_level)],
        )
        assert kept == []

    def test_one_aggregated_warning_names_entries(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Rule (c)'s "and logged" half: ONE WARNING per turn naming each
        casualty's tier, id, raw label, and channel — not one line per
        entry (the §A log-flood rationale)."""
        gate = TurnInjectionGate(acting="internal", agent_id="a1")
        gate.filter_entries("episodic", [
            _Entry(id="bad-1", protection_level="xyzzy",
                   source_channel_id="group:leadership"),
            _Entry(id="bad-2", protection_level=""),
        ])
        with caplog.at_level(
            logging.WARNING, logger="agents.persona_runtime.injection_gate",
        ):
            gate.emit_log()
        warnings = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1, "aggregate per turn, never per entry"
        text = warnings[0].getMessage()
        assert "bad-1" in text and "bad-2" in text
        assert "group:leadership" in text
        assert "'xyzzy'" in text

    def test_clean_withholds_do_not_warn(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An above-rank withhold is normal operation — DEBUG, not WARNING."""
        gate = TurnInjectionGate(acting="public", agent_id="a1")
        gate.filter_entries(
            "notes", [_Entry(id="n1", protection_level="secret")],
        )
        with caplog.at_level(
            logging.DEBUG, logger="agents.persona_runtime.injection_gate",
        ):
            gate.emit_log()
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]
        assert [r for r in caplog.records if r.levelno == logging.DEBUG]


# ─── Positive-list acting-level coverage (review item 5) ─────────────────


class TestEveryEventTypeResolvesActingClass:
    def test_positive_lists_jointly_cover_the_enum(self) -> None:
        """Every ``EventType`` member belongs to exactly one acting class —
        a new event type forces a conscious choice (the ``episode_routing``
        frozenset precedent), because an unlisted type would otherwise get
        an acting level by accident rather than decision."""
        covered = CHANNEL_ACTING_EVENT_TYPES | PUBLIC_FLOOR_EVENT_TYPES
        assert covered == frozenset(EventType), (
            f"uncovered event types: {set(EventType) - covered}; "
            "add each to CHANNEL_ACTING_EVENT_TYPES or "
            "PUBLIC_FLOOR_EVENT_TYPES in injection_gate.py"
        )
        assert not (CHANNEL_ACTING_EVENT_TYPES & PUBLIC_FLOOR_EVENT_TYPES)

    @pytest.mark.parametrize(
        "event_type", sorted(PUBLIC_FLOOR_EVENT_TYPES, key=repr),
    )
    def test_floor_class_always_resolves_none(
        self, event_type: EventType,
    ) -> None:
        event = AgentEvent(
            event_type=event_type,
            metadata={"channel_classification": "secret"},
        )
        assert acting_classification_for_event(event) is None


# ─── The §G manifest (dark until PR 7) ───────────────────────────────────


class TestInjectionManifest:
    def test_manifest_labels_admitted_entries_only(self) -> None:
        """The manifest names what the budget ADMITTED, labeled with the
        gate-passed protection level — not what recall returned."""
        gate = TurnInjectionGate(acting="restricted", agent_id="a1")
        gate.filter_entries("facts", [
            _Entry(id="f-admitted", protection_level="restricted"),
            _Entry(id="f-dropped", protection_level="internal"),
        ])
        budget = MemoryBudget(total_tokens=1500)
        # Only one of the two gate-passed facts is budget-admitted.
        budget.record_admission(
            tier="facts", item_id="f-admitted", tokens_admitted=10,
        )
        assert gate.manifest(budget) == (
            InjectionManifestEntry(
                tier="facts", entry_id="f-admitted",
                protection_level="restricted",
            ),
        )

    def test_ungated_tier_admissions_are_absent(self) -> None:
        """A relationship admission (ungated tier) never reaches the
        manifest — the gate only labels entries it passed."""
        gate = TurnInjectionGate(acting="internal", agent_id="a1")
        budget = MemoryBudget(total_tokens=1500)
        budget.record_admission(
            tier="relationship", item_id="rel-1", tokens_admitted=5,
        )
        assert gate.manifest(budget) == ()


# ─── The two PR 4 lattice helpers ────────────────────────────────────────


class TestInjectableLevels:
    def test_known_levels_yield_prefix_sets(self) -> None:
        assert injectable_levels("public") == ("public",)
        assert injectable_levels("internal") == ("public", "internal")
        assert injectable_levels("restricted") == (
            "public", "internal", "restricted",
        )
        assert injectable_levels("secret") == LEVELS

    @pytest.mark.parametrize("acting", [None, "", "xyzzy"])
    def test_unknown_acting_floors_to_public_set(
        self, acting: str | None,
    ) -> None:
        """Rule (b) in the set domain — and never empty, which is why the
        memory boundary treats an empty allowlist as a caller bug."""
        assert injectable_levels(acting) == ("public",)


class TestLevelsBelowStamp:
    def test_strictly_below_the_rule_a_stamp(self) -> None:
        assert levels_below_stamp("public") == ()
        assert levels_below_stamp("internal") == ("public",)
        assert levels_below_stamp("secret") == (
            "public", "internal", "restricted",
        )

    @pytest.mark.parametrize("level", [None, "", "xyzzy"])
    def test_absent_resolves_via_stamp_default(self, level: str | None) -> None:
        """Rule (a), not rule (b): an absent acting level stamps
        ``internal``, so exactly ``public`` ranks below it."""
        assert levels_below_stamp(level) == ("public",)
