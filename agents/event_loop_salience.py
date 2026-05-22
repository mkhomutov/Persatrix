"""RFC 0024 PR 3b — ``MemoryWriteBus`` subscriber that enqueues ``SalienceWake``.

Split out of :mod:`agents.event_loop` for file-size review-friendliness
(mirrors the :mod:`agents.event_loop_timers` split landed in PR 2.1).

The :class:`_SalienceSubscriber` is the bridge from
:class:`agents.memory._events.MemoryWriteBus` (RFC 0024 PR 3a's
write-side fan-out surface) to :meth:`agents.event_loop.EventLoop.enqueue`.
It is installed at :meth:`EventLoop.start` and removed at
:meth:`EventLoop.stop`; the bus's in-process synchronous fan-out means
the subscriber runs in the call stack of the memory write itself, which
is exactly what the loop-back guard depends on
([RFC 0024 §F failure mode row 3](../docs/rfcs/0024-event-driven-scheduling.md#f-failure-modes)).

The subscriber owns four pieces of suppression policy:

1. **Cross-agent filter.** Only writes whose ``agent_id`` matches this
   subscriber's owning :class:`EventLoop` are considered.  Writes for
   other agents short-circuit *before* the suppression-decision tree so
   they do not pollute the per-agent counter cardinality.  Matches the
   ``_global_bus`` fan-out comment in :mod:`agents.memory._events`.
2. **Threshold.** Strict ``>`` per RFC §D — a salience of exactly the
   threshold value is suppressed.  This is the inequality that keeps
   PR 3a's conservative ``REFLECTION_CONTRADICTION_SALIENCE = 0.6`` off
   under PR 3b's default threshold ``0.95``; the inequality is asserted
   from both sides in
   :mod:`agents.tests.test_event_loop_salience_default_off`.
3. **Loop-back guard.** A write whose captured ``source_span_id``
   matches the current OTEL span at publish time is suppressed.  This
   is the v0.2.1 cost-leak in a new costume: a memory write inside an
   LLM response that triggers a wake that triggers another LLM response
   is the same unbounded path the polling loop opened.  PR 3a captures
   ``source_span_id`` synchronously at the write site via
   :func:`agents.observability.spans.current_llm_span_id`; PR 3b's
   subscriber re-reads at the publish site (still synchronous on the
   same call stack) — equality means same-span re-entry, suppress.
   The explicit ``is not None`` guard prevents the
   ``None == None`` vacuous match for background-task writes with no
   captured span.

   The guard matches by span **id** alone: :func:`current_llm_span_id`
   reads whatever span is active, *not* only ``agent.llm.call`` spans
   (the "llm" in the name reflects the load-bearing caller, not a
   filter on span name or kind).  Combined with the synchronous
   capture-and-publish, the practical contract is broader than "inside
   an LLM call" — *any* write that fires inside an active span is
   suppressed; only writes with no active span at capture time
   (``source_span_id is None``) can ever enqueue a wake.  This errs
   cost-safe (over-suppression cannot reintroduce the v0.2.1 leak);
   whether to narrow it to LLM-call spans specifically is a
   calibration-PR decision deferred until the v0.4.0+ consumer lands
   (RFC 0024 PR plan §"From PR 3b review").  Pinned span-agnostic by
   ``test_loopback_suppresses_for_any_active_span_not_only_llm``.
4. **Rate-limit.** A sliding 1-second window with a configurable cap
   (default ``10`` per RFC §Security Considerations) keeps a malicious
   or buggy write storm from DoS-ing the agent.  ``time_fn`` is injected
   so tests freeze the clock — production callers leave it as
   :func:`time.monotonic`.

Every same-agent write records *exactly one* data point on
``agent.wake.salience`` with a ``suppressed_reason`` attribute.  The
three-branch suppression tree yields ``below_threshold`` | ``loopback``
| ``rate_limit``; the admit branch then resolves at the substrate to
``none`` (enqueued) or ``queue_full`` (admitted by salience policy but
the ``EventLoop`` queue was full — also counted on ``agent.wake.dropped``).
Five reason values cover every outcome; dashboards can attribute every
``MemoryWriteEvent`` to exactly one of them, and ``none`` alone is the
true-enqueue count.
"""

from __future__ import annotations

import collections
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from .event_loop_types import SalienceWake
from .memory._events import MemoryWriteEvent
from .observability._metrics_wakes import wake_attrs
from .observability.metrics import try_get_instruments
from .observability.spans import current_llm_span_id

if TYPE_CHECKING:
    from .event_loop_types import WakeEvent

