"""
Unit tests for :class:`agents.memory.interactions.InteractionTracker`
and the boundary-detector chain (RFC 0020 PR 1).

PR 1 covers:

* tracker lifecycle (start, add_turn, close, idle_check)
* scope keying (different scopes → independent interactions)
* idle-check semantics with deterministic clock advance
* metric counter increments on open / close / idle-gap / structural

LLM-driven summarisation, the closing-state janitor, and the
persistence-side wiring land in later PRs and are deliberately not
exercised here.
"""

from __future__ import annotations

import pytest

from agents.memory.boundary_detectors import (
    DEFAULT_IDLE_TIMEOUT_SEC,
    REASON_IDLE_GAP,
    REASON_STRUCTURAL,
    IdleGapDetector,
    StructuralCloseDetector,
    TopicShiftDetector,
    default_detectors,
)
from agents.memory.interactions import (
    SCOPE_TICK,
    InteractionTracker,
    scope_for_dm,
    scope_for_group,
    scope_for_thread,
)


# ─── Scope helpers ──────────────────────────────────────────


class TestScopeBuilders:
    def test_dm_scope_is_symmetric(self):
        assert scope_for_dm("alice", "bob") == scope_for_dm("bob", "alice")

    def test_dm_scope_uses_canonical_prefix(self):
        # RFC 0020 §D pins the prefix vocabulary (`dm`, `thread`, `group`).
        assert scope_for_dm("a", "b").startswith("dm:")
        assert scope_for_thread("t1").startswith("thread:")
        assert scope_for_group("planning").startswith("group:")

    def test_tick_scope_constant(self):
        assert SCOPE_TICK == "tick"


# ─── Tracker lifecycle ──────────────────────────────────────


class TestInteractionTracker:
    def test_add_turn_opens_interaction_for_new_scope(self):
        tracker = InteractionTracker()
        i = tracker.add_turn("group:planning", now=100.0)
        assert i.is_open
        assert i.turn_count == 1
        assert i.scope == "group:planning"
        assert i.started_at == 100.0

    def test_repeated_add_turn_same_scope_appends(self):
        tracker = InteractionTracker()
        i1 = tracker.add_turn("group:planning", now=100.0)
        i2 = tracker.add_turn("group:planning", now=110.0)
        i3 = tracker.add_turn("group:planning", now=120.0)
        assert i1 is i2 is i3
        assert i1.turn_count == 3
        assert i1.last_turn_at == 120.0

    def test_different_scopes_yield_independent_interactions(self):
        tracker = InteractionTracker()
        a = tracker.add_turn("group:planning", now=100.0)
        b = tracker.add_turn("dm:alice:bob", now=100.0)
        assert a is not b
        assert a.interaction_id != b.interaction_id
        assert set(tracker.open_scopes()) == {"group:planning", "dm:alice:bob"}

    def test_close_sets_closed_at_and_reason(self):
        tracker = InteractionTracker()
        tracker.add_turn("tick", now=100.0)
        closed = tracker.close("tick", reason=REASON_STRUCTURAL, now=200.0)
        assert closed is not None
        assert closed.closed_at == 200.0
        assert closed.close_reason == REASON_STRUCTURAL
        assert tracker.get("tick") is None

    def test_close_unknown_scope_is_noop(self):
        tracker = InteractionTracker()
        assert tracker.close("never-opened") is None

    def test_reopen_rule_starts_fresh_interaction(self):
        # RFC 0020 §C: once closed, a new turn in the same scope starts a
        # fresh interaction (no reopen) — confirmed by a different
        # interaction_id.
        tracker = InteractionTracker()
        first = tracker.add_turn("dm:a:b", now=100.0)
        tracker.close("dm:a:b", reason=REASON_STRUCTURAL, now=110.0)
        second = tracker.add_turn("dm:a:b", now=120.0)
        assert second.interaction_id != first.interaction_id
        assert second.turn_count == 1

    def test_start_returns_existing_open_interaction(self):
        tracker = InteractionTracker()
        a = tracker.start("tick", now=100.0)
        b = tracker.start("tick", now=200.0)
        assert a is b
        assert a.started_at == 100.0  # not reset


# ─── Idle-gap semantics ─────────────────────────────────────


class TestIdleCheck:
    def test_idle_check_does_not_close_within_timeout(self):
        tracker = InteractionTracker(idle_timeout_sec=600.0)
        tracker.add_turn("tick", now=100.0)
        closed = tracker.idle_check(now=100.0 + 599.0)
        assert closed == []
        assert tracker.get("tick") is not None

    def test_idle_check_closes_after_timeout(self):
        tracker = InteractionTracker(idle_timeout_sec=600.0)
        tracker.add_turn("tick", now=100.0)
        closed = tracker.idle_check(now=100.0 + 600.0)
        assert len(closed) == 1
        assert closed[0].close_reason == REASON_IDLE_GAP
        assert tracker.get("tick") is None

    def test_subsequent_turn_after_idle_close_opens_new_interaction(self):
        tracker = InteractionTracker(idle_timeout_sec=600.0)
        first = tracker.add_turn("dm:a:b", now=100.0)
        tracker.idle_check(now=800.0)
        second = tracker.add_turn("dm:a:b", now=900.0)
        assert second.interaction_id != first.interaction_id

    def test_structural_marker_closes_via_idle_check(self):
        tracker = InteractionTracker(idle_timeout_sec=600.0)
        i = tracker.add_turn("thread:t1", now=100.0)
        i.structural_close_reason = REASON_STRUCTURAL
        closed = tracker.idle_check(now=110.0)
        assert len(closed) == 1
        assert closed[0].close_reason == REASON_STRUCTURAL

    def test_idle_check_with_no_open_interactions_returns_empty(self):
        tracker = InteractionTracker()
        assert tracker.idle_check(now=1000.0) == []


