"""What the log shipper's queue holds, and what a stopped shipper does.

Found in review of the ISSUE-0207 fix, which made a failed batch go back
to the queue and made every record count as shipped or dropped.

* **What is queued.**  A record logged with a traceback carries the live
  exception.  The shipper's own "stream error, reconnecting" warning is
  such a record, it is queued like any other, and its traceback holds the
  frame of the failed attempt and so the whole batch that attempt had
  encoded.  With batches going back to the queue instead of being lost,
  those records piled up for as long as the orchestrator was away: one
  batch kept alive per attempt.  The queue now leaves out what never
  reaches the wire.
* **A stopped shipper.**  Its counters must still add up when a record
  arrives late, from another thread or after the stop; and the lines the
  shipper logs while it stops must not be queued into the shipper that
  is stopping.

Uses the scripted stream stand-in (``_log_stream_double``, no network).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from agents.generated import log_service_pb2_grpc as loggrpc
from agents.observability import log_shipper
from agents.observability.log_shipper import Shipper, get_active_shipper, set_active_shipper
from agents.observability.logging import _ship_to_orchestrator

from ._log_stream_double import Orchestrator, Stream


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reconnect and flush in milliseconds, so no test waits a second."""
    monkeypatch.setattr(log_shipper, "RECONNECT_BACKOFF_INITIAL_SEC", 0.01)
    monkeypatch.setattr(log_shipper, "RECONNECT_BACKOFF_CAP_SEC", 0.01)
    monkeypatch.setattr(log_shipper, "FLUSH_INTERVAL_SEC", 0.01)


@pytest.fixture(autouse=True)
def _no_shipper_left_behind() -> Any:
    yield
    set_active_shipper(None)


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


def _record(i: int) -> dict[str, Any]:
    return {"level": "INFO", "message": f"m{i}"}


class _TheLoggingChain(logging.Handler):
    """Does what the agent's logging chain does with each record: hands
    it to the shipper that is installed, if one is."""

    def emit(self, record: logging.LogRecord) -> None:
        _ship_to_orchestrator(None, "", {
            "level": record.levelname, "message": record.getMessage(),
            "exc_info": record.exc_info, "_record": record,
        })


@pytest.fixture
def chain() -> Any:
    """The shipper's own logger, wired the way the agent wires every logger."""
    handler = _TheLoggingChain()
    logger = logging.getLogger("Persatrix.agent.log_shipper")
    logger.addHandler(handler)
    yield
    logger.removeHandler(handler)


# ─── What is queued ───────────────────────────────────────────────────────


def test_a_queued_record_holds_no_traceback_and_no_logging_record() -> None:
    shipper = _shipper()
    try:
        raise ConnectionError("failed to connect to all addresses")
    except ConnectionError:
        caught = sys.exc_info()
    given = {
        "level": "WARN", "message": "stream error", "exc_info": caught,
        "_record": logging.makeLogRecord({}), "attempt": 7,
    }

    shipper.enqueue(given)

    assert list(shipper._queue) == [{"level": "WARN", "message": "stream error", "attempt": 7}]
    assert set(given) == {"level", "message", "exc_info", "_record", "attempt"}, (
        "the caller's own record is left as it was"
    )


