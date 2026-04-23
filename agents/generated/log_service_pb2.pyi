"""Type stubs for generated protobuf module log_service_pb2."""

from typing import Any

from google.protobuf.message import Message
from google.protobuf.struct_pb2 import Struct
from google.protobuf.timestamp_pb2 import Timestamp


class LogEntry(Message):
    schema_version: str
    timestamp: Timestamp
    level: str
    service_kind: str
    service_instance: str
    service_role: str
    message: str
    execution_id: str
    step_id: str
    agent_id: str
    request_id: str
    trace_id: str
    span_id: str
    attributes: Struct
    source: "LogEntry.Source"

    class Source(Message):
        file: str
        line: int
        function: str
        def __init__(
            self,
            *,
            file: str = ...,
            line: int = ...,
            function: str = ...,
        ) -> None: ...

    def __init__(
        self,
        *,
        schema_version: str = ...,
        timestamp: Timestamp | None = ...,
        level: str = ...,
        service_kind: str = ...,
        service_instance: str = ...,
        service_role: str = ...,
        message: str = ...,
        execution_id: str = ...,
        step_id: str = ...,
        agent_id: str = ...,
        request_id: str = ...,
        trace_id: str = ...,
        span_id: str = ...,
        attributes: Struct | None = ...,
        source: "LogEntry.Source | None" = ...,
    ) -> None: ...


class LogBatch(Message):
    entries: list[LogEntry]
    agent_id: str
    def __init__(
        self,
        *,
        entries: list[LogEntry] | None = ...,
        agent_id: str = ...,
    ) -> None: ...


class LogAck(Message):
    received_through_seq: int
    def __init__(self, *, received_through_seq: int = ...) -> None: ...


# Module-level descriptor (referenced by the _grpc.py companion).
DESCRIPTOR: Any
