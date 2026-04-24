"""Persatrix agent → orchestrator log shipper (RFC 0018 PR 5).

This module owns the bidi gRPC stream that delivers structured log
records from the Python agent process to the orchestrator's
``LogService``.  The structlog processor chain configured by
:mod:`agents.observability.logging` enqueues every emitted record onto
the shipper's bounded :class:`asyncio.Queue` via :func:`enqueue`; a
single background task drains the queue, batches entries, and streams
them.

Key invariants
--------------
* **Bounded queue**: the queue holds at most :data:`MAX_QUEUE` records.
  When full, :func:`enqueue` drops the **oldest** entry (FIFO drop) and
  increments :attr:`Shipper.dropped` so the loss is observable.
  This is the deliberate trade-off documented in RFC 0018 § E — a slow
  orchestrator must never back-pressure the agent's hot path.
* **Reconnect with exponential backoff**: when the stream errors the
  shipper retries with backoff (1s → 2s → 4s → … → 30s cap).  Records
  enqueued during the gap stay in the queue (subject to the FIFO drop).
* **Graceful flush**: :meth:`Shipper.stop` drains the queue and awaits
  the final ack from the orchestrator before returning, so a clean
  ``server.stop()`` does not lose tail records.

Wire-format conversion
----------------------
The shipper accepts plain ``dict`` records (the ``event_dict`` produced
by the structlog chain after :func:`_reorder_keys`) and maps them onto
``persatrix.v1.LogEntry`` proto messages.  Unknown keys go into the
``attributes`` ``Struct`` so the downstream JSON line layer still
surfaces them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging as _stdlib_logging
import os
import threading
from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import grpc
from google.protobuf import struct_pb2, timestamp_pb2

from agents.generated import log_service_pb2 as logpb
from agents.generated import log_service_pb2_grpc as loggrpc

# ─── Tunables ────────────────────────────────────────────────────────────────

#: Maximum in-memory record queue depth.  At ~500 bytes per entry this
#: caps shipper memory at ~2.5 MiB per agent process — trivial against
#: the LLM context buffers but enough headroom to absorb a multi-second
#: orchestrator hiccup.  Override via PERSATRIX_LOG_SHIPPER_QUEUE.
MAX_QUEUE = 5000

#: Maximum entries per outgoing :class:`LogBatch`.  Mirrors the
#: orchestrator-side cap (``maxEntriesPerBatch`` in ``logs_service.go``);
#: keeping these in lockstep avoids InvalidArgument flapping if the
#: shipper occasionally fills its window.
BATCH_MAX = 256

#: Maximum wait between batch flushes when the queue is non-empty but
#: not yet full.  Keeps latency bounded for low-traffic agents that
#: would otherwise sit on a single record indefinitely.
FLUSH_INTERVAL_SEC = 0.5

#: Backoff schedule used when the gRPC stream errors.  Doubles up to
#: the cap so an orchestrator restart settles within a single 30s slot.
RECONNECT_BACKOFF_INITIAL_SEC = 1.0
RECONNECT_BACKOFF_CAP_SEC = 30.0

#: Reserved keys lifted directly onto the proto's typed fields rather
#: than packed into the attributes Struct.  Order is irrelevant — the
#: lookup is by name.
_RESERVED_KEYS: frozenset[str] = frozenset({
    "schema_version",
    "timestamp",
    "level",
    "service.kind",
    "service.instance",
    "service.role",
    "message",
    "execution_id",
    "step_id",
    "agent_id",
    "request_id",
    "trace_id",
    "span_id",
    "source",
})

#: structlog / stdlib-logging bookkeeping keys that should never reach
#: the orchestrator's attributes Struct.  ``_record`` is structlog's
#: stdlib LogRecord shim; ``_from_structlog`` flags records that
#: round-tripped through the stdlib bridge; ``logger``/``stack_info``/
#: ``exc_info`` are stdlib LogRecord internals.  Filtering by exact
#: name keeps the contract stable; the additional ``_``-prefix sweep
#: in ``record_to_proto`` covers ad-hoc bookkeeping keys added by
#: third-party processors.  (PR #173 review nice-to-have #1.)
_BOOKKEEPING_KEYS: frozenset[str] = frozenset({
    "_record",
    "_from_structlog",
    "logger",
    "stack_info",
    "exc_info",
})

logger = _stdlib_logging.getLogger("Persatrix.agent.log_shipper")


# ─── Module-level shipper handle ─────────────────────────────────────────────

_active_shipper: Shipper | None = None  # type: ignore[name-defined]


def get_active_shipper() -> Shipper | None:  # type: ignore[name-defined]
    """Return the process-global shipper, if any.

    The structlog tail processor calls this on every record emission;
    when ``None`` (shipper not started, or already stopped) the record
    is rendered to stderr only.
    """
    return _active_shipper


def set_active_shipper(s: Shipper | None) -> None:  # type: ignore[name-defined]
    """Install the process-global shipper handle.  Idempotent."""
    global _active_shipper
    _active_shipper = s


# ─── Shipper ────────────────────────────────────────────────────────────────


class Shipper:
    """Drain queued log records onto a long-lived gRPC bidi stream."""

    def __init__(
        self,
        orchestrator_grpc_target: str,
        agent_id: str,
        *,
        max_queue: int = MAX_QUEUE,
        batch_max: int = BATCH_MAX,
    ) -> None:
        self.target = orchestrator_grpc_target
        self.agent_id = agent_id
        self.max_queue = max_queue
        self.batch_max = batch_max

        # asyncio.Queue with maxsize would block put_nowait() rather
        # than evict; we want FIFO eviction so the hot path is
        # uninterrupted.  A deque guarded by a threading.Lock gives us
        # cross-thread-safe append+popleft (structlog tail processors
        # may emit from worker threads — gRPC executor pools, aiohttp
        # resolver, third-party SDKs that thread off blocking I/O).
        # The asyncio.Event is woken via call_soon_threadsafe so the
        # cross-thread caller never touches loop-affine state directly.
        # (PR #173 review Must-Fix #3.)
        self._queue: deque[dict[str, Any]] = deque()
        self._queue_lock = threading.Lock()
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._channel: grpc.aio.Channel | None = None

        # Counters surfaced for tests and (future) metric instruments.
        self.enqueued: int = 0
        self.dropped: int = 0
        self.shipped: int = 0
        self.last_ack_seq: int = 0

    # ── Public surface ──────────────────────────────────────────────

    def enqueue(self, record: dict[str, Any]) -> None:
        """Append a log record.

        Non-blocking; drops the oldest queued record on overflow so the
        hot path of the structlog chain never awaits.  Safe to call
        from any thread or coroutine: the deque is guarded by a
        ``threading.Lock`` and the loop-affine ``asyncio.Event`` wake
        is dispatched via ``loop.call_soon_threadsafe``.  (PR #173
        review Must-Fix #3 — prior to this change, calling enqueue from
        a non-loop thread mutated the deque and ``Event`` directly,
        which is undefined and could wedge the drain task.)
        """
        with self._queue_lock:
            if len(self._queue) >= self.max_queue:
                self._queue.popleft()
                self.dropped += 1
            self._queue.append(record)
            self.enqueued += 1
        # Wake the drain task.  When called from the loop thread the
        # direct .set() is fine; from another thread we must hop onto
        # the loop because asyncio.Event is not thread-safe.  start()
        # captures the loop so this branch only runs after start().
        loop = self._loop
        if loop is None:
            # enqueue() before start() — record is queued; the drain
            # task's first iteration will see the wake set anyway.
            self._wake.set()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._wake.set()
        else:
            loop.call_soon_threadsafe(self._wake.set)

    async def start(self) -> None:
        """Open the channel + spawn the drain task.  Idempotent."""
        if self._task is not None:
            return
        # Capture the loop so cross-thread enqueue() can route the
        # wake through call_soon_threadsafe (PR #173 review Must-Fix #3).
        self._loop = asyncio.get_running_loop()
        self._channel = grpc.aio.insecure_channel(self.target)
        self._task = asyncio.create_task(self._run(), name="log-shipper")

    async def stop(self, *, timeout: float = 5.0) -> None:
        """Drain the queue, close the stream, and shut the channel down.

        ``timeout`` caps the drain wait so a wedged orchestrator does
        not hold up agent shutdown indefinitely.
        """
        if self._task is None:
            return
        self._stopping.set()
        self._wake.set()
        try:
            await asyncio.wait_for(self._task, timeout=timeout)
        except TimeoutError:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
        self._task = None
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
        set_active_shipper(None)

    # ── Internals ───────────────────────────────────────────────────

    def _queue_nonempty(self) -> bool:
        """Lock-guarded ``len(self._queue) > 0`` for cross-thread safety."""
        with self._queue_lock:
            return bool(self._queue)

    async def _run(self) -> None:
        """Outer loop: keep a stream open, reconnect with backoff on error."""
        backoff = RECONNECT_BACKOFF_INITIAL_SEC
        while not self._stopping.is_set() or self._queue_nonempty():
            try:
                await self._stream_once()
                # Clean EOF (orchestrator closed) — reset backoff so
                # the next reconnect tries quickly.
                backoff = RECONNECT_BACKOFF_INITIAL_SEC
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — reconnect-with-backoff is the contract.
                logger.warning(
                    "log shipper stream error, reconnecting in %.1fs",
                    backoff,
                    exc_info=True,
                )
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2.0, RECONNECT_BACKOFF_CAP_SEC)

    async def _stream_once(self) -> None:
        """Single bidi stream lifetime; returns on EOF, raises on error."""
        if self._channel is None:  # pragma: no cover — start() guards this.
            return
        stub = loggrpc.LogServiceStub(self._channel)
        call = stub.StreamLogs()

        async def request_iter() -> Any:
            while True:
                # Drain to a batch.  The lock keeps the popleft loop
                # consistent with cross-thread enqueue() callers
                # (PR #173 review Must-Fix #3).
                batch_records: list[dict[str, Any]] = []
                with self._queue_lock:
                    while self._queue and len(batch_records) < self.batch_max:
                        batch_records.append(self._queue.popleft())
                if batch_records:
                    yield self._batch_proto(batch_records)
                    self.shipped += len(batch_records)
                    continue
                if self._stopping.is_set():
                    return
                # Wait for either a wake or a flush-interval tick.
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=FLUSH_INTERVAL_SEC,
                    )

        # Consume acks concurrently so the high-water mark advances
        # even when we are waiting on the request iterator.
        async def ack_consumer() -> None:
            async for ack in call:
                self.last_ack_seq = max(self.last_ack_seq, int(ack.received_through_seq))

        ack_task = asyncio.create_task(ack_consumer(), name="log-shipper-acks")
        try:
            async for batch in request_iter():
                await call.write(batch)
            await call.done_writing()
        finally:
            ack_task.cancel()
            with contextlib.suppress(BaseException):
                await ack_task

    def _batch_proto(self, records: Iterable[dict[str, Any]]) -> logpb.LogBatch:
        return logpb.LogBatch(
            entries=[record_to_proto(r) for r in records],
            agent_id=self.agent_id,
        )


# ─── Record → proto conversion ──────────────────────────────────────────────


def record_to_proto(record: dict[str, Any]) -> logpb.LogEntry:
    """Convert a structlog event dict to ``persatrix.v1.LogEntry``."""
    e = logpb.LogEntry(
        schema_version=str(record.get("schema_version", "")),
        level=str(record.get("level", "")),
        service_kind=str(record.get("service.kind", "")),
        service_instance=str(record.get("service.instance", "")),
        service_role=str(record.get("service.role", "")),
        message=str(record.get("message", "")),
        execution_id=str(record.get("execution_id", "")),
        step_id=str(record.get("step_id", "")),
        agent_id=str(record.get("agent_id", "")),
        request_id=str(record.get("request_id", "")),
        trace_id=str(record.get("trace_id", "")),
        span_id=str(record.get("span_id", "")),
    )
    ts_parse_error = False
    if (ts := record.get("timestamp")) is not None:
        proto_ts, ts_parse_error = _to_timestamp(ts)
        e.timestamp.CopyFrom(proto_ts)
    if (src := record.get("source")) is not None and isinstance(src, dict):
        e.source.CopyFrom(logpb.LogEntry.Source(
            file=str(src.get("file", "")),
            line=int(src.get("line", 0)),
            function=str(src.get("function", "")),
        ))
    extras = {
        k: v
        for k, v in record.items()
        if k not in _RESERVED_KEYS
        and k not in _BOOKKEEPING_KEYS
        and not k.startswith("_")
    }
    if ts_parse_error and "timestamp_parse_error" not in extras:
        # Issue #179 Should-Fix #2: surface malformed-timestamp inputs
        # rather than silently emitting an epoch-zero entry that would
        # sort to the front of every chronological merge in the
        # orchestrator's handleListLogs / SSE broadcast path.
        #
        # Guard: if a producer record already carries its own
        # `timestamp_parse_error` key (unlikely but possible for
        # pass-through / replay logs), preserve the user value rather
        # than silently overwriting it.  The shipper's flag is a
        # best-effort signal; an explicit producer value takes
        # precedence.  (PR #182 review Should-Fix #1.)
        extras["timestamp_parse_error"] = True
    if extras:
        e.attributes.CopyFrom(_dict_to_struct(extras))
    return e


def _to_timestamp(ts: Any) -> tuple[timestamp_pb2.Timestamp, bool]:
    """Convert a record timestamp to a proto Timestamp.

    Returns ``(timestamp, parse_error)``.  ``parse_error`` is ``True``
    only for string inputs that failed RFC 3339 parsing; callers
    surface this as a ``timestamp_parse_error=true`` attribute so the
    downstream merge path can distinguish stamped-now entries from
    well-formed ones.  Issue #179 Should-Fix #2.
    """
    out = timestamp_pb2.Timestamp()
    if isinstance(ts, datetime):
        out.FromDatetime(ts)
        return out, False
    if isinstance(ts, (int, float)):
        # Preserve sub-second precision for float epoch inputs; the
        # previous FromSeconds(int(ts)) silently truncated milliseconds
        # so SSE / chronological merges across log sources lost
        # ordering information for entries within the same second.
        # (PR #173 review nice-to-have #5.)
        out.FromNanoseconds(int(ts * 1_000_000_000))
        return out, False
    if isinstance(ts, str):
        # Best-effort RFC 3339 parse.  On failure stamp the current
        # wall clock and flag the entry so it still sorts into the
        # vicinity of its siblings rather than collapsing the whole
        # merged view onto 1970-01-01.
        try:
            out.FromDatetime(datetime.fromisoformat(ts.replace("Z", "+00:00")))
            return out, False
        except ValueError:
            out.FromDatetime(datetime.now(UTC))
            return out, True
    # Unknown type: stamp now to keep the entry chronologically sane.
    out.FromDatetime(datetime.now(UTC))
    return out, True


def _dict_to_struct(d: dict[str, Any]) -> struct_pb2.Struct:
    s = struct_pb2.Struct()
    s.update({k: _coerce(v) for k, v in d.items()})
    return s


def _coerce(v: Any) -> Any:
    """Coerce arbitrary Python values into Struct-compatible types."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, dict):
        return {str(k): _coerce(val) for k, val in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_coerce(item) for item in v]
    return repr(v)


# ─── Env helpers ────────────────────────────────────────────────────────────


def queue_capacity_from_env(default: int = MAX_QUEUE) -> int:
    """Honour PERSATRIX_LOG_SHIPPER_QUEUE for the shipper's queue cap."""
    raw = os.environ.get("PERSATRIX_LOG_SHIPPER_QUEUE")
    if not raw:
        return default
    try:
        n = int(raw)
    except ValueError:
        return default
    return n if n > 0 else default
