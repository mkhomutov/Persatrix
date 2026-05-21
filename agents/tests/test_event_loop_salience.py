"""RFC 0024 PR 3b — SalienceWake enqueue, threshold, loop-back guard, rate-limit.

Pins the four branches of the salience subscriber's enqueue decision tree:

* **Above threshold + no loopback + under rate cap** → enqueue ``SalienceWake``
  and record ``agent.wake.salience{suppressed_reason="none"}``.
* **At or below threshold** → suppress; record ``suppressed_reason="below_threshold"``.
  Strict ``>`` per RFC §D so the PR 3a maximum (``REFLECTION_CONTRADICTION_SALIENCE
  = 0.6``) at the PR 3b threshold default (``0.95``) stays off by inequality.
* **``source_span_id`` matches the active span on the same agent** → suppress;
  record ``suppressed_reason="loopback"``.  This is the v0.2.1 cost-leak in a
  new costume per RFC §F row 3 — a memory write that fires inside an LLM
  response must not enqueue another wake that triggers another LLM response.
* **Per-agent rate above ``autonomy.salience_rate_max_per_sec``** → suppress;
  record ``suppressed_reason="rate_limit"``.  Default cap is 10/sec per
  RFC §Security Considerations.

Cross-agent filtering (the ``_global_bus`` fan-out → per-agent
subscriber-side filter recorded in :mod:`agents.memory._events`' module
docstring) is also pinned here: a write with a different ``agent_id`` does
not even reach the suppression-decision tree.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider

from agents.event_loop import EventLoop, ScheduledWake
from agents.memory._events import (
    MemoryWriteBus,
    MemoryWriteEvent,
    get_memory_write_bus,
    set_memory_write_bus,
)
from agents.observability import metrics as pmetrics
from agents.persona_types import ActionType, AgentAction, AgentEvent

# ─── Fixtures ───────────────────────────────────────────────────────────────


_AGENT_ID = "test-persona"
_OTHER_AGENT_ID = "another-persona"


@pytest.fixture(autouse=True)
def _real_tracer_provider() -> None:
    """Install an SDK ``TracerProvider`` so spans report valid span_ids."""
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())


@pytest.fixture
def fresh_bus() -> Iterator[MemoryWriteBus]:
    """Install a fresh global ``MemoryWriteBus`` per test (no cross-test bleed)."""
    original = get_memory_write_bus()
    bus = MemoryWriteBus()
    set_memory_write_bus(bus)
    try:
        yield bus
    finally:
        set_memory_write_bus(original)


@pytest.fixture
def metric_reader() -> Iterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    pmetrics.init_metrics(reader=reader)
    try:
        yield reader
    finally:
        asyncio.run(pmetrics.shutdown())


def _salience_points_by_reason(
    reader: InMemoryMetricReader,
) -> dict[str, int]:
    """Return ``{suppressed_reason: total}`` for ``agent.wake.salience`` points."""
    data = reader.get_metrics_data()
    out: dict[str, int] = {}
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name != "agent.wake.salience":
                    continue
                for dp in m.data.data_points:  # type: ignore[union-attr]
                    raw = (dp.attributes or {}).get("suppressed_reason")
                    reason = str(raw) if raw is not None else "<missing>"
                    out[reason] = out.get(reason, 0) + int(getattr(dp, "value", 0))
    return out


async def _default_on_event(_event: AgentEvent) -> list[AgentAction]:
    return [AgentAction(ActionType.DO_NOTHING, {})]


async def _default_on_tick(_wake: ScheduledWake) -> None:
    return None


@pytest.fixture
async def loop_above_threshold() -> AsyncIterator[EventLoop]:
    """EventLoop with threshold 0.5 + rate cap 100 + subscribed to the bus."""
    loop = EventLoop(
        agent_id=_AGENT_ID,
        on_event=_default_on_event,
        on_tick=_default_on_tick,
        salience_threshold=0.5,
        salience_rate_max_per_sec=100,
    )
    loop.start()
    try:
        yield loop
    finally:
        await loop.stop(timeout=1.0)


def _write_event(
    *,
    agent_id: str = _AGENT_ID,
    salience: float = 0.9,
    source_span_id: str | None = None,
    tier: str = "episodic",
) -> MemoryWriteEvent:
    return MemoryWriteEvent(
        agent_id=agent_id,
        tier=tier,  # type: ignore[arg-type]
        salience=salience,
        source_span_id=source_span_id,
        written_at=time.time(),
    )


# ─── Threshold enforcement ──────────────────────────────────────────────────


class TestThreshold:
    async def test_above_threshold_enqueues_salience_wake(
        self,
        fresh_bus: MemoryWriteBus,
        loop_above_threshold: EventLoop,
        metric_reader: InMemoryMetricReader,
    ) -> None:
        """Salience strictly above threshold enqueues a ``SalienceWake``."""
        # The not-suppressed branch records ``suppressed_reason="none"``;
        # the supervisor's draining is observable on the counter so we do
        # not need to monkey-patch the wake dispatch path.
        fresh_bus.publish(_write_event(salience=0.9))
        for _ in range(50):
            if _salience_points_by_reason(metric_reader).get("none", 0) >= 1:
                break
            await asyncio.sleep(0.01)
        assert _salience_points_by_reason(metric_reader).get("none", 0) == 1

    async def test_at_threshold_does_not_enqueue(
        self,
        fresh_bus: MemoryWriteBus,
        loop_above_threshold: EventLoop,
        metric_reader: InMemoryMetricReader,
    ) -> None:
        """Equality is suppression — strict ``>`` per RFC §D."""
        fresh_bus.publish(_write_event(salience=0.5))
        # Subscriber dispatch is synchronous from publish(); counter records
        # immediately.
        reasons = _salience_points_by_reason(metric_reader)
        assert reasons.get("below_threshold", 0) == 1
        assert reasons.get("none", 0) == 0

    async def test_below_threshold_records_suppressed_reason(
        self,
        fresh_bus: MemoryWriteBus,
        loop_above_threshold: EventLoop,
        metric_reader: InMemoryMetricReader,
    ) -> None:
        """Below threshold increments ``suppressed_reason="below_threshold"``."""
        fresh_bus.publish(_write_event(salience=0.1))
        reasons = _salience_points_by_reason(metric_reader)
        assert reasons.get("below_threshold", 0) == 1

    async def test_above_threshold_records_suppressed_reason_none(
        self,
        fresh_bus: MemoryWriteBus,
        loop_above_threshold: EventLoop,
        metric_reader: InMemoryMetricReader,
    ) -> None:
        """The not-suppressed branch increments with ``suppressed_reason="none"``."""
        fresh_bus.publish(_write_event(salience=0.6))
        for _ in range(50):
            if _salience_points_by_reason(metric_reader).get("none", 0) >= 1:
                break
            await asyncio.sleep(0.01)
        reasons = _salience_points_by_reason(metric_reader)
        assert reasons.get("none", 0) == 1


# ─── Loop-back guard (RFC §F row 3) ─────────────────────────────────────────


class TestLoopbackGuard:
    async def test_loopback_suppresses_when_source_span_matches_active(
        self,
        fresh_bus: MemoryWriteBus,
        loop_above_threshold: EventLoop,
        metric_reader: InMemoryMetricReader,
    ) -> None:
        """A write that originated inside the agent's active span must not enqueue."""
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("agent.llm.call") as span:
            sid_hex = f"{span.get_span_context().span_id:016x}"
            fresh_bus.publish(
                _write_event(salience=0.9, source_span_id=sid_hex),
            )
        reasons = _salience_points_by_reason(metric_reader)
        assert reasons.get("loopback", 0) == 1
        assert reasons.get("none", 0) == 0

    async def test_loopback_does_not_suppress_when_no_active_span(
        self,
        fresh_bus: MemoryWriteBus,
        loop_above_threshold: EventLoop,
        metric_reader: InMemoryMetricReader,
    ) -> None:
        """A write with a captured span_id but no active span at publish time fires."""
        # The write captured some past span; by publish time no span is
        # active. ``current_llm_span_id()`` returns ``None`` so the guard
        # cannot match — the wake enqueues.
        fresh_bus.publish(
            _write_event(salience=0.9, source_span_id="abcd1234abcd1234"),
        )
        for _ in range(50):
            if _salience_points_by_reason(metric_reader).get("none", 0) >= 1:
                break
            await asyncio.sleep(0.01)
        reasons = _salience_points_by_reason(metric_reader)
        assert reasons.get("none", 0) == 1
        assert reasons.get("loopback", 0) == 0

    async def test_loopback_does_not_suppress_when_source_is_none(
        self,
        fresh_bus: MemoryWriteBus,
        loop_above_threshold: EventLoop,
        metric_reader: InMemoryMetricReader,
    ) -> None:
        """``source_span_id is None`` matches active=None vacuously — must NOT loopback.

        Without the explicit ``is not None`` guard, a background-task write
        with no captured span and a runtime with no active span would equate
        as ``None == None`` and every such write would be suppressed.  This
        test is the regression backstop.
        """
        tracer = trace.get_tracer("test")
        # No active span at publish time either.
        del tracer
        fresh_bus.publish(_write_event(salience=0.9, source_span_id=None))
        for _ in range(50):
            if _salience_points_by_reason(metric_reader).get("none", 0) >= 1:
                break
            await asyncio.sleep(0.01)
        reasons = _salience_points_by_reason(metric_reader)
        assert reasons.get("loopback", 0) == 0
        assert reasons.get("none", 0) == 1