@pytest.mark.asyncio
async def test_an_outage_does_not_fill_the_queue_with_tracebacks(
    connect: Any, chain: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each failed attempt logs a warning with its traceback, and that
    warning is queued.  None of them may keep the attempt's batch alive."""
    orchestrator = connect(Stream(fail_write_at=0))
    shipper = _shipper()
    set_active_shipper(shipper)
    # Every record that leaves the queue for a batch, attempt after attempt.
    # (The queue itself is empty whenever a batch is being written.)
    batched: list[dict[str, Any]] = []
    encode = shipper._encode

    def record_and_encode(records: list[dict[str, Any]]) -> Any:
        batched.extend(records)
        return encode(records)

    monkeypatch.setattr(shipper, "_encode", record_and_encode)
    for i in range(3):
        shipper.enqueue(_record(i))
    await shipper.start()
    try:
        for _ in range(600):  # until a few attempts have failed
            if len(orchestrator.calls) >= 5:
                break
            await asyncio.sleep(0.005)
        assert len(orchestrator.calls) >= 5

        warnings = [r for r in batched if "stream error" in r["message"]]
        assert warnings, "the reconnect warnings are queued like any record"
        assert not [r for r in batched if "exc_info" in r or "_record" in r]
    finally:
        await shipper.stop(timeout=1.0)


# ─── An acknowledgement that covers the failed batch ──────────────────────


@pytest.mark.asyncio
async def test_a_batch_the_orchestrator_acknowledged_is_not_sent_again(connect: Any) -> None:
    """A write can report a failure for a batch that did arrive.  When its
    acknowledgement came in, the batch is shipped, not put back."""
    orchestrator = connect(Stream(fail_write_at=0, failed_write_arrived=True), Stream())
    shipper = _shipper()
    for i in range(3):
        shipper.enqueue(_record(i))
    await shipper.start()

    await shipper.stop(timeout=2.0)

    assert orchestrator.received == ["m0", "m1", "m2"], "each record once"
    assert (shipper.enqueued, shipper.shipped, shipper.dropped) == (3, 3, 0)


# ─── A stopped shipper ────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAStoppedShipper:
    async def test_a_record_that_arrives_after_the_stop_is_counted_as_dropped(
        self, connect: Any,
    ) -> None:
        connect(Stream())
        shipper = _shipper()
        shipper.enqueue(_record(0))
        await shipper.start()
        await shipper.stop(timeout=2.0)

        shipper.enqueue(_record(1))  # a caller that still holds the shipper

        assert len(shipper._queue) == 0, "nothing will ever send it"
        assert (shipper.enqueued, shipper.shipped, shipper.dropped) == (2, 1, 1)

    async def test_a_stop_before_any_start_counts_what_was_queued(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        shipper = _shipper()
        for i in range(3):
            shipper.enqueue(_record(i))

        with caplog.at_level(logging.WARNING, logger="Persatrix.agent.log_shipper"):
            await shipper.stop(timeout=1.0)
            await shipper.stop(timeout=1.0)  # a second stop adds nothing

        assert len(shipper._queue) == 0
        assert (shipper.enqueued, shipper.shipped, shipper.dropped) == (3, 0, 3)
        stopped = [r for r in caplog.records if r.getMessage().startswith("log shipper stopped")]
        assert len(stopped) == 1
        assert "3 of 3 record(s) were not acknowledged" in stopped[0].getMessage()

    async def test_two_stops_at_once_do_not_trip_over_each_other(self, connect: Any) -> None:
        connect(Stream(end="hang"))
        shipper = _shipper()
        shipper.enqueue(_record(0))
        await shipper.start()

        results = await asyncio.gather(
            shipper.stop(timeout=0.2), shipper.stop(timeout=0.2), return_exceptions=True,
        )

        assert results == [None, None]
        assert shipper.enqueued == shipper.shipped + shipper.dropped == 1

    async def test_the_lines_it_logs_while_stopping_are_not_queued_into_it(
        self, connect: Any, chain: None, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """With the agent's logging chain wired, the shipper's own warnings
        would be handed back to it.  The handle is cleared first, so they
        go to the process's own log output only, and are not counted."""
        connect(Stream(fail_write_at=0))
        shipper = _shipper()
        set_active_shipper(shipper)
        for i in range(3):
            shipper.enqueue(_record(i))
        await shipper.start()

        with caplog.at_level(logging.WARNING, logger="Persatrix.agent.log_shipper"):
            await shipper.stop(timeout=2.0)

        assert get_active_shipper() is None
        messages = [r.getMessage() for r in caplog.records]
        assert any("stream error while stopping" in m for m in messages), messages
        stopped = [m for m in messages if m.startswith("log shipper stopped")]
        assert len(stopped) == 1
        assert "3 of 3 record(s) were not acknowledged" in stopped[0]
        assert (shipper.enqueued, shipper.shipped, shipper.dropped) == (3, 0, 3)
