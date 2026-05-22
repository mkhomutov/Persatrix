"""RFC 0024 Phase 3 — memory-write event bus (PR 3a substrate).

``MemoryWriteEvent`` is the write-side signal PR 3b's :class:`EventLoop`
will subscribe to in order to enqueue :class:`SalienceWake`.  PR 3a ships
the emission on every memory-tier write site; it ships **no subscriber**.

The bus is intentionally:

* **In-process** — a single per-process ``MemoryWriteBus`` singleton with
  ``set_memory_write_bus()`` / ``get_memory_write_bus()`` accessors so tests
  can install a fresh instance without monkeypatching call sites.  PR 3b
  subscribes the :class:`EventLoop` to the global bus at agent start.
* **Synchronous fan-out** — :meth:`MemoryWriteBus.publish` invokes each
  subscriber inline.  No asyncio queue, no buffering.  This guarantees that
  the no-subscriber baseline (PR 3a) cannot regress steady-state memory-
  write latency and that a late subscriber sees only post-subscribe events
  (no retention — pinned by
  :mod:`agents.tests.test_memory_write_event_no_subscriber`).
* **Failure-isolating** — a subscriber that raises does not break the
  write path.  Exceptions are logged at WARNING and swallowed; a buggy PR
  3b subscriber must not cascade into a write failure.
* **Single-threaded** — subscribe / unsubscribe / :func:`set_memory_write_bus`
  and :meth:`MemoryWriteBus.publish` mutate / iterate the subscriber list
  with **no lock**.  The invariant that makes this safe: PR 3b's
  :class:`agents.event_loop.EventLoop` subscribes once at start from the
  main asyncio loop's thread, and every memory-tier write site calls
  ``publish`` from that same loop thread (the writes are ``await`` ed on
  the event loop, so the synchronous fan-out runs on it too).  ``publish``
  already snapshots via ``tuple(self._subscribers)`` so a subscriber that
  (un)subscribes during dispatch cannot corrupt iteration.  A future
  background-thread subscriber — e.g. a Phase-4 channel-message receiver
  subscribing from outside the loop's thread — would break this invariant
  and require a ``threading.Lock`` around subscribe/unsubscribe (RFC 0024
  PR-plan deferred finding (3)).

Salience is clipped to ``[0.0, 1.0]`` defensively at :class:`MemoryWriteEvent`
construction time per RFC §Security Considerations.  Write sites should
still pass already-clipped values — the clip here is defence-in-depth, not
the primary validator.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)


# ─── Tier vocabulary ────────────────────────────────────────────────────────


# The five Persatrix memory tiers (RFC 0024 PR plan §PR 3a).  ``reflection``
# has no production write site at v0.3.3 — it is reserved for the future
# RFC 0027 reflection-driven consolidation work — but lives in the literal
# so PR 3b's subscriber's ``match`` over tiers is exhaustive from day one.
MemoryTier = Literal[
    "episodic",
    "notes",
    "reflection",
    "relationship",
    "facts",
]


# ─── Event dataclass ────────────────────────────────────────────────────────


@dataclass
class MemoryWriteEvent:
    """A successful memory write, surfaced to subscribers via :class:`MemoryWriteBus`.

    ``source_span_id`` carries the OTEL span id active at the write
    call-site (or ``None`` if no span is active).  PR 3b's loop-back guard
    uses this to suppress a :class:`SalienceWake` whose triggering write
    happened inside the agent's own LLM-call span — without that, a memory
    write inside an LLM response could trigger another wake → another LLM
    response → unbounded cost.

    The ``__post_init__`` clip is defensive — the formal contract is that
    write sites pass values in ``[0.0, 1.0]``.
    """

    agent_id: str
    tier: MemoryTier
    salience: float
    source_span_id: str | None
    written_at: float

    def __post_init__(self) -> None:
        # NaN sneaks past ``max``/``min`` (every comparison with NaN is
        # ``False``), so route it explicitly to 0.0 — otherwise a NaN
        # would silently fail PR 3b's threshold comparison and the
        # event would land in the "below threshold" branch by accident.
        if not math.isfinite(self.salience):
            self.salience = 0.0 if math.isnan(self.salience) or self.salience < 0 else 1.0
            return
        clipped = max(0.0, min(1.0, self.salience))
        if clipped != self.salience:
            self.salience = clipped


Subscriber = Callable[[MemoryWriteEvent], None]


# ─── Bus ────────────────────────────────────────────────────────────────────


class MemoryWriteBus:
    """In-process synchronous pub/sub.  No retention, no buffering."""

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.append(subscriber)

    def unsubscribe(self, subscriber: Subscriber) -> None:
        try:
            self._subscribers.remove(subscriber)
        except ValueError:
            # Idempotent — unsubscribing an unknown subscriber is a no-op.
            pass

    def publish(self, event: MemoryWriteEvent) -> None:
        # Copy the list so a subscriber that mutates the list during dispatch
        # (subscribes / unsubscribes itself) does not corrupt iteration.
        for subscriber in tuple(self._subscribers):
            try:
                subscriber(event)
            except Exception:
                # A buggy subscriber must not break the write path.  Log so
                # PR 3b's misbehaviour is debuggable, but swallow.
                logger.warning(
                    "MemoryWriteBus subscriber raised; suppressing",
                    exc_info=True,
                )


# ─── Module-level singleton + accessors ─────────────────────────────────────


# A single process-global bus means every subscriber sees events from
# **every agent** running in the process — there is no per-agent
# partitioning here.  PR 3b's :class:`EventLoop` subscriber MUST filter
# by :attr:`MemoryWriteEvent.agent_id` to route a wake only to its own
# loop; otherwise persona A's write would enqueue a :class:`SalienceWake`
# on persona B's loop.  The :attr:`MemoryWriteEvent.agent_id` field
# exists specifically to support that subscriber-side fan-out filter.
_global_bus: MemoryWriteBus = MemoryWriteBus()


def get_memory_write_bus() -> MemoryWriteBus:
    """Return the process-global :class:`MemoryWriteBus`."""
    return _global_bus


def set_memory_write_bus(bus: MemoryWriteBus) -> None:
    """Replace the process-global :class:`MemoryWriteBus`.

    Tests use this to install a fresh bus per case so subscriber lists do
    not leak across tests.  Production code does not call this — the bus
    is created once at module import and PR 3b's :class:`EventLoop` simply
    subscribes via :func:`get_memory_write_bus`.
    """
    global _global_bus
    _global_bus = bus


# ─── Convenience emitter ────────────────────────────────────────────────────


def emit_memory_write(
    *,
    agent_id: str,
    tier: MemoryTier,
    salience: float,
    source_span_id: str | None,
    written_at: float | None = None,
) -> None:
    """Publish a :class:`MemoryWriteEvent` on the global bus.

    No-op-shaped from the caller's perspective: returns ``None``; raises
    only on programmer error (bad ``tier``, etc.) caught at type-check
    time.  Subscriber exceptions are swallowed by :meth:`MemoryWriteBus.publish`.
    """
    _global_bus.publish(
        MemoryWriteEvent(
            agent_id=agent_id,
            tier=tier,
            salience=salience,
            source_span_id=source_span_id,
            written_at=written_at if written_at is not None else time.time(),
        ),
    )


__all__ = [
    "MemoryTier",
    "MemoryWriteBus",
    "MemoryWriteEvent",
    "Subscriber",
    "emit_memory_write",
    "get_memory_write_bus",
    "set_memory_write_bus",
]
