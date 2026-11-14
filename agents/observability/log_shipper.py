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
  enqueued during the gap stay in the queue (subject to the FIFO drop),
  and a batch whose write failed goes back to the front of it.
* **Graceful flush**: :meth:`Shipper.stop` sends what is queued and
  waits, up to its timeout, for the orchestrator to read the stream to
  its end and acknowledge it, so a clean ``server.stop()`` does not lose
  tail records.
* **Every record is counted** (ISSUE-0207): a record is *shipped* once
  the orchestrator has acknowledged it, and *dropped* when it leaves the
  shipper any other way.  After ``stop()``,
  ``enqueued == shipped + dropped``, and a record handed to a stopped
  shipper is counted as dropped.  "Dropped" means not acknowledged: the
  orchestrator may have received a record it had not yet acknowledged
  when the stream ended.

The record → wire conversion is :mod:`.log_record_proto`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging as _stdlib_logging
import os
import threading
from collections import deque
from typing import Any

import grpc

from ..generated import log_service_pb2 as logpb
from ..generated import log_service_pb2_grpc as loggrpc
from .log_record_proto import record_to_proto

# ─── Tunables ────────────────────────────────────────────────────────────────

#: Maximum in-memory record queue depth.  At ~500 bytes per entry this
#: caps shipper memory at ~2.5 MiB per agent process — trivial against
#: the LLM context buffers but enough headroom to absorb a multi-second
#: orchestrator hiccup.  Override via PERSATRIX_LOG_SHIPPER_QUEUE.
MAX_QUEUE = 5000

#: Keys a queued record does not keep.  Neither reaches the wire
#: (``record_to_proto`` leaves them out), and both hold live objects: a
#: traceback keeps every frame it passed through alive, and a frame of
#: this module holds the batch it was sending.  The shipper's own
#: "stream error" warning is queued with such a traceback on every failed
#: attempt, so through an outage each one would keep a whole batch alive,
#: and the estimate above would not hold.
_NOT_QUEUED = ("exc_info", "_record")

#: Maximum entries per outgoing :class:`LogBatch`.  Must stay at or under
#: the orchestrator-side cap (``maxEntriesPerBatch``, 1024, in
#: ``internal/server/logs_service.go``): a larger batch ends the stream
#: with InvalidArgument.
BATCH_MAX = 256

#: Maximum wait between batch flushes when the queue is non-empty but
#: not yet full.  Keeps latency bounded for low-traffic agents that
#: would otherwise sit on a single record indefinitely.
FLUSH_INTERVAL_SEC = 0.5

#: How long a broken stream's acknowledgement reader is given to end.  A
#: stream that breaks ends its reader by itself, after any acknowledgement
#: that had already arrived; this only bounds the wait for one that does
#: not.
ACK_SETTLE_SEC = 0.5

