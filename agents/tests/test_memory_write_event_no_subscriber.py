"""RFC 0024 PR 3a — ``MemoryWriteBus`` fan-out contract with no subscribers.

The bus is the new pub/sub surface that PR 3a ships *empty*.  No production
subscriber exists yet (PR 3b adds the ``EventLoop`` subscriber); these tests
pin the load-bearing invariant that PR 3a's write-site emissions cannot
regress steady-state memory-write performance just by emitting into the
void.

If a future change accidentally turned the bus into a buffering queue, the
``no-retention`` test would catch it — a late subscriber would suddenly see
the historical event and PR 3b's wake-enqueue path would fire for backlog.
"""

from __future__ import annotations

import math
import time

from agents.memory._events import MemoryWriteBus, MemoryWriteEvent


def _make_event(*, tier: str = "episodic", salience: float = 0.0) -> MemoryWriteEvent:
    return MemoryWriteEvent(
        agent_id="agent-x",
        tier=tier,  # type: ignore[arg-type]
        salience=salience,
        source_span_id=None,
        written_at=time.time(),
    )


class TestPublishWithNoSubscriber:
    def test_publish_without_subscriber_is_noop(self) -> None:
        bus = MemoryWriteBus()
        # Must not raise; no subscribers means nothing happens.
        bus.publish(_make_event())

    def test_late_subscriber_sees_no_historical_events(self) -> None:
        """The bus is fan-out, not a queue — past events are not retained."""
        bus = MemoryWriteBus()
        bus.publish(_make_event(tier="notes"))
        bus.publish(_make_event(tier="facts"))

        seen: list[MemoryWriteEvent] = []
        bus.subscribe(seen.append)

        assert seen == [], (
            "Late subscriber must not receive events published before subscribe()"
        )


class TestSubscriberLifecycle:
    def test_subscriber_receives_only_post_subscription_events(self) -> None:
        bus = MemoryWriteBus()
        seen: list[MemoryWriteEvent] = []
        bus.subscribe(seen.append)

        ev = _make_event(tier="relationship")
        bus.publish(ev)

        assert seen == [ev]

    def test_unsubscribe_stops_delivery(self) -> None:
        bus = MemoryWriteBus()
        seen: list[MemoryWriteEvent] = []
        bus.subscribe(seen.append)
        bus.publish(_make_event())
        bus.unsubscribe(seen.append)
        bus.publish(_make_event(tier="notes"))

        assert len(seen) == 1, "After unsubscribe, no more events should arrive"

    def test_subscriber_exception_does_not_break_bus(self) -> None:
        """A buggy subscriber must not break the write path."""
        bus = MemoryWriteBus()

        def boom(_event: MemoryWriteEvent) -> None:
            raise RuntimeError("subscriber bug")

        seen: list[MemoryWriteEvent] = []
        bus.subscribe(boom)
        bus.subscribe(seen.append)

        # Must not raise — write-path safety is the load-bearing contract.
        bus.publish(_make_event())

        assert len(seen) == 1, "Healthy subscriber should still receive the event"


class TestDataclassDefensiveClipping:
    def test_salience_clipped_high(self) -> None:
        ev = MemoryWriteEvent(
            agent_id="a", tier="episodic", salience=1.5,
            source_span_id=None, written_at=0.0,
        )
        assert ev.salience == 1.0

    def test_salience_clipped_low(self) -> None:
        ev = MemoryWriteEvent(
            agent_id="a", tier="episodic", salience=-0.3,
            source_span_id=None, written_at=0.0,
        )
        assert ev.salience == 0.0

    def test_salience_in_range_unchanged(self) -> None:
        ev = MemoryWriteEvent(
            agent_id="a", tier="reflection", salience=0.6,
            source_span_id=None, written_at=0.0,
        )
        assert ev.salience == 0.6

    def test_salience_nan_clipped_to_zero(self) -> None:
        # NaN is the one value ``max(0.0, min(1.0, x))`` does NOT clip
        # (every comparison with NaN is False).  Defence-in-depth: PR 3b's
        # subscriber will compare salience to a threshold — a NaN slipping
        # through would silently fail every comparison and route the
        # event through the "below threshold" branch by accident.  Clip
        # to 0.0 explicitly so the contract is "every constructed event
        # carries a salience in [0.0, 1.0]".
        ev = MemoryWriteEvent(
            agent_id="a", tier="episodic", salience=float("nan"),
            source_span_id=None, written_at=0.0,
        )
        assert ev.salience == 0.0
        assert math.isfinite(ev.salience)

    def test_salience_positive_infinity_clipped_to_one(self) -> None:
        # ``min(1.0, +inf)`` already returns 1.0 so this passes today —
        # pinned alongside the NaN case so the "every event in [0.0, 1.0]"
        # contract holds for the full IEEE-754 corner-set.
        ev = MemoryWriteEvent(
            agent_id="a", tier="episodic", salience=float("inf"),
            source_span_id=None, written_at=0.0,
        )
        assert ev.salience == 1.0

    def test_salience_negative_infinity_clipped_to_zero(self) -> None:
        ev = MemoryWriteEvent(
            agent_id="a", tier="episodic", salience=float("-inf"),
            source_span_id=None, written_at=0.0,
        )
        assert ev.salience == 0.0