# ─── Rate-limit (RFC §Security Considerations) ──────────────────────────────


class TestRateLimit:
    async def test_rate_limit_caps_per_second(
        self,
        fresh_bus: MemoryWriteBus,
        metric_reader: InMemoryMetricReader,
    ) -> None:
        """The configured cap is the maximum enqueues per rolling second."""
        # Frozen clock so all 20 publishes share the same window.
        clock = [1000.0]

        def _now() -> float:
            return clock[0]

        loop = EventLoop(
            agent_id=_AGENT_ID,
            on_event=_default_on_event,
            on_tick=_default_on_tick,
            salience_threshold=0.5,
            salience_rate_max_per_sec=3,
            queue_size=64,
            salience_time_fn=_now,
        )
        loop.start()
        try:
            for _ in range(20):
                fresh_bus.publish(_write_event(salience=0.9))
            # Give the supervisor time to drain the queue (the three enqueued
            # wakes increment ``suppressed_reason=none`` via the supervisor
            # path).
            for _ in range(50):
                if _salience_points_by_reason(metric_reader).get("none", 0) >= 3:
                    break
                await asyncio.sleep(0.01)
        finally:
            await loop.stop(timeout=1.0)

        reasons = _salience_points_by_reason(metric_reader)
        assert reasons.get("none", 0) == 3
        assert reasons.get("rate_limit", 0) == 17

    async def test_rate_limit_window_slides(
        self,
        fresh_bus: MemoryWriteBus,
        metric_reader: InMemoryMetricReader,
    ) -> None:
        """After the rolling 1s window advances, the cap resets."""
        clock = [1000.0]

        def _now() -> float:
            return clock[0]

        loop = EventLoop(
            agent_id=_AGENT_ID,
            on_event=_default_on_event,
            on_tick=_default_on_tick,
            salience_threshold=0.5,
            salience_rate_max_per_sec=2,
            queue_size=64,
            salience_time_fn=_now,
        )
        loop.start()
        try:
            # Two fires at t=1000 (under the cap).
            fresh_bus.publish(_write_event(salience=0.9))
            fresh_bus.publish(_write_event(salience=0.9))
            # Third fire at t=1000 should hit the cap.
            fresh_bus.publish(_write_event(salience=0.9))
            # Advance the clock past the window — the next fire is allowed.
            clock[0] = 1002.0
            fresh_bus.publish(_write_event(salience=0.9))
            for _ in range(50):
                if _salience_points_by_reason(metric_reader).get("none", 0) >= 3:
                    break
                await asyncio.sleep(0.01)
        finally:
            await loop.stop(timeout=1.0)

        reasons = _salience_points_by_reason(metric_reader)
        assert reasons.get("none", 0) == 3
        assert reasons.get("rate_limit", 0) == 1


