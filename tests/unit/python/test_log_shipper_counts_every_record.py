"""The log shipper loses no record without counting it (ISSUE-0207).

The shipper sends the agent's log records to the orchestrator over one
gRPC stream.  Two cases lost records while its counters said nothing was
lost:

* **Stop.**  After the last batch it cancelled the task that reads the
  orchestrator's acknowledgements, and cancelling that read cancels the
  whole call: the orchestrator had not read the stream yet.  Every record
  was counted as shipped.
* **A failed write.**  A batch leaves the queue before it is written.
  When the write failed the batch was gone, counted as neither shipped
  nor dropped.

Now a record is *shipped* once the orchestrator has acknowledged it, and
every other record that leaves the shipper is *dropped*: evicted from a
full queue, written to a stream that broke before it was acknowledged, or
still queued when a stop could not deliver it.  A batch whose write
failed goes back to the front of the queue.  After ``stop()``:
``enqueued == shipped + dropped``.

Most tests drive the shipper against a scripted stand-in for the stream
(``_log_stream_double``, no network).  One runs the issue's own
measurement against a real in-process log service on the loopback
interface.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from concurrent import futures
from typing import Any
from unittest.mock import MagicMock

import grpc
import pytest

from agents.generated import log_service_pb2 as logpb
from agents.generated import log_service_pb2_grpc as loggrpc
from agents.observability import log_shipper
from agents.observability.log_shipper import Shipper

from ._log_stream_double import Orchestrator, Stream


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reconnect and flush in milliseconds, so no test waits a second."""
    monkeypatch.setattr(log_shipper, "RECONNECT_BACKOFF_INITIAL_SEC", 0.01)
    monkeypatch.setattr(log_shipper, "RECONNECT_BACKOFF_CAP_SEC", 0.01)
    monkeypatch.setattr(log_shipper, "FLUSH_INTERVAL_SEC", 0.01)