# ─── Boundary detectors (direct unit coverage) ──────────────


class TestBoundaryDetectors:
    def test_structural_detector_fires_on_marker(self):
        tracker = InteractionTracker()
        i = tracker.add_turn("thread:t1", now=100.0)
        i.structural_close_reason = REASON_STRUCTURAL
        should_close, reason = StructuralCloseDetector().evaluate(i, now=110.0)
        assert should_close is True
        assert reason == REASON_STRUCTURAL

    def test_structural_detector_silent_without_marker(self):
        tracker = InteractionTracker()
        i = tracker.add_turn("thread:t1", now=100.0)
        should_close, reason = StructuralCloseDetector().evaluate(i, now=110.0)
        assert should_close is False
        assert reason == ""

    def test_idle_gap_detector_returns_false_for_empty_interaction(self):
        tracker = InteractionTracker()
        i = tracker.start("tick", now=100.0)
        # No turns yet → nothing to close.
        should_close, _ = IdleGapDetector(idle_timeout_sec=10.0).evaluate(
            i, now=10_000.0,
        )
        assert should_close is False

    def test_topic_shift_detector_default_is_noop(self):
        tracker = InteractionTracker()
        i = tracker.add_turn("group:planning", now=100.0)
        assert TopicShiftDetector().evaluate(i, now=10_000.0) == (False, "")

    def test_default_detector_chain_priority(self):
        chain = default_detectors(idle_timeout_sec=600.0)
        assert isinstance(chain[0], StructuralCloseDetector)
        assert isinstance(chain[1], IdleGapDetector)
        assert isinstance(chain[2], TopicShiftDetector)
        # The supplied timeout flows into the IdleGapDetector instance.
        assert chain[1].idle_timeout_sec == 600.0

    def test_idle_timeout_default_constant_unchanged(self):
        # Pinning the spec value — RFC 0020 §B "default 600s (10 minutes)".
        assert DEFAULT_IDLE_TIMEOUT_SEC == 600.0


# ─── Metric counter wiring ──────────────────────────────────


class TestMetricEmission:
    """Verify the tracker emits the RFC 0020 §Phase 1 counters.

    Uses an :class:`InMemoryMetricReader` per the pattern in
    ``test_observability_metrics.py`` so the assertions stay
    decoupled from the OTLP exporter.
    """

    def _build_meter(self):
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        from agents.observability import metrics as metrics_mod

        reader = InMemoryMetricReader()
        metrics_mod.init_metrics(reader=reader)
        return reader, metrics_mod

    @staticmethod
    def _counter_total(reader, name: str) -> int:
        data = reader.get_metrics_data()
        if data is None:
            return 0
        total = 0
        for resource_metric in data.resource_metrics:
            for scope_metric in resource_metric.scope_metrics:
                for metric in scope_metric.metrics:
                    if metric.name == name:
                        for point in metric.data.data_points:
                            total += point.value
        return total

    def test_open_emits_opened_counter(self):
        reader, _ = self._build_meter()
        tracker = InteractionTracker()
        tracker.add_turn("tick", now=100.0)
        assert self._counter_total(reader, "agent.interactions.opened") == 1

    def test_close_emits_closed_and_by_structural(self):
        reader, _ = self._build_meter()
        tracker = InteractionTracker()
        tracker.add_turn("tick", now=100.0)
        tracker.close("tick", reason=REASON_STRUCTURAL, now=200.0)
        assert self._counter_total(reader, "agent.interactions.closed") == 1
        assert (
            self._counter_total(reader, "agent.interactions.closed.by_structural")
            == 1
        )

    def test_idle_close_emits_by_idle_gap(self):
        reader, _ = self._build_meter()
        tracker = InteractionTracker(idle_timeout_sec=10.0)
        tracker.add_turn("tick", now=100.0)
        tracker.idle_check(now=200.0)
        assert (
            self._counter_total(reader, "agent.interactions.closed.by_idle_gap")
            == 1
        )

    def test_no_metric_emission_when_uninitialised(self):
        # Before init_metrics(), tracker calls must not raise — the
        # try_get_instruments() call returns None and emission no-ops.
        from agents.observability import metrics as metrics_mod

        # Reset module state so this assertion is independent of test order.
        metrics_mod._provider = None
        metrics_mod._instruments = None
        tracker = InteractionTracker()
        tracker.add_turn("tick", now=100.0)
        tracker.close("tick", reason=REASON_STRUCTURAL, now=200.0)
        # No exception is the assertion; no counter to read.


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
