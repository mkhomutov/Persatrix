"""RFC 0037 §G (v0.3.12 PR 7) — the per-turn tripwire watch built off the
§D gate's decision record.

The persona-runtime half of Phase 3: after the gate has filtered every
tier, the WITHHELD candidates (the entries §D kept out of the prompt —
the only entries whose text can be *above* the publish target, given the
§B single-channel-turn guard) become a hash-only :class:`TripwireWatch`
stamped onto the turn's event metadata, which ``DispatchContext.for_event``
lifts structurally to the ``ActionExecutor``.

Load-bearing contracts pinned here:

* **Withheld entries only.** An admitted entry's level is ≤ the acting
  level = the §B-guarded publish target, so it can never satisfy §G's
  "protection level above the target" condition — hashing it would be
  hot-path dead weight (the PR 6 common-case economics precedent).
* **Rule-(c) casualties ride the watch** at the sentinel level
  ``unknown`` — a corrupted stored label is treated above-``secret`` by
  the gate, and its text surfacing in output is exactly the mis-stamp
  class §G exists to surface. The raw label never rides (unbounded /
  possibly content-bearing).
* **Empty watch → no stamp** — the common case (nothing withheld) adds
  zero bytes and zero work to the turn.
* **The metric registers and fires** (`channel.confidentiality.tripwire_hits`),
  and is a no-op before ``init_metrics`` — call sites never guard.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

import agents.observability.metrics as pmetrics
from agents.confidentiality_tripwire import (
    TRIPWIRE_WATCH_METADATA_KEY,
    span_hashes,
)
from agents.observability._metrics_confidentiality import record_tripwire_hit
from agents.persona_runtime.injection_gate import (
    _MANIFEST_TIERS,
    TurnInjectionGate,
)
from agents.persona_runtime.tripwire_watch import (
    TIER_CONTENT_ATTRS,
    build_tripwire_watch,
    stamp_turn_tripwire_watch,
)
from agents.persona_types import AgentEvent, EventType

# Long enough to clear the span threshold; distinct per fixture.
_EPISODE_TEXT = (
    "The leadership sync agreed to acquire Meadowlark Systems for ninety "
    "million dollars closing next quarter"
)
_NOTE_TEXT = (
    "Draft term sheet for the Meadowlark acquisition is stored in the "
    "restricted data room pending signatures"
)
_FACT_TEXT = (
    "negotiating the confidential Meadowlark Systems acquisition deal "
    "expected to close in the next quarter"
)
_SHORT_TEXT = "too short to watch"


@dataclass(frozen=True)
class _Episode:
    id: str
    summary: str
    protection_level: str | None
    source_channel_id: str | None = None


@dataclass(frozen=True)
class _Fact:
    fact_id: str
    object: str
    protection_level: str | None
    source_channel_id: str | None = None


@dataclass(frozen=True)
class _Note:
    id: str
    content: str
    protection_level: str | None
    source_channel_id: str | None = None


def _gate_with_withholds(*, acting: str = "internal") -> TurnInjectionGate:
    """A gate that admitted one internal episode and withheld one restricted
    episode, one secret note, one restricted fact, and one unknown-label
    channel-history row."""
    gate = TurnInjectionGate(acting=acting, agent_id="ember-owl")
    gate.filter_entries("channel_history", [
        _Episode(id="ch-bad", summary=_EPISODE_TEXT, protection_level="mangled!"),
    ])
    gate.filter_entries("episodic", [
        _Episode(id="ep-ok", summary=_EPISODE_TEXT, protection_level="internal"),
        _Episode(id="ep-res", summary=_EPISODE_TEXT, protection_level="restricted"),
    ])
    gate.filter_entries("facts", [
        _Fact(fact_id="f-res", object=_FACT_TEXT, protection_level="restricted"),
    ], id_attr="fact_id")
    gate.filter_entries("notes", [
        _Note(id="n-sec", content=_NOTE_TEXT, protection_level="secret"),
    ])
    return gate


class TestBuildWatch:
    def test_withheld_entries_ride_with_their_levels(self) -> None:
        watch = build_tripwire_watch(_gate_with_withholds())
        assert watch is not None
        assert watch.acting == "internal"
        by_id = {e.entry_id: e for e in watch.entries}
        assert by_id["ep-res"].protection_level == "restricted"
        assert by_id["ep-res"].tier == "episodic"
        assert by_id["n-sec"].protection_level == "secret"
        assert by_id["f-res"].protection_level == "restricted"
        assert by_id["ep-res"].span_hashes == span_hashes(_EPISODE_TEXT)
        assert by_id["n-sec"].span_hashes == span_hashes(_NOTE_TEXT)

    def test_admitted_entries_never_ride(self) -> None:
        watch = build_tripwire_watch(_gate_with_withholds())
        assert watch is not None
        assert "ep-ok" not in {e.entry_id for e in watch.entries}

    def test_unknown_label_rides_as_sentinel_never_raw(self) -> None:
        watch = build_tripwire_watch(_gate_with_withholds())
        assert watch is not None
        by_id = {e.entry_id: e for e in watch.entries}
        assert by_id["ch-bad"].protection_level == "unknown"
        assert "mangled" not in repr(watch)

    def test_nothing_withheld_builds_none(self) -> None:
        gate = TurnInjectionGate(acting="secret", agent_id="a1")
        gate.filter_entries("episodic", [
            _Episode(id="e1", summary=_EPISODE_TEXT, protection_level="internal"),
        ])
        assert build_tripwire_watch(gate) is None

    def test_below_threshold_content_is_unwatchable(self) -> None:
        gate = TurnInjectionGate(acting="public", agent_id="a1")
        gate.filter_entries("episodic", [
            _Episode(id="e1", summary=_SHORT_TEXT, protection_level="secret"),
        ])
        # The only withheld entry has no spans above the threshold → no
        # watchable entry → no watch at all.
        assert build_tripwire_watch(gate) is None

    def test_tier_map_matches_gate_tier_order(self) -> None:
        """Drift pin: the content-attr map IS the watch's tier walk — it
        must name exactly the gate's §D-gated tiers."""
        assert tuple(TIER_CONTENT_ATTRS) == _MANIFEST_TIERS


