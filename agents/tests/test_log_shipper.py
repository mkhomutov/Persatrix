"""Tests for the agent → orchestrator log shipper (RFC 0018 PR 5)."""

from __future__ import annotations

import asyncio
from concurrent import futures
from datetime import UTC, datetime
from typing import Any

import grpc
import pytest

from agents.generated import log_service_pb2 as logpb
from agents.generated import log_service_pb2_grpc as loggrpc
from agents.observability.log_shipper import (
    Shipper,
    get_active_shipper,
    record_to_proto,
    set_active_shipper,
)

# ─── record_to_proto ──────────────────────────────────────────────


def test_record_to_proto_lifts_typed_fields() -> None:
    rec = {
        "schema_version": "0.1",
        "level": "INFO",
        "message": "hello",
        "execution_id": "exec-1",
        "agent_id": "agent-x",
        "service.kind": "agent",
        "service.instance": "i-1",
        "trace_id": "trace-1",
        "extra_attr": "value",
    }
    proto = record_to_proto(rec)
    assert proto.message == "hello"
    assert proto.execution_id == "exec-1"
    assert proto.agent_id == "agent-x"
    assert proto.service_kind == "agent"
    assert proto.trace_id == "trace-1"
    # Unknown keys land in the attributes Struct.
    assert proto.attributes.fields["extra_attr"].string_value == "value"


def test_record_to_proto_handles_datetime_timestamp() -> None:
    ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    proto = record_to_proto({"timestamp": ts, "level": "INFO", "message": "m"})
    assert proto.timestamp.seconds == int(ts.timestamp())


def test_record_to_proto_malformed_timestamp_stamps_now_and_flags() -> None:
    # Issue #179 Should-Fix #2: malformed RFC 3339 timestamps must not
    # collapse to epoch-zero (which would pull the entry to the front
    # of every chronological merge).  The shipper stamps now() and
    # surfaces the failure via a `timestamp_parse_error=true` attribute.
    before = datetime.now(UTC)
    proto = record_to_proto({
        "timestamp": "not-a-real-timestamp",
        "level": "INFO",
        "message": "m",
    })
    after = datetime.now(UTC)
    # Stamped with the current wall clock, not 1970.
    stamped = proto.timestamp.ToDatetime(tzinfo=UTC)
    assert before <= stamped <= after
    # Flagged in attributes so downstream consumers can distinguish
    # stamped-now entries from well-formed ones.
    assert proto.attributes.fields["timestamp_parse_error"].bool_value is True


def test_record_to_proto_valid_string_timestamp_no_flag() -> None:
    ts = "2026-01-01T12:00:00+00:00"
    proto = record_to_proto({"timestamp": ts, "level": "INFO", "message": "m"})
    assert proto.timestamp.seconds == int(
        datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp(),
    )
    assert "timestamp_parse_error" not in proto.attributes.fields


def test_record_to_proto_with_source() -> None:
    proto = record_to_proto({
        "level": "ERROR",
        "message": "boom",
        "source": {"file": "foo.py", "line": 42, "function": "bar"},
    })
    assert proto.source.file == "foo.py"
    assert proto.source.line == 42
    assert proto.source.function == "bar"


# ─── Queue overflow / FIFO drop ───────────────────────────────────


def test_enqueue_drops_oldest_on_overflow() -> None:
    s = Shipper("127.0.0.1:9090", "agent-x", max_queue=3)
    for i in range(5):
        s.enqueue({"message": f"m{i}"})
    assert s.enqueued == 5
    assert s.dropped == 2
    # The two oldest (m0, m1) must have been evicted.
    remaining = list(s._queue)
    assert [r["message"] for r in remaining] == ["m2", "m3", "m4"]


# ─── Module-level handle ─────────────────────────────────────────


def test_active_shipper_get_set_roundtrip() -> None:
    assert get_active_shipper() is None
    s = Shipper("127.0.0.1:9090", "a", max_queue=1)
    set_active_shipper(s)
    try:
        assert get_active_shipper() is s
    finally:
        set_active_shipper(None)
    assert get_active_shipper() is None


# ─── End-to-end stream against an in-process LogServiceServicer ──


class _RecordingServicer(loggrpc.LogServiceServicer):
    """Minimal LogService that captures every received entry."""

    def __init__(self) -> None:
        self.batches: list[logpb.LogBatch] = []
        self.received_through = 0
        self.done = asyncio.Event()

    async def StreamLogs(  # noqa: N802 — gRPC servicer naming.
        self,
        request_iterator: Any,
        context: Any,
    ) -> Any:
        async for batch in request_iterator:
            self.batches.append(batch)
            self.received_through += len(batch.entries)
            yield logpb.LogAck(received_through_seq=self.received_through)
        # Final ack on EOF so the shipper's last_ack_seq matches.
        self.done.set()


@pytest.mark.asyncio
async def test_shipper_streams_and_acks_end_to_end() -> None:
    servicer = _RecordingServicer()
    server = grpc.aio.server(futures.ThreadPoolExecutor(max_workers=4))
    loggrpc.add_LogServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    shipper = Shipper(f"127.0.0.1:{port}", "agent-x", max_queue=100, batch_max=8)
    await shipper.start()

    try:
        for i in range(5):
            shipper.enqueue({
                "schema_version": "0.1",
                "level": "INFO",
                "message": f"m{i}",
                "execution_id": "exec-1",
            })
        # Allow the drain task time to flush + the ack to come back.
        for _ in range(40):
            if shipper.last_ack_seq >= 5:
                break
            await asyncio.sleep(0.05)
        assert shipper.last_ack_seq >= 5
        assert shipper.shipped == 5
        # Per-entry agent_id empty → batch-level agent_id is "agent-x".
        assert any(b.agent_id == "agent-x" for b in servicer.batches)
        # Total entries received == enqueued.
        total = sum(len(b.entries) for b in servicer.batches)
        assert total == 5
    finally:
        await shipper.stop(timeout=2.0)
        await server.stop(grace=0.5)