# ─── Cross-agent filtering (per-agent subscriber routing) ───────────────────


class TestCrossAgentFiltering:
    async def test_writes_from_other_agent_do_not_enqueue(
        self,
        fresh_bus: MemoryWriteBus,
        loop_above_threshold: EventLoop,
        metric_reader: InMemoryMetricReader,
    ) -> None:
        """Per the ``_global_bus`` cross-agent fan-out comment, the subscriber
        filters by ``agent_id`` before recording — no counter increment, no
        wake."""
        fresh_bus.publish(_write_event(agent_id=_OTHER_AGENT_ID, salience=0.9))
        # No counter increment at all — the cross-agent guard short-circuits
        # before the suppression-decision tree.
        reasons = _salience_points_by_reason(metric_reader)
        assert reasons == {}


# ─── Subscription lifecycle ─────────────────────────────────────────────────


class TestSubscriptionLifecycle:
    async def test_subscriber_unsubscribes_on_stop(
        self,
        fresh_bus: MemoryWriteBus,
        metric_reader: InMemoryMetricReader,
    ) -> None:
        """After ``stop()`` the bus no longer routes events to this loop's subscriber."""
        loop = EventLoop(
            agent_id=_AGENT_ID,
            on_event=_default_on_event,
            on_tick=_default_on_tick,
            salience_threshold=0.5,
            salience_rate_max_per_sec=100,
        )
        loop.start()
        # Subscribed: a write records on the counter.
        fresh_bus.publish(_write_event(salience=0.9))
        for _ in range(50):
            if _salience_points_by_reason(metric_reader).get("none", 0) >= 1:
                break
            await asyncio.sleep(0.01)
        assert _salience_points_by_reason(metric_reader).get("none", 0) == 1

        await loop.stop(timeout=1.0)
        # After stop, the subscriber must be gone — a further publish must
        # NOT increment the counter.
        fresh_bus.publish(_write_event(salience=0.9))
        # Give the (now-stopped) supervisor a small grace period; no record
        # should appear.
        await asyncio.sleep(0.05)
        assert _salience_points_by_reason(metric_reader).get("none", 0) == 1

    async def test_no_subscription_until_start(
        self,
        fresh_bus: MemoryWriteBus,
        metric_reader: InMemoryMetricReader,
    ) -> None:
        """A constructed-but-not-started ``EventLoop`` does not subscribe."""
        loop = EventLoop(
            agent_id=_AGENT_ID,
            on_event=_default_on_event,
            on_tick=_default_on_tick,
            salience_threshold=0.5,
            salience_rate_max_per_sec=100,
        )
        # Do not start().
        fresh_bus.publish(_write_event(salience=0.9))
        reasons = _salience_points_by_reason(metric_reader)
        assert reasons == {}
        # Cleanup — stop() on a never-started loop is documented as a no-op.
        await loop.stop(timeout=0.1)
