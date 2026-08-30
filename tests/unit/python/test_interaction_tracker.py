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
from _otel_test_helpers import counter_total

from agents.memory.boundary_detectors import (
    DEFAULT_IDLE_TIMEOUT_SEC,
    REASON_CATCHUP_COMPLETE,
    REASON_COST,
    REASON_IDLE_GAP,
    REASON_MAX_TURNS,
    REASON_SHUTDOWN,
    REASON_STRUCTURAL,
    REASON_TOPIC_SHIFT,
    IdleGapDetector,
    MaxTurnsDetector,
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
        opened = tracker.add_turn("tick", now=100.0)
        closed = tracker.close_record(
            opened, reason=REASON_STRUCTURAL, now=200.0,
        )
        assert closed is not None
        assert closed.closed_at == 200.0
        assert closed.close_reason == REASON_STRUCTURAL
        assert tracker.get("tick") is None

    def test_close_unknown_scope_is_noop(self):
        """PR #846 review: the scope-keyed ``close`` was removed (it
        resolved the ambient principal and empty speaker, so post-re-key
        it closed nothing or the wrong record).  The no-op contract it
        carried now belongs to the room fan, which returns an empty
        list rather than ``None``."""
        tracker = InteractionTracker()
        assert tracker.close_scope(
            "never-opened", reason=REASON_STRUCTURAL,
        ) == []

    def test_reopen_rule_starts_fresh_interaction(self):
        # RFC 0020 §C: once closed, a new turn in the same scope starts a
        # fresh interaction (no reopen) — confirmed by a different
        # interaction_id.
        tracker = InteractionTracker()
        first = tracker.add_turn("dm:a:b", now=100.0)
        tracker.close_record(first, reason=REASON_STRUCTURAL, now=110.0)
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
        # Ordering pinned per RFC 0020 §B: structural pre-empts idle,
        # idle pre-empts the max-turns safety net, topic-shift (no-op
        # in v0.3.0) is last.  PR-216 review (Should-Fix #1) inserted
        # MaxTurnsDetector at slot [2] to enforce
        # DEFAULT_MAX_INTERACTION_TURNS — see boundary_detectors.py.
        assert isinstance(chain[0], StructuralCloseDetector)
        assert isinstance(chain[1], IdleGapDetector)
        assert isinstance(chain[2], MaxTurnsDetector)
        assert isinstance(chain[3], TopicShiftDetector)
        # The supplied timeout flows into the IdleGapDetector instance.
        assert chain[1].idle_timeout_sec == 600.0

    def test_idle_timeout_default_constant_unchanged(self):
        # Pinning the spec value — RFC 0020 §B "default 600s (10 minutes)".
        assert DEFAULT_IDLE_TIMEOUT_SEC == 600.0


# ─── PR-3 review #12: MaxTurns cap fires inline in add_turn ────


class TestMaxTurnsInlineCap:
    """RFC 0020 PR 6 slice 6 — PR-3 review finding #12.

    Before this slice, :class:`MaxTurnsDetector` was wired into the
    default chain but only consulted from :meth:`InteractionTracker.idle_check`,
    which the runtime calls at the *top* of the next event.  An interaction
    whose ``turn_count`` reached the cap therefore stayed in the
    tracker's ``_open`` map until *another* event arrived in any scope
    — and a structural close that fired in the meantime would label the
    closure as ``REASON_STRUCTURAL`` rather than ``REASON_MAX_TURNS``,
    losing the security-sensitive attribution RFC 0020 §Security
    Considerations names.

    This slice tightens enforcement: :meth:`add_turn` evaluates the
    cap inline after appending the turn and closes the interaction
    immediately when the cap is reached.  ``REASON_MAX_TURNS`` is the
    only reason that can fire inline (structural is event-driven and
    idle-gap requires time to pass since the just-appended turn).
    """

    def test_add_turn_at_cap_closes_interaction_inline(self):
        # Cap of 3 chosen so the test doesn't have to thread a 200-deep
        # loop just to exercise the boundary.  Wire only MaxTurnsDetector
        # so the fixture is hermetic — no idle / structural surface to
        # confound the assertion.
        tracker = InteractionTracker(detectors=(MaxTurnsDetector(max_turns=3),))
        tracker.add_turn("dm:a:b", now=100.0)
        tracker.add_turn("dm:a:b", now=110.0)
        third = tracker.add_turn("dm:a:b", now=120.0)
        # Inline-cap contract: add_turn returns the (now-closed)
        # interaction so the caller can observe ``is_open`` and persist.
        assert third.turn_count == 3
        assert third.is_open is False
        assert third.close_reason == REASON_MAX_TURNS
        assert third.closed_at == 120.0
        # The scope is popped from the open map per RFC 0020 §C "do not
        # reopen" — same shape as :meth:`close`.
        assert tracker.get("dm:a:b") is None

    def test_add_turn_below_cap_does_not_close(self):
        tracker = InteractionTracker(detectors=(MaxTurnsDetector(max_turns=3),))
        first = tracker.add_turn("dm:a:b", now=100.0)
        second = tracker.add_turn("dm:a:b", now=110.0)
        assert first is second
        assert first.is_open
        assert tracker.get("dm:a:b") is first

    def test_subsequent_turn_after_inline_cap_opens_new_interaction(self):
        # Confirms the "close-and-reopen on overflow" wording in the
        # finding: the next add_turn after the cap fired starts a fresh
        # interaction rather than extending the closed one (RFC 0020
        # §C reopen rule).
        tracker = InteractionTracker(detectors=(MaxTurnsDetector(max_turns=2),))
        tracker.add_turn("dm:a:b", now=100.0)
        capped = tracker.add_turn("dm:a:b", now=110.0)
        assert capped.is_open is False
        next_turn = tracker.add_turn("dm:a:b", now=120.0)
        assert next_turn.interaction_id != capped.interaction_id
        assert next_turn.turn_count == 1
        assert next_turn.is_open

    def test_inline_cap_fires_max_turns_subtotal_counter(self):
        # The counter wiring already exists (slice 2) — this test
        # pins that the inline path drives the same dispatch table.
        # Without the inline-cap fix, the counter would remain at 0
        # because no idle_check was called in this test body.
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        from agents.observability import metrics as metrics_mod

        saved_provider = metrics_mod._provider
        saved_instruments = metrics_mod._instruments
        metrics_mod._provider = None
        metrics_mod._instruments = None
        try:
            reader = InMemoryMetricReader()
            metrics_mod.init_metrics(reader=reader)
            tracker = InteractionTracker(
                detectors=(MaxTurnsDetector(max_turns=2),),
            )
            tracker.add_turn("dm:a:b", now=100.0)
            tracker.add_turn("dm:a:b", now=110.0)
            assert (
                counter_total(reader, "agent.interactions.closed.by_max_turns")
                == 1
            )
            assert counter_total(reader, "agent.interactions.closed") == 1
        finally:
            import asyncio

            if metrics_mod._provider is not None:
                asyncio.run(metrics_mod.shutdown())
            metrics_mod._provider = saved_provider
            metrics_mod._instruments = saved_instruments

    def test_no_inline_cap_when_max_turns_detector_absent(self):
        # If a caller installs a custom detector chain without
        # MaxTurnsDetector, add_turn must not invent a cap.  Pins the
        # contract that the inline check is sourced from the chain,
        # not from a hardcoded constant.
        tracker = InteractionTracker(detectors=(IdleGapDetector(),))
        for i in range(5):
            tracker.add_turn("dm:a:b", now=100.0 + i)
        interaction = tracker.get("dm:a:b")
        assert interaction is not None
        assert interaction.is_open
        assert interaction.turn_count == 5


# ─── Metric counter wiring ──────────────────────────────────


class TestMetricEmission:
    """Verify the tracker emits the RFC 0020 §Phase 1 counters.

    Uses an :class:`InMemoryMetricReader` per the pattern in
    ``test_observability_metrics.py`` so the assertions stay
    decoupled from the OTLP exporter.

    PR-1 review finding #5: the class mutates the module-global metrics
    registry via :func:`init_metrics`.  Without an autouse cleanup
    fixture each test method would inherit whatever state the previous
    test method (or the previous test class in the same pytest session)
    left behind, producing order-coupled failures whose root cause is
    invisible at the assertion site.  The class-scoped autouse fixture
    below snapshots the relevant module globals before every test and
    restores them after, using the public :func:`metrics_mod.shutdown`
    contract for the active provider so SDK background threads do not
    leak across tests either.
    """

    @pytest.fixture(autouse=True)
    def _reset_metrics_state(self):
        # Snapshot the module globals before each test.  The cleanup
        # fixture restores them after, regardless of how the test
        # mutates state.  This isolates this class from sibling test
        # classes in the same pytest session that may also call
        # ``init_metrics`` or assume ``try_get_instruments() is None``.
        import asyncio

        from agents.observability import metrics as metrics_mod

        saved_provider = metrics_mod._provider
        saved_instruments = metrics_mod._instruments
        # Force a known-clean baseline so each test method starts from
        # ``_provider is None`` / ``_instruments is None``.  Tests that
        # need a meter call ``_build_meter`` explicitly.
        metrics_mod._provider = None
        metrics_mod._instruments = None
        try:
            yield
        finally:
            # Tear down whatever the test installed via the public
            # contract — this releases the SDK's background threads —
            # then restore the pre-test snapshot so neighbouring test
            # classes see the same state they had before.
            if metrics_mod._provider is not None:
                asyncio.run(metrics_mod.shutdown())
            metrics_mod._provider = saved_provider
            metrics_mod._instruments = saved_instruments

    def test_autouse_fixture_clears_state_before_each_test(self):
        # Sentinel for finding #5: the autouse fixture must zero the
        # module globals before this test sees them.  Without the
        # fixture, the prior method's ``_build_meter()`` call would
        # have left ``_instruments`` set and this assertion would fail.
        from agents.observability import metrics as metrics_mod

        assert metrics_mod._provider is None
        assert metrics_mod._instruments is None

    def _build_meter(self):
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        from agents.observability import metrics as metrics_mod

        reader = InMemoryMetricReader()
        metrics_mod.init_metrics(reader=reader)
        return reader, metrics_mod

    def test_open_emits_opened_counter(self):
        reader, _ = self._build_meter()
        tracker = InteractionTracker()
        tracker.add_turn("tick", now=100.0)
        assert counter_total(reader, "agent.interactions.opened") == 1

    def test_close_emits_closed_and_by_structural(self):
        reader, _ = self._build_meter()
        tracker = InteractionTracker()
        opened = tracker.add_turn("tick", now=100.0)
        tracker.close_record(opened, reason=REASON_STRUCTURAL, now=200.0)
        assert counter_total(reader, "agent.interactions.closed") == 1
        assert (
            counter_total(reader, "agent.interactions.closed.by_structural")
            == 1
        )

    def test_idle_close_emits_by_idle_gap(self):
        reader, _ = self._build_meter()
        tracker = InteractionTracker(idle_timeout_sec=10.0)
        tracker.add_turn("tick", now=100.0)
        tracker.idle_check(now=200.0)
        assert (
            counter_total(reader, "agent.interactions.closed.by_idle_gap")
            == 1
        )

    @pytest.mark.parametrize(
        ("reason", "subtotal_metric"),
        [
            (REASON_STRUCTURAL, "agent.interactions.closed.by_structural"),
            (REASON_IDLE_GAP, "agent.interactions.closed.by_idle_gap"),
            (REASON_MAX_TURNS, "agent.interactions.closed.by_max_turns"),
            (REASON_TOPIC_SHIFT, "agent.interactions.closed.by_topic_shift"),
            (REASON_SHUTDOWN, "agent.interactions.closed.by_shutdown"),
            (REASON_COST, "agent.interactions.closed.by_cost"),
            (
                REASON_CATCHUP_COMPLETE,
                "agent.interactions.closed.by_catchup_complete",
            ),
        ],
    )
    def test_close_emits_per_reason_subtotal(self, reason, subtotal_metric):
        # PR 6 slice 2 #3: every REASON_* defined in
        # ``boundary_detectors`` has a paired
        # ``agent.interactions.closed.by_<reason>`` counter, dispatched
        # from the same table that the boundary-detector chain consults.
        # Drift between the two surfaces (a new REASON_* with no
        # counter, as happened with ``max_turns`` / ``topic_shift`` /
        # ``shutdown`` between PR 1 and PR 6 slice 1) becomes a typing
        # / table-lookup miss instead of a silent telemetry hole.
        reader, _ = self._build_meter()
        tracker = InteractionTracker()
        opened = tracker.add_turn("tick", now=100.0)
        tracker.close_record(opened, reason=reason, now=200.0)
        assert counter_total(reader, "agent.interactions.closed") == 1
        assert counter_total(reader, subtotal_metric) == 1

    def test_no_metric_emission_when_uninitialised(self):
        # Before init_metrics(), tracker calls must not raise — the
        # try_get_instruments() call returns None and emission no-ops.
        #
        # The autouse ``_reset_metrics_state`` fixture establishes the
        # uninitialised baseline (see class docstring); this test's
        # body simply pins the no-op contract.  An earlier revision
        # called ``asyncio.run(metrics_mod.shutdown())`` inline to
        # undo sibling tests' side-effects, which pytest's
        # ``test_observability_metrics.py`` review explicitly flagged
        # as fragile (private-global shape, no-fixture, mid-test event
        # loop).  The fixture now owns that contract.
        from agents.observability import metrics as metrics_mod

        assert metrics_mod.try_get_instruments() is None

        tracker = InteractionTracker()
        opened = tracker.add_turn("tick", now=100.0)
        tracker.close_record(opened, reason=REASON_STRUCTURAL, now=200.0)
        # No exception is the assertion; no counter to read.


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