class TestStampTurnWatch:
    def _event(self) -> AgentEvent:
        return AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hi"},
            channel_id="group:planning",
            sender_id="alice",
            metadata={"channel_classification": "internal"},
        )

    def test_withholds_stamp_the_event(self) -> None:
        event = self._event()
        stamp_turn_tripwire_watch(event, _gate_with_withholds())
        watch = event.metadata.get(TRIPWIRE_WATCH_METADATA_KEY)
        assert watch is not None

    def test_no_withholds_stamp_nothing(self) -> None:
        event = self._event()
        gate = TurnInjectionGate(acting="secret", agent_id="a1")
        stamp_turn_tripwire_watch(event, gate)
        assert TRIPWIRE_WATCH_METADATA_KEY not in event.metadata

    def test_stamp_failure_never_propagates(self) -> None:
        """The injection path's never-fail contract: a poisoned gate must
        not take down the turn."""
        event = self._event()

        class _Broken(TurnInjectionGate):
            def decisions(self, tier: str) -> Any:
                raise RuntimeError("poisoned decision record")

        stamp_turn_tripwire_watch(
            event, _Broken(acting="internal", agent_id="a1"),
        )
        assert TRIPWIRE_WATCH_METADATA_KEY not in event.metadata


class TestTripwireMetric:
    @pytest.fixture
    def metric_reader(self) -> Iterator[InMemoryMetricReader]:
        reader = InMemoryMetricReader()
        pmetrics.init_metrics(reader=reader)
        try:
            yield reader
        finally:
            asyncio.run(pmetrics.shutdown())

    @staticmethod
    def _points(reader: InMemoryMetricReader) -> list[Any]:
        data = reader.get_metrics_data()
        assert data is not None
        points: list[Any] = []
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    if metric.name == "channel.confidentiality.tripwire_hits":
                        points.extend(metric.data.data_points)
        return points

    def test_hit_records_bounded_attributes(
        self, metric_reader: InMemoryMetricReader,
    ) -> None:
        record_tripwire_hit(tier="episodic", protection_level="restricted")
        points = self._points(metric_reader)
        assert len(points) == 1
        assert points[0].value == 1
        assert dict(points[0].attributes) == {
            "tier": "episodic", "protection_level": "restricted",
        }

    def test_noop_before_registration(self) -> None:
        import agents.observability._metrics_confidentiality as mod

        original = mod._tripwire
        mod._tripwire = None
        try:
            record_tripwire_hit(tier="notes", protection_level="secret")
        finally:
            mod._tripwire = original