logger = logging.getLogger(__name__)

__all__ = ["_SalienceSubscriber"]


# Reasons recorded on ``agent.wake.salience`` ``suppressed_reason``
# attribute.  Held as module-level constants so the test file and the
# dashboard documentation share a single source of truth.
_REASON_BELOW = "below_threshold"
_REASON_LOOPBACK = "loopback"
_REASON_RATE_LIMIT = "rate_limit"
_REASON_NONE = "none"
_REASON_QUEUE_FULL = "queue_full"


class _SalienceSubscriber:
    """``MemoryWriteBus`` subscriber owned by one :class:`EventLoop`.

    The subscriber is a plain ``Callable[[MemoryWriteEvent], None]`` so it
    plugs directly into :meth:`MemoryWriteBus.subscribe`; the bus's
    failure-isolation contract swallows any exception this raises, so the
    write path stays decoupled from a buggy subscriber.
    """

    __slots__ = (
        "_agent_id",
        "_enqueue",
        "_rate_max",
        "_recent",
        "_threshold",
        "_time_fn",
    )

    def __init__(
        self,
        *,
        agent_id: str,
        enqueue: Callable[[WakeEvent], bool],
        threshold: float,
        rate_max_per_sec: int,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._agent_id = agent_id
        self._enqueue = enqueue
        self._threshold = threshold
        self._rate_max = rate_max_per_sec
        self._time_fn = time_fn
        self._recent: collections.deque[float] = collections.deque()

    def __call__(self, event: MemoryWriteEvent) -> None:
        # Cross-agent filter short-circuits before the decision tree so
        # other personas' writes do not inflate this agent's counter.
        if event.agent_id != self._agent_id:
            return
        reason = self._decide(event)
        self._record(tier=event.tier, reason=reason)

    def _decide(self, event: MemoryWriteEvent) -> str:
        # Strict ``>`` keeps a salience of exactly the threshold off — see
        # the default-off inequality in
        # :mod:`agents.tests.test_event_loop_salience_default_off`.
        if not (event.salience > self._threshold):
            return _REASON_BELOW

        # Loop-back guard: the write's captured span vs the span active
        # in this call stack right now.  Matches by span id alone —
        # ``current_llm_span_id()`` reads whatever span is active, not
        # only LLM-call spans, so any write that fires inside an active
        # span is suppressed (cost-safe over-suppression; see module
        # docstring §3).  Both ``None`` is the background-task case and
        # must NOT match (it would suppress every legitimate span-less
        # write).
        if event.source_span_id is not None:
            active = current_llm_span_id()
            if active is not None and event.source_span_id == active:
                return _REASON_LOOPBACK

        # Rate-limit: sliding 1s window per agent.  The window counts
        # enqueue *attempts* (this slot is consumed even if the enqueue
        # below is rejected by a full queue) — intentional, so the cap
        # bounds the DoS-relevant attempt rate, not just successes.
        # The check returns before the enqueue, so the suppression reasons
        # take precedence over the substrate outcome: a write that would
        # both exceed the cap and hit a full queue records ``rate_limit``,
        # never ``queue_full`` (``queue_full`` is reachable only for a
        # write that passed threshold, loop-back, and the rate cap).
        now = self._time_fn()
        cutoff = now - 1.0
        while self._recent and self._recent[0] < cutoff:
            self._recent.popleft()
        if len(self._recent) >= self._rate_max:
            return _REASON_RATE_LIMIT
        self._recent.append(now)

        accepted = self._enqueue(SalienceWake(write_event=event))
        if not accepted:
            # Salience policy admitted this write, but the substrate queue
            # was full so it never enqueued.  Record ``queue_full`` (not
            # ``none``) so the salience-side reason agrees with the
            # ``agent.wake.dropped`` counter the enqueue call-site bumps —
            # ``none`` is reserved for writes that actually enqueued, so a
            # dashboard does not over-count true enqueues by the number of
            # queue-full drops.
            return _REASON_QUEUE_FULL
        return _REASON_NONE

    def _record(self, *, tier: str, reason: str) -> None:
        inst = try_get_instruments()
        if inst is None:
            # Metrics not initialised — typical in early-startup test
            # fixtures.  No-op rather than raise so a misconfigured
            # caller does not break the write path's failure-isolation
            # contract.
            return
        inst.wake_salience.add(
            1,
            attributes=wake_attrs(
                agent_id=self._agent_id,
                wake_kind="salience",
                tier=tier,
                suppressed_reason=reason,
            ),
        )
