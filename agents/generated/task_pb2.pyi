"""Type stubs for generated protobuf module task_pb2."""

from typing import ClassVar

from google.protobuf.descriptor import Descriptor
from google.protobuf.message import Message

# ─── Enums (TaskStatus) ────────────────────────────────
PENDING: int
RUNNING: int
COMPLETED: int
FAILED: int
CANCELLED: int
RETRYING: int

# ─── Enums (HealthStatus) ──────────────────────────────
UNKNOWN: int
SERVING: int
NOT_SERVING: int

# ─── Messages ──────────────────────────────────────────

class TaskConfig(Message):
    max_llm_calls: int
    max_tokens: int
    timeout_seconds: int
    allowed_tools: list[str]
    def __init__(
        self,
        *,
        max_llm_calls: int = ...,
        max_tokens: int = ...,
        timeout_seconds: int = ...,
        allowed_tools: list[str] | None = ...,
    ) -> None: ...

class TaskRequest(Message):
    task_id: str
    workflow_id: str
    agent_id: str
    payload: str
    context: dict[str, str]
    config: TaskConfig
    def __init__(
        self,
        *,
        task_id: str = ...,
        workflow_id: str = ...,
        agent_id: str = ...,
        payload: str = ...,
        context: dict[str, str] | None = ...,
        config: TaskConfig | None = ...,
    ) -> None: ...

class TaskResponse(Message):
    task_id: str
    status: int
    result: str
    metadata: dict[str, str]
    error_message: str
    def __init__(
        self,
        *,
        task_id: str = ...,
        status: int = ...,
        result: str = ...,
        metadata: dict[str, str] | None = ...,
        error_message: str = ...,
    ) -> None: ...

class TaskProgress(Message):
    task_id: str
    status: int
    message: str
    progress_percent: float
    timestamp: int
    def __init__(
        self,
        *,
        task_id: str = ...,
        status: int = ...,
        message: str = ...,
        progress_percent: float = ...,
        timestamp: int = ...,
    ) -> None: ...

class HealthCheckRequest(Message):
    service: str
    def __init__(self, *, service: str = ...) -> None: ...

class HealthCheckResponse(Message):
    status: int
    def __init__(self, *, status: int = ...) -> None: ...