@pytest.fixture
def connect(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Put a scripted orchestrator behind the shipper's stub."""
    def _connect(*scripts: Stream) -> Orchestrator:
        orchestrator = Orchestrator(list(scripts))
        monkeypatch.setattr(loggrpc, "LogServiceStub", lambda channel: orchestrator)
        return orchestrator
    return _connect


def _shipper(**kwargs: Any) -> Shipper:
    # A channel that is never used: the stub is the scripted orchestrator.
    return Shipper("unused:0", "ember-owl", channel=MagicMock(), **kwargs)


def _records(n: int, start: int = 0) -> list[dict[str, Any]]:
    return [{"level": "INFO", "message": f"m{i}"} for i in range(start, start + n)]


def _names(n: int, start: int = 0) -> list[str]:
    return [f"m{i}" for i in range(start, start + n)]


async def _until(condition: Any, *, seconds: float = 3.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "the shipper did not get there in time"
        await asyncio.sleep(0.005)


def _queued(shipper: Shipper) -> list[str]:
    return [r["message"] for r in shipper._queue]


# ─── A failed write ───────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAFailedWrite:
    async def test_the_batch_goes_back_to_the_front_of_the_queue(self, connect: Any) -> None:
        """The issue's measurement: 1 000 records, an unreachable orchestrator.

        768 of them used to vanish in three attempts.
        """
        orchestrator = connect(Stream(fail_write_at=0))
        shipper = _shipper()
        for record in _records(1000):
            shipper.enqueue(record)
        await shipper.start()
        try:
            await _until(lambda: len(orchestrator.calls) >= 3)
            # A batch is out of the queue while its write is under way, so
            # wait for the third attempt's batch to come back.
            await _until(lambda: len(shipper._queue) == 1000)

            assert _queued(shipper) == _names(1000), "all of them, in their order"
            assert (shipper.shipped, shipper.dropped) == (0, 0)
        finally:
            await shipper.stop(timeout=2.0)

    async def test_records_arrive_in_order_once_the_orchestrator_is_back(
        self, connect: Any,
    ) -> None:
        orchestrator = connect(Stream(fail_write_at=0), Stream(fail_write_at=0), Stream())
        shipper = _shipper(batch_max=4)
        for record in _records(10):
            shipper.enqueue(record)
        await shipper.start()

        await _until(lambda: shipper.shipped == 10)
        await shipper.stop(timeout=2.0)

        assert orchestrator.received == _names(10), "no gap, no repeat, in order"
        assert (shipper.enqueued, shipper.shipped, shipper.dropped) == (10, 10, 0)

    async def test_a_batch_put_back_into_a_full_queue_evicts_the_oldest(
        self, connect: Any,
    ) -> None:
        """The queue's bound holds, and what it costs is counted."""
        orchestrator = connect(Stream(fail_write_at=0, hold_first_write=True), Stream())
        shipper = _shipper(max_queue=6, batch_max=4)
        for record in _records(6):
            shipper.enqueue(record)
        await shipper.start()
        await _until(lambda: len(orchestrator.calls) == 1 and len(shipper._queue) == 2)
        for record in _records(4, start=6):  # the queue is full again: m4 … m9
            shipper.enqueue(record)
        assert shipper.dropped == 0

        orchestrator.release.set()  # the held write fails; m0 … m3 come back
        await _until(lambda: shipper.shipped == 6)

        assert shipper.dropped == 4
        assert orchestrator.received == _names(6, start=4), "the four oldest were evicted"
        await shipper.stop(timeout=2.0)
        assert shipper.enqueued == 10 == shipper.shipped + shipper.dropped


@pytest.mark.asyncio
async def test_a_record_that_cannot_be_encoded_is_dropped_and_the_rest_are_sent(
    connect: Any,
) -> None:
    """Such a record can never be sent.  Put back with a failed batch, it
    would fail every stream after it; lost uncounted, it would be the gap
    this file is about."""
    orchestrator = connect(Stream())
    shipper = _shipper(batch_max=4)
    records = _records(5)
    records[2]["source"] = {"file": "x.py", "line": "not a number"}
    for record in records:
        shipper.enqueue(record)
    await shipper.start()

    await _until(lambda: shipper.shipped == 4)
    await shipper.stop(timeout=2.0)

    assert orchestrator.received == ["m0", "m1", "m3", "m4"]
    assert (shipper.enqueued, shipper.shipped, shipper.dropped) == (5, 4, 1)
    assert len(orchestrator.calls) == 1, "the stream did not break over it"


# ─── A stream that breaks after a write ───────────────────────────────────


@pytest.mark.asyncio
class TestAWriteThatWasNeverAcknowledged:
    async def test_it_is_counted_as_dropped_and_the_failed_batch_is_sent_again(
        self, connect: Any,
    ) -> None:
        """Stream one takes a batch, acknowledges nothing, and breaks.

        The first batch is in doubt: written, never acknowledged.  It is
        counted as dropped (this orchestrator happens to have read it; a
        real one may not have).  The second batch, whose write failed, was
        put back, and stream two delivers it and the rest.
        """
        orchestrator = connect(Stream(fail_write_at=1, acks="end"), Stream())
        shipper = _shipper(batch_max=4)
        for record in _records(10):
            shipper.enqueue(record)
        await shipper.start()

        await _until(lambda: shipper.shipped == 6)
        await shipper.stop(timeout=2.0)

        assert orchestrator.received == _names(10)
        assert (shipper.enqueued, shipper.shipped, shipper.dropped) == (10, 6, 4)

    async def test_an_acknowledgement_that_arrived_as_the_write_failed_still_counts(
        self, connect: Any,
    ) -> None:
        """The reader had not run yet when the write after it failed.

        A broken stream ends its reader by itself, after whatever had
        arrived.  The shipper lets it get there before it counts, or these
        four records, which the orchestrator did acknowledge, would be
        counted as dropped.
        """
        orchestrator = connect(Stream(fail_write_at=1, acks="late"), Stream())
        shipper = _shipper(batch_max=4)
        for record in _records(10):
            shipper.enqueue(record)
        await shipper.start()

        await _until(lambda: shipper.shipped == 10)
        await shipper.stop(timeout=2.0)

        assert orchestrator.received == _names(10)
        assert (shipper.shipped, shipper.dropped) == (10, 0)

    async def test_an_acknowledged_write_stays_shipped_when_the_stream_then_breaks(
        self, connect: Any,
    ) -> None:
        orchestrator = connect(Stream(fail_write_at=2), Stream())
        shipper = _shipper(batch_max=4)
        for record in _records(10):
            shipper.enqueue(record)
        await shipper.start()

        await _until(lambda: shipper.shipped == 10)
        await shipper.stop(timeout=2.0)

        assert orchestrator.received == _names(10)
        assert (shipper.shipped, shipper.dropped) == (10, 0)


# ─── Stop ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestStop:
    async def test_it_waits_for_the_orchestrator_to_read_the_stream(self, connect: Any) -> None:
        orchestrator = connect(Stream(acks="end", end_delay=0.2))
        shipper = _shipper(batch_max=8)
        for record in _records(20):
            shipper.enqueue(record)
        await shipper.start()
        await _until(lambda: len(orchestrator.received) == 20)

        started = time.monotonic()
        await shipper.stop(timeout=5.0)
        waited = time.monotonic() - started

        assert waited >= 0.2, "it stayed until the orchestrator had finished reading"
        assert waited < 2.0, "and no longer"
        assert orchestrator.clean_ends == 1
        assert not orchestrator.calls[0].cancelled, "the call was not cancelled under the reader"
        assert (shipper.shipped, shipper.last_ack_seq, shipper.dropped) == (20, 20, 0)

    async def test_it_gives_up_at_its_timeout_and_counts_what_was_not_acknowledged(
        self, connect: Any,
    ) -> None:
        orchestrator = connect(Stream(acks="none", end="hang"))
        shipper = _shipper(batch_max=8)
        for record in _records(20):
            shipper.enqueue(record)
        await shipper.start()
        await _until(lambda: len(orchestrator.received) == 20)

        started = time.monotonic()
        await shipper.stop(timeout=0.3)
        waited = time.monotonic() - started

        assert 0.3 <= waited < 1.5
        assert orchestrator.calls[0].cancelled
        assert (shipper.enqueued, shipper.shipped, shipper.dropped) == (20, 0, 20)

    async def test_against_an_unreachable_orchestrator_it_tries_once_and_counts_the_rest(
        self, connect: Any,
    ) -> None:
        """No tight loop: once stopping, a failed attempt is the last.

        A batch whose write fails goes back to the queue, and while the
        shipper is stopping the wait before a reconnect returns at once.
        Left alone, the two would retry as fast as the loop turns until
        the stop's timeout.
        """
        orchestrator = connect(Stream(fail_write_at=0))
        shipper = _shipper()
        for record in _records(1000):
            shipper.enqueue(record)
        await shipper.start()
        await _until(lambda: len(orchestrator.calls) >= 1)
        attempts_before = len(orchestrator.calls)

        started = time.monotonic()
        await shipper.stop(timeout=5.0)
        waited = time.monotonic() - started

        assert len(orchestrator.calls) - attempts_before <= 1, "one last attempt at most"
        assert waited < 1.0, "it did not spin until the timeout"
        assert len(shipper._queue) == 0
        assert (shipper.enqueued, shipper.shipped, shipper.dropped) == (1000, 0, 1000)

    @pytest.mark.parametrize(
        ("scripts", "dropped"),
        [
            ((Stream(),), 0),
            ((Stream(acks="end"),), 0),
            ((Stream(fail_write_at=0), Stream()), 0),
            ((Stream(fail_write_at=1, acks="end"), Stream()), 4),
            ((Stream(acks="each", end="error"),), 0),
            ((Stream(acks="none", end="error"),), 10),
            ((Stream(fail_write_at=0),), 10),
        ],
        ids=[
            "acknowledged batch by batch",
            "acknowledged once, at the end",
            "one failed write, then a working stream",
            "an unacknowledged batch, then a working stream",
            "all acknowledged, then the stream ends with an error",
            "nothing acknowledged, then the stream ends with an error",
            "an unreachable orchestrator",
        ],
    )
    async def test_every_record_is_shipped_or_dropped_when_it_returns(
        self, connect: Any, scripts: tuple[Stream, ...], dropped: int,
    ) -> None:
        connect(*scripts)
        shipper = _shipper(batch_max=4)
        for record in _records(10):
            shipper.enqueue(record)
        await shipper.start()
        await asyncio.sleep(0.1)

        await shipper.stop(timeout=2.0)

        assert len(shipper._queue) == 0
        assert shipper.enqueued == 10
        assert (shipper.shipped, shipper.dropped) == (10 - dropped, dropped)

    async def test_it_says_so_when_records_were_not_delivered(
        self, connect: Any, caplog: pytest.LogCaptureFixture,
    ) -> None:
        connect(Stream(fail_write_at=0))
        shipper = _shipper()
        for record in _records(3):
            shipper.enqueue(record)
        await shipper.start()

        with caplog.at_level(logging.INFO, logger="Persatrix.agent.log_shipper"):
            await shipper.stop(timeout=2.0)

        stopped = [r for r in caplog.records if r.getMessage().startswith("log shipper stopped")]
        assert [(r.name, r.levelno) for r in stopped] == [
            ("Persatrix.agent.log_shipper", logging.WARNING),
        ]
        assert "3 of 3 record(s) were not delivered" in stopped[0].getMessage()

    async def test_it_says_nothing_when_all_were_delivered(
        self, connect: Any, caplog: pytest.LogCaptureFixture,
    ) -> None:
        connect(Stream())
        shipper = _shipper()
        for record in _records(3):
            shipper.enqueue(record)
        await shipper.start()

        with caplog.at_level(logging.INFO, logger="Persatrix.agent.log_shipper"):
            await shipper.stop(timeout=2.0)

        assert shipper.shipped == 3
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ─── The issue's stop measurement, against a real log service ─────────────


class _SlowLogService(loggrpc.LogServiceServicer):
    """A log service that takes 20 ms to store each batch."""

    def __init__(self) -> None:
        self.received = 0
        self.read_to_the_end = False
        self.cancelled = False

    async def StreamLogs(  # noqa: N802 — gRPC servicer naming.
        self, request_iterator: Any, context: Any,
    ) -> AsyncIterator[logpb.LogAck]:
        try:
            async for batch in request_iterator:
                await asyncio.sleep(0.02)
                self.received += len(batch.entries)
            self.read_to_the_end = True
            yield logpb.LogAck(received_through_seq=self.received)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@pytest.mark.asyncio
async def test_a_stop_delivers_what_is_queued_to_a_slow_log_service() -> None:
    """2 000 records, 20 ms a batch: the service used to receive 256, 0 or 1."""
    service = _SlowLogService()
    server = grpc.aio.server(futures.ThreadPoolExecutor(max_workers=2))
    loggrpc.add_LogServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    shipper = Shipper(f"127.0.0.1:{port}", "ember-owl")
    for record in _records(2000):
        shipper.enqueue(record)
    await shipper.start()
    try:
        await shipper.stop(timeout=10.0)

        assert service.received == 2000
        assert service.read_to_the_end and not service.cancelled
        assert (shipper.shipped, shipper.last_ack_seq, shipper.dropped) == (2000, 2000, 0)
    finally:
        await server.stop(grace=0.5)
