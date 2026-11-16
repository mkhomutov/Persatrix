"""What an agent's log shipper does when the orchestrator has no log service.

ISSUE-0204: the orchestrator keeps running when its log buffer cannot
start, and serves agents on its gRPC port without the log service.  Every
agent's shipper then meets a port that answers "unimplemented" for as long
as that orchestrator runs.  Keeping the orchestrator up is only right if
the shipper takes that quietly, so this file holds it to three things:

* it does not end: the shipper's task is still running, and tries again;
* it does not flood the agent's own log: each try waits out the backoff,
  which doubles up to its cap and is never reset by a failed try;
* it does not hold up the agent: a log call returns at once, the queue
  keeps its bound, and a stop returns with every record counted.

The orchestrator here is a real gRPC server on a loopback port with no
service registered.  That is what the agent-facing port is to a shipper
when the log buffer did not start: the wallet service is there, the log
service is not.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import grpc
import pytest
import pytest_asyncio

from agents.generated import log_service_pb2_grpc as loggrpc
from agents.observability import log_shipper
from agents.observability.log_shipper import Shipper, set_active_shipper
from agents.observability.logging import _ship_to_orchestrator

_INITIAL = 0.02
_CAP = 0.08
#: Every wait on the shipper is bounded by this, so a shipper that hangs
#: fails the test instead of hanging it.
_LIMIT = 10.0


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real schedule is 1 s doubling to 30 s; this one has its shape."""
    monkeypatch.setattr(log_shipper, "RECONNECT_BACKOFF_INITIAL_SEC", _INITIAL)
    monkeypatch.setattr(log_shipper, "RECONNECT_BACKOFF_CAP_SEC", _CAP)
    monkeypatch.setattr(log_shipper, "FLUSH_INTERVAL_SEC", 0.005)


@pytest_asyncio.fixture
async def channel() -> AsyncIterator[grpc.aio.Channel]:
    """A channel to an orchestrator whose gRPC port has no log service."""
    server = grpc.aio.server()
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield channel
    finally:
        await channel.close()
        await server.stop(grace=0)


class _Warnings(logging.Handler):
    """Records when the shipper warned, and hands each warning back to the
    shipper as the agent's logging chain does: the shipper's own warning is
    a record like any other, so there is always something to send."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.at: list[float] = []
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.at.append(time.monotonic())
        self.messages.append(record.getMessage())
        _ship_to_orchestrator(None, "", {
            "level": record.levelname, "message": record.getMessage(),
            "exc_info": record.exc_info, "_record": record,
        })


@pytest.fixture
def warnings() -> Any:
    handler = _Warnings()
    logger = logging.getLogger("Persatrix.agent.log_shipper")
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)
    set_active_shipper(None)


def _record(i: int) -> dict[str, Any]:
    return {"level": "INFO", "message": f"m{i}"}


@pytest.mark.asyncio
async def test_the_stand_in_answers_as_an_orchestrator_without_a_log_service(
    channel: grpc.aio.Channel,
) -> None:
    """The control: a log stream to this port ends "unimplemented"."""
    call = loggrpc.LogServiceStub(channel).StreamLogs()
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await asyncio.wait_for(call.read(), _LIMIT)
    assert refused.value.code() == grpc.StatusCode.UNIMPLEMENTED


@pytest.mark.asyncio
async def test_the_shipper_keeps_trying_no_faster_than_its_backoff(
    channel: grpc.aio.Channel, warnings: _Warnings,
) -> None:
    """A try fails when a record is written to a stream that has already
    ended, so it takes an agent that keeps logging to make the shipper try
    again at all.  This one logs every few milliseconds, far faster than
    the backoff, and the tries still come no faster than the backoff."""
    shipper = Shipper("unused:0", "ember-owl", channel=channel, max_queue=50)
    set_active_shipper(shipper)
    await shipper.start()
    task = shipper._task
    assert task is not None

    async def a_busy_agent() -> None:
        i = 0
        while True:
            shipper.enqueue(_record(i))
            i += 1
            await asyncio.sleep(0.002)

    agent = asyncio.create_task(a_busy_agent())
    try:
        # Seven tries: the doubling (0.02, 0.04), then the cap four times.
        started = time.monotonic()
        while len(warnings.at) < 7:
            assert time.monotonic() - started < _LIMIT, (
                f"only {len(warnings.at)} tries in {_LIMIT} s: the shipper gave up"
            )
            assert not task.done(), "the shipper's task ended on the missing service"
            await asyncio.sleep(0.005)

        gaps = [b - a for a, b in zip(warnings.at, warnings.at[1:], strict=False)]
        schedule = [_INITIAL, 2 * _INITIAL, _CAP, _CAP, _CAP, _CAP]
        for n, (gap, wait) in enumerate(zip(gaps, schedule, strict=False)):
            # A slow machine only makes a gap longer, so the wait is a floor.
            assert gap >= wait * 0.9, (
                f"try {n + 2} came {gap:.3f} s after try {n + 1}; the backoff there is {wait} s. "
                f"All gaps: {[round(g, 3) for g in gaps]}"
            )
        assert set(warnings.messages) <= {
            f"log shipper stream error, reconnecting in {wait:.1f}s"
            for wait in (_INITIAL, 2 * _INITIAL, _CAP)
        }, "one line per try, and nothing else"
        assert len(shipper._queue) <= 50, "the queue keeps its bound while the service is away"
        assert shipper.shipped == 0
    finally:
        agent.cancel()
        await asyncio.wait_for(shipper.stop(timeout=1.0), _LIMIT)


@pytest.mark.asyncio
async def test_a_quiet_agent_logs_nothing_about_the_missing_service(
    channel: grpc.aio.Channel, warnings: _Warnings,
) -> None:
    """A record written to a stream that is ending is accepted, and nothing
    fails until the next record.  So an agent with nothing more to log does
    not hear of the missing service again, however long it runs; its
    records are counted as not delivered when the stream ends."""
    shipper = Shipper("unused:0", "ember-owl", channel=channel, max_queue=50)
    set_active_shipper(shipper)
    await shipper.start()
    for i in range(20):
        shipper.enqueue(_record(i))

    await asyncio.sleep(10 * _CAP)

    assert warnings.at == [], "no line for a stream nothing more was written to"
    task = shipper._task
    assert task is not None and not task.done()
    await asyncio.wait_for(shipper.stop(timeout=1.0), _LIMIT)
    assert (shipper.shipped, shipper.dropped) == (0, shipper.enqueued)


@pytest.mark.asyncio
async def test_a_log_call_returns_at_once_and_a_stop_counts_every_record(
    channel: grpc.aio.Channel, warnings: _Warnings,
) -> None:
    shipper = Shipper("unused:0", "ember-owl", channel=channel, max_queue=50)
    set_active_shipper(shipper)
    await shipper.start()
    await asyncio.sleep(_INITIAL)  # the first try has met the missing service

    started = time.monotonic()
    for i in range(10_000):
        shipper.enqueue(_record(i))  # a plain call: nothing here waits on the stream
    took = time.monotonic() - started

    assert took < 2.0, f"10,000 log calls took {took:.2f} s with the log service away"
    assert len(shipper._queue) <= 50

    stopping = time.monotonic()
    await asyncio.wait_for(shipper.stop(timeout=1.0), _LIMIT)

    assert time.monotonic() - stopping < 3.0, "a stop does not wait on the missing service"
    assert shipper.shipped == 0, "nothing was acknowledged: there is nobody to acknowledge"
    assert shipper.enqueued == shipper.dropped, "and every record is counted as not delivered"
    assert shipper.enqueued >= 10_000
