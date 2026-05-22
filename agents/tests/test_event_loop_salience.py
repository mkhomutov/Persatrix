"""RFC 0024 PR 3b — SalienceWake enqueue, threshold, loop-back guard, rate-limit.

Pins the salience subscriber's enqueue decision tree — three suppression
branches plus the admit branch's two substrate outcomes:

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
* **Admitted but the substrate queue is full** → record
  ``suppressed_reason="queue_full"`` (not ``none``), so the salience-side
  outcome agrees with the ``agent.wake.dropped`` counter.

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
async def loop_above_threshold(fresh_bus: MemoryWriteBus) -> AsyncIterator[EventLoop]:
    """EventLoop with threshold 0.5 + rate cap 100 + subscribed to the bus.

    Depends on ``fresh_bus`` explicitly so the per-test bus is installed as
    the process global *before* ``loop.start()`` subscribes — otherwise the
    loop would subscribe to whichever bus the global happened to hold,
    leaving correctness hostage to fixture parameter ordering.
    """
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
        # The admit branch records ``suppressed_reason="none"`` synchronously
        # inside the subscriber during ``publish()``: ``enqueue`` is a
        # non-blocking ``put_nowait``, and ``_record`` runs in the same
        # frame — the counter is already set when ``publish()`` returns, so
        # no wait on the supervisor drain path is needed (matches the
        # suppression-branch tests above).
        fresh_bus.publish(_write_event(salience=0.9))
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
        # Recorded synchronously during ``publish()`` — see
        # ``test_above_threshold_enqueues_salience_wake``.
        fresh_bus.publish(_write_event(salience=0.6))
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

    async def test_loopback_suppresses_for_any_active_span_not_only_llm(
        self,
        fresh_bus: MemoryWriteBus,
        loop_above_threshold: EventLoop,
        metric_reader: InMemoryMetricReader,
    ) -> None:
        """The guard matches by span *id* alone — span name/kind is irrelevant.

        ``current_llm_span_id()`` reads whatever span is active, not only
        ``agent.llm.call`` spans (its name reflects the load-bearing caller,
        not a filter). Because every write site captures ``source_span_id``
        and publishes synchronously in the same frame, the practical contract
        is broader than "inside an LLM call": *any* write that fires inside an
        active span is suppressed as ``loopback``; only span-less writes
        (``source_span_id is None``) can enqueue a wake. This test pins a
        deliberately non-LLM span name so a future change that narrows the
        guard to LLM-call spans (a calibration-PR decision — see
        ``agents.event_loop_salience`` module docstring §3) trips here rather
        than silently flipping the trigger population.
        """
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("agent.persona.tick") as span:
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
        # cannot match — the wake enqueues (recorded synchronously).
        fresh_bus.publish(
            _write_event(salience=0.9, source_span_id="abcd1234abcd1234"),
        )
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
            # Each publish records its outcome synchronously in the
            # subscriber: the first 3 admit (``none``), the remaining 17 hit
            # the frozen-window cap (``rate_limit``).  No supervisor-drain
            # wait is involved — the counter is complete once the publish
            # loop returns.
            for _ in range(20):
                fresh_bus.publish(_write_event(salience=0.9))
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
        finally:
            await loop.stop(timeout=1.0)

        # Outcomes recorded synchronously per publish: t=1000 admits 2 then
        # caps 1; t=1002 admits 1 after the window slides → none=3, rate=1.
        reasons = _salience_points_by_reason(metric_reader)
        assert reasons.get("none", 0) == 3
        assert reasons.get("rate_limit", 0) == 1


# ─── Queue-full outcome (substrate rejection of a salience-admitted write) ───


class TestQueueFullOutcome:
    def test_queue_full_records_distinct_reason_not_none(
        self,
        metric_reader: InMemoryMetricReader,
    ) -> None:
        """A salience-admitted write the substrate queue rejects records
        ``suppressed_reason="queue_full"`` — never ``none``.

        ``none`` is reserved for writes that actually enqueued; a dashboard
        reading ``none`` as the true-enqueue count would otherwise over-count
        by the number of queue-full drops. The drop is *also* recorded on
        ``agent.wake.dropped`` at the ``EventLoop.enqueue`` call site, so the
        two counters now agree that the write did not enqueue.

        Constructed against the subscriber directly with a stub ``enqueue``
        that returns ``False``: filling a real ``asyncio.Queue`` to capacity
        is racy because the ``SalienceWake`` drain path is near-instant, so a
        focused unit test of the decision branch is the deterministic seam.
        """
        from agents.event_loop_salience import _SalienceSubscriber

        subscriber = _SalienceSubscriber(
            agent_id=_AGENT_ID,
            enqueue=lambda _wake: False,  # substrate queue full
            threshold=0.5,
            rate_max_per_sec=100,
        )
        subscriber(_write_event(salience=0.9, source_span_id=None))
        reasons = _salience_points_by_reason(metric_reader)
        assert reasons.get("queue_full", 0) == 1
        assert reasons.get("none", 0) == 0


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
        # Subscribed: a write records on the counter synchronously.
        fresh_bus.publish(_write_event(salience=0.9))
        assert _salience_points_by_reason(metric_reader).get("none", 0) == 1

        # ``stop()`` unsubscribes synchronously (before its first await), so
        # once it returns a further publish reaches no subscriber and cannot
        # increment the counter.
        await loop.stop(timeout=1.0)
        fresh_bus.publish(_write_event(salience=0.9))
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
