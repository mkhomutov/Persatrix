"""Type stubs for generated protobuf module agent_message_pb2."""

from google.protobuf.message import Message

# ─── Enums (MessageType) ──────────────────────────────
TEXT: int
DECISION: int
QUESTION: int
TASK_ASSIGN: int
STATUS_UPDATE: int
ESCALATION: int
APPROVAL_REQ: int
APPROVAL_RESP: int
SOCIAL: int

# ─── Enums (Visibility) ───────────────────────────────
CHANNEL: int
PRIVATE: int
CONFIDENTIAL: int

# ─── Messages ─────────────────────────────────────────

class Attachment(Message):
    filename: str
    mime_type: str
    content: bytes
    def __init__(
        self,
        *,
        filename: str = ...,
        mime_type: str = ...,
        content: bytes = ...,
    ) -> None: ...

class AgentMessage(Message):
    message_id: str
    channel_id: str
    sender_id: str
    thread_id: str
    type: int
    content: str
    attachments: list[Attachment]
    mentions: list[str]
    reply_to: str
    visibility: int
    timestamp: int
    def __init__(
        self,
        *,
        message_id: str = ...,
        channel_id: str = ...,
        sender_id: str = ...,
        thread_id: str = ...,
        type: int = ...,
        content: str = ...,
        attachments: list[Attachment] | None = ...,
        mentions: list[str] | None = ...,
        reply_to: str = ...,
        visibility: int = ...,
        timestamp: int = ...,
    ) -> None: ...

class SendMessageResponse(Message):
    message_id: str
    delivered: bool
    def __init__(
        self,
        *,
        message_id: str = ...,
        delivered: bool = ...,
    ) -> None: ...

class SubscribeRequest(Message):
    channel_id: str
    agent_id: str
    since_timestamp: int
    def __init__(
        self,
        *,
        channel_id: str = ...,
        agent_id: str = ...,
        since_timestamp: int = ...,
    ) -> None: ...