#: Backoff schedule used when the gRPC stream errors.  Doubles up to
#: the cap so an orchestrator restart settles within a single 30s slot.
RECONNECT_BACKOFF_INITIAL_SEC = 1.0
RECONNECT_BACKOFF_CAP_SEC = 30.0

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
        channel: grpc.aio.Channel | None = None,
    ) -> None:
        self.target = orchestrator_grpc_target
        self.agent_id = agent_id
        self.max_queue = max_queue
        self.batch_max = batch_max
        # RFC 0023 — when *channel* is supplied the shipper streams logs
        # over a channel owned by the caller (AgentServer shares one
        # orchestrator channel between LogService and WalletService).
        # An externally-owned channel is NOT closed by stop(): its owner
        # closes it once every stub over it is done.
        self._external_channel = channel is not None

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
        # Set, under the lock, once stop() has counted what was left: a
        # record that arrives after that has nobody to send it.
        self._closed = False
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._channel: grpc.aio.Channel | None = channel

        # Counters surfaced for tests and (future) metric instruments.
        # None of them ever goes down, and after stop():
        # enqueued == shipped + dropped (ISSUE-0207).
        #: Records handed to enqueue().
        self.enqueued: int = 0
        #: Records that did not reach the orchestrator, or are not known to
        #: have: evicted from a full queue, impossible to encode, written to
        #: a stream that ended before they were acknowledged, or still
        #: queued when a stop could not deliver them.
        self.dropped: int = 0
        #: Records the orchestrator has acknowledged.
        self.shipped: int = 0
        #: The highest acknowledgement seen.  The orchestrator counts from
        #: zero on every stream, so this is not a total.
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
        if any(key in record for key in _NOT_QUEUED):
            record = {k: v for k, v in record.items() if k not in _NOT_QUEUED}
        with self._queue_lock:
            self.enqueued += 1
            if self._closed:
                # Stopped: a caller on another thread, or one that still
                # holds the shipper.  Counted, so the counters add up.
                self.dropped += 1
                return
            if len(self._queue) >= self.max_queue:
                self._queue.popleft()
                self.dropped += 1
            self._queue.append(record)
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
        # Open a channel only when the caller did not supply one (RFC 0023).
        if self._channel is None:
            self._channel = grpc.aio.insecure_channel(self.target)
        self._task = asyncio.create_task(self._run(), name="log-shipper")

    async def stop(self, *, timeout: float = 5.0) -> None:
        """Send what is queued, close the stream, and shut the channel down.

        Waits for the orchestrator to read the stream to its end and
        acknowledge it.  ``timeout`` caps that wait so a wedged
        orchestrator does not hold up agent shutdown indefinitely.
        Whatever was not acknowledged by then, and whatever is still
        queued, is counted as dropped (ISSUE-0207), and a WARNING says how
        many records that was.  The orchestrator may have received some of
        them: it acknowledges every few records, and nothing says which
        arrived after its last acknowledgement.

        The process-wide handle is cleared first.  From here on a log line
        goes to the process's own log output only: the lines this stop
        itself logs must not be queued into the shipper that is stopping,
        where they would be counted as records it failed to deliver.

        Safe to call again, and before :meth:`start`: a shipper that never
        started still counts what was queued.
        """
        task, self._task = self._task, None
        with self._queue_lock:
            if task is None and self._closed:
                return
        set_active_shipper(None)
        if task is not None:
            self._stopping.set()
            self._wake.set()
            try:
                await asyncio.wait_for(task, timeout=timeout)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        self._drop_queued()
        # Close the channel only when the shipper opened it — an
        # externally-supplied channel is closed by its owner (RFC 0023).
        if self._channel is not None and not self._external_channel:
            await self._channel.close()
        self._channel = None
        if self.dropped:
            logger.warning(
                "log shipper stopped: %d of %d record(s) were not acknowledged "
                "by the orchestrator (some may have arrived; every record is "
                "in this process's own log output)",
                self.dropped, self.enqueued,
            )

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
                if self._stopping.is_set():
                    # ISSUE-0207: one attempt on the way out, no reconnect.
                    # A batch whose write fails goes back to the queue, and
                    # once stopping the wait below returns at once, so
                    # against an unreachable orchestrator this loop would
                    # turn as fast as it can until stop()'s timeout.
                    # stop() counts what is still queued as dropped.
                    logger.warning(
                        "log shipper stream error while stopping, not reconnecting",
                        exc_info=True,
                    )
                    return
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

    def _drop_queued(self) -> None:
        """Count every record still queued as dropped, empty the queue,
        and close it: :meth:`enqueue` counts a later record as dropped."""
        with self._queue_lock:
            self.dropped += len(self._queue)
            self._queue.clear()
            self._closed = True

    def _requeue(self, records: list[dict[str, Any]]) -> None:
        """Put *records* back at the front of the queue, oldest first.

        The queue keeps its bound: when records arrived meanwhile and it
        is over ``max_queue``, the oldest are evicted and counted as
        dropped, as :meth:`enqueue` does.
        """
        with self._queue_lock:
            self._queue.extendleft(reversed(records))
            while len(self._queue) > self.max_queue:
                self._queue.popleft()
                self.dropped += 1

    async def _next_batch(self) -> list[dict[str, Any]] | None:
        """The next records to send; ``None`` once stopping with nothing queued."""
        while True:
            # The lock keeps the popleft loop consistent with cross-thread
            # enqueue() callers (PR #173 review Must-Fix #3).
            with self._queue_lock:
                count = min(len(self._queue), self.batch_max)
                records = [self._queue.popleft() for _ in range(count)]
            if records:
                return records
            if self._stopping.is_set():
                return None
            # Wait for either a wake or a flush-interval tick.
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=FLUSH_INTERVAL_SEC)

    def _encode(self, records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], logpb.LogBatch]:
        """Encode *records* as one batch; return the records that are in it.

        A record that cannot be encoded can never be sent.  It is counted
        as dropped here and left out: put back in the queue with the rest
        of a failed batch, it would fail every stream after this one.
        """
        sendable: list[dict[str, Any]] = []
        entries: list[logpb.LogEntry] = []
        for record in records:
            try:
                entries.append(record_to_proto(record))
            except Exception:  # noqa: BLE001 — whatever a record holds, the stream goes on.
                with self._queue_lock:
                    self.dropped += 1
                continue
            sendable.append(record)
        return sendable, logpb.LogBatch(entries=entries, agent_id=self.agent_id)

    async def _stream_once(self) -> None:
        """Single bidi stream lifetime; returns on EOF, raises on error.

        How records are counted (ISSUE-0207).  The orchestrator
        acknowledges how many records it has received on this stream, and
        a record is *shipped* when an acknowledgement covers it.  When the
        stream ends, however it ends:

        * a batch whose write did not return goes back to the front of
          the queue;
        * records written and never acknowledged are counted as dropped.
          The orchestrator may have received some of them (it acknowledges
          every few records), but nothing says so.

        On a stop the stream is half-closed and this waits for the reader
        to reach the end of it: the orchestrator answers a half-close with
        a last acknowledgement once it has read everything.  The reader
        used to be cancelled right after the half-close, and cancelling a
        pending read cancels the whole call, so the orchestrator lost
        whatever it had not read yet.  The wait is bounded by stop()'s
        timeout, which cancels this task.
        """
        if self._channel is None:  # pragma: no cover — start() guards this.
            return
        stub = loggrpc.LogServiceStub(self._channel)
        call = stub.StreamLogs()
        written = 0  # records handed to this stream
        acked = 0  # of those, how many the orchestrator has acknowledged
        in_flight: list[dict[str, Any]] = []  # the batch whose write has not returned

        # Consume acks concurrently so the high-water mark advances
        # even when we are waiting on the request iterator.
        async def ack_consumer() -> None:
            nonlocal acked
            async for ack in call:
                through = int(ack.received_through_seq)
                self.last_ack_seq = max(self.last_ack_seq, through)
                newly = min(through, written) - acked
                if newly > 0:
                    acked += newly
                    self.shipped += newly

        ack_task = asyncio.create_task(ack_consumer(), name="log-shipper-acks")
        try:
            while (records := await self._next_batch()) is not None:
                in_flight, batch = self._encode(records)
                if not in_flight:
                    continue
                # Counted before the write: its acknowledgement can arrive
                # before the write returns.
                written += len(in_flight)
                await call.write(batch)
                in_flight = []
            await call.done_writing()
            await ack_task
        except asyncio.CancelledError:
            raise
        except Exception:
            # The stream broke.  Let its reader take in what had already
            # arrived, so a record the orchestrator did acknowledge is not
            # counted as dropped because the write after it failed first.
            await asyncio.wait({ack_task}, timeout=ACK_SETTLE_SEC)
            raise
        finally:
            ack_task.cancel()
            with contextlib.suppress(BaseException):
                await ack_task
            if in_flight:
                # Put back what no acknowledgement covers.
                confirmed = max(0, acked - (written - len(in_flight)))
                written -= len(in_flight) - confirmed
                self._requeue(in_flight[confirmed:])
            if written > acked:
                with self._queue_lock:
                    self.dropped += written - acked


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
