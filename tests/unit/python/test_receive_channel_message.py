"""
Tests for the AgentService.ReceiveChannelMessage handler.

RFC 0011 PR 4a shipped the real receiver-side handler; RFC 0024 Phase 4
inverted its dispatch shape. The handler now:

1. Validates the wire-side ``ChannelMessageEvent`` (mentions cap, content
   size, channel_type/channel_id prefix agreement, thread_id length).
2. Resolves the target agent on this single-agent-per-process server
   (``agents/server.py`` enforces single-agent today; multi-agent
   disambiguation deferred — see ``error_message`` taxonomy below).
3. Constructs an ``AgentEvent(event_type=CHANNEL_MESSAGE)`` populated
   from the proto fields plus the additive top-level ``thread_id``
   per RFC 0011 §D.
4. Enqueues it **fire-and-forget** onto the agent's per-agent
   ``EventLoop`` via ``EventDispatcher.enqueue_inbound`` (no
   ``SyncDispatchHandle``); the loop owns processing (decide → execute →
   recover) when it drains.
5. Returns ``TaskAck(success=True)`` once the wake is accepted, or
   ``TaskAck(success=False)`` when the loop's bounded queue is full
   (discard-not-block backpressure, RFC 0024 Decided §1).

Validation failures return ``TaskAck(success=False, error_message=...)``
and DO NOT enqueue; ``error_message`` is taxonomised so operators reading
wire traces can locate the failure class.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import grpc
import pytest

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.dispatch import EventDispatcher
from agents.generated import task_pb2
from agents.persona_types import AgentEvent, EventType
from agents.server_servicers import AgentServiceServicer


class _StubAgent(BaseAgent):
    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="ok")


def _make_servicer(
    *,
    agents: dict[str, BaseAgent] | None = None,
    enqueue_accepts: bool = True,
) -> tuple[AgentServiceServicer, MagicMock]:
    """Build a servicer over a mock dispatcher.

    ``enqueue_inbound`` is a *synchronous* method returning a bool
    (accepted / dropped); model it with a plain ``MagicMock`` so tests
    can assert call args and toggle the backpressure outcome.
    """
    if agents is None:
        agents = {"ember-owl": _StubAgent(agent_id="ember-owl", config={"model": "test"})}
    dispatcher = MagicMock(spec=EventDispatcher)
    dispatcher.enqueue_inbound = MagicMock(return_value=enqueue_accepts)
    return AgentServiceServicer(agents, dispatcher), dispatcher


def _channel_event(**overrides: Any) -> task_pb2.ChannelMessageEvent:
    fields: dict[str, Any] = {
        "message_id": "msg-001",
        "channel_id": "group:general",
        "channel_type": "group",
        "sender_id": "iron-fox",
        "content": "hello",
        "timestamp": "2026-05-04T00:00:00Z",
        "thread_id": "",
        "mentions": [],
        # RFC 0011 PR 4b additions: validator now requires
        # ``respond_policy``. Default to ``always`` so the existing
        # cases keep passing; gate-specific tests live in
        # ``test_response_gate.py``.
        "respond_policy": "always",
        "thread_parent_sender_id": "",
    }
    fields.update(overrides)
    return task_pb2.ChannelMessageEvent(**fields)


def _enqueued_event(dispatcher: MagicMock) -> AgentEvent:
    """The ``AgentEvent`` the handler passed to ``enqueue_inbound``."""
    dispatcher.enqueue_inbound.assert_called_once()
    return dispatcher.enqueue_inbound.call_args.args[1]


# ─── Happy path ────────────────────────────────────────────


class TestReceiveChannelMessageHappyPath:
    async def test_returns_success_true(self):
        servicer, _ = _make_servicer()
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(), MagicMock(spec=grpc.aio.ServicerContext)
        )
        assert isinstance(ack, task_pb2.TaskAck)
        assert ack.success is True
        assert ack.error_message == ""

    async def test_enqueues_channel_message_event(self):
        servicer, dispatcher = _make_servicer()
        await servicer.ReceiveChannelMessage(
            _channel_event(
                channel_id="dm:alpha:beta",
                channel_type="dm",
                sender_id="alpha",
                content="ping",
                thread_id="msg-parent-123",
                mentions=["ember-owl", "iron-fox"],
            ),
            MagicMock(spec=grpc.aio.ServicerContext),
        )

        dispatcher.enqueue_inbound.assert_called_once()
        call = dispatcher.enqueue_inbound.call_args
        assert call.args[0] == "ember-owl"
        event = call.args[1]
        assert isinstance(event, AgentEvent)
        assert event.event_type is EventType.CHANNEL_MESSAGE
        assert event.channel_id == "dm:alpha:beta"
        assert event.sender_id == "alpha"
        assert event.message_id == "msg-001"
        assert event.thread_id == "msg-parent-123"
        assert event.payload["content"] == "ping"
        assert event.payload["channel_type"] == "dm"
        assert event.payload["mentions"] == ["ember-owl", "iron-fox"]


# ─── Backpressure: full event-loop queue rejects with success=False ──


class TestReceiveChannelMessageBackpressure:
    async def test_queue_full_returns_success_false(self):
        """A full event-loop queue (``enqueue_inbound`` -> False) surfaces as
        ``TaskAck(success=False)`` with a taxonomised overload reason —
        discard-not-block, RFC 0024 Decided §1."""
        servicer, dispatcher = _make_servicer(enqueue_accepts=False)
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(), MagicMock(spec=grpc.aio.ServicerContext)
        )
        assert ack.success is False
        assert "overloaded" in ack.error_message or "queue full" in ack.error_message
        # The event was still *offered* to the loop (the drop happens at
        # enqueue time, not before).
        dispatcher.enqueue_inbound.assert_called_once()


# ─── Validation: invalid inputs are rejected without enqueue ──


class TestReceiveChannelMessageValidation:
    async def test_rejects_oversized_content(self):
        servicer, dispatcher = _make_servicer()
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(content="x" * 4001),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        assert ack.success is False
        assert "content" in ack.error_message
        dispatcher.enqueue_inbound.assert_not_called()

    async def test_rejects_too_many_mentions(self):
        servicer, dispatcher = _make_servicer()
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(mentions=[f"agent-{i}" for i in range(11)]),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        assert ack.success is False
        assert "mentions" in ack.error_message
        dispatcher.enqueue_inbound.assert_not_called()

    async def test_rejects_invalid_mention_id(self):
        servicer, dispatcher = _make_servicer()
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(mentions=["BAD_ID"]),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        assert ack.success is False
        assert "mention" in ack.error_message
        dispatcher.enqueue_inbound.assert_not_called()

    async def test_rejects_oversized_thread_id(self):
        servicer, dispatcher = _make_servicer()
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(thread_id="x" * 129),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        assert ack.success is False
        assert "thread_id" in ack.error_message
        dispatcher.enqueue_inbound.assert_not_called()

    @pytest.mark.parametrize(
        ("channel_id", "channel_type"),
        [
            ("group:general", "dm"),       # type disagrees with prefix
            ("dm:a:b", "group"),
            ("thread:x", "group"),
            ("noprefix", "group"),         # unknown prefix
            ("group:general", "bogus"),    # unknown type
        ],
    )
    async def test_rejects_channel_type_disagreement(
        self, channel_id: str, channel_type: str
    ):
        servicer, dispatcher = _make_servicer()
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(channel_id=channel_id, channel_type=channel_type),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        assert ack.success is False
        assert "channel_type" in ack.error_message or "channel_id" in ack.error_message
        dispatcher.enqueue_inbound.assert_not_called()


# ─── Agent resolution ──────────────────────────────────────


class TestReceiveChannelMessageAgentResolution:
    async def test_no_agents_registered_returns_failure(self):
        servicer, dispatcher = _make_servicer(agents={})
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(), MagicMock(spec=grpc.aio.ServicerContext)
        )
        assert ack.success is False
        assert "no agents" in ack.error_message.lower()
        dispatcher.enqueue_inbound.assert_not_called()

    async def test_multi_agent_server_returns_failure(self):
        # v0.3.0 server is single-agent-per-process; multi-agent
        # disambiguation requires an additive `recipient_id` proto field
        # deferred to a follow-up PR (chat-path migration scope).
        agents = {
            "ember-owl": _StubAgent(agent_id="ember-owl", config={"model": "test"}),
            "iron-fox": _StubAgent(agent_id="iron-fox", config={"model": "test"}),
        }
        servicer, dispatcher = _make_servicer(agents=agents)
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(), MagicMock(spec=grpc.aio.ServicerContext)
        )
        assert ack.success is False
        assert "multi" in ack.error_message.lower() or "recipient" in ack.error_message.lower()
        # PR #248 deep review Low: the rejection message is a wire-trace
        # taxonomy element. Pin the **specific** follow-up PR pointer
        # (4a-ii) rather than the obsolete "PR 4 chat-path migration"
        # phrasing — the original PR 4 was split 2026-05-04 into
        # 4a-i / 4a-ii / 4b, and the additive ``recipient_id`` field
        # lands in 4a-ii (see docs/rfcs/0011-pr-plan.md line 248). A
        # stale pointer here would mis-route operators tracing a
        # rejected ack back to the responsible slice.
        assert "4a-ii" in ack.error_message
        # Negative-pin: the obsolete pointer must NOT reappear.
        assert "PR 4 chat-path" not in ack.error_message
        dispatcher.enqueue_inbound.assert_not_called()


# ─── Timestamp propagation + thread_id coercion ────────────


class TestReceiveChannelMessageEventConstruction:
    async def test_propagates_wire_timestamp(self):
        """Receiver MUST forward the orchestrator's RFC 3339 publish-time.

        Re-stamping with ``time.time()`` would lose cross-agent ordering.
        PR #248 deep review M finding.
        """
        servicer, dispatcher = _make_servicer()
        await servicer.ReceiveChannelMessage(
            _channel_event(timestamp="2026-05-04T12:34:56Z"),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = _enqueued_event(dispatcher)
        # 2026-05-04T12:34:56Z = 1777898096.0 Unix seconds (UTC).
        assert event.timestamp == pytest.approx(1777898096.0, abs=1.0)

    async def test_rejects_malformed_timestamp(self):
        """Per proto contract: malformed timestamps are a drop reason."""
        servicer, dispatcher = _make_servicer()
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(timestamp="not-a-timestamp"),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        assert ack.success is False
        assert "timestamp" in ack.error_message
        dispatcher.enqueue_inbound.assert_not_called()

    async def test_rejects_empty_timestamp(self):
        servicer, dispatcher = _make_servicer()
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(timestamp=""),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        assert ack.success is False
        assert "timestamp" in ack.error_message
        dispatcher.enqueue_inbound.assert_not_called()

    async def test_empty_thread_id_coerced_to_none(self):
        """Empty wire ``thread_id`` MUST become ``event.thread_id is None``.

        The ``request.thread_id or None`` coercion at the servicer
        construction site was previously implicit-only. PR #248 deep
        review L finding.
        """
        servicer, dispatcher = _make_servicer()
        await servicer.ReceiveChannelMessage(
            _channel_event(thread_id=""),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = _enqueued_event(dispatcher)
        assert event.thread_id is None


# ─── sender_id / channel_id / message_id bounds ────────────


class TestReceiveChannelMessageIdValidation:
    @pytest.mark.parametrize("bad_sender", ["BAD_ID", "with space", "-leading", ""])
    async def test_rejects_invalid_sender_id(self, bad_sender: str):
        """`sender_id` MUST satisfy the participant-id pattern symmetrically
        with ``mentions[i]`` — same trust boundary, stronger trust claim.
        PR #248 deep review M finding (trust-boundary asymmetry)."""
        servicer, dispatcher = _make_servicer()
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(sender_id=bad_sender),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        assert ack.success is False
        assert "sender_id" in ack.error_message
        dispatcher.enqueue_inbound.assert_not_called()

    async def test_rejects_oversized_channel_id(self):
        servicer, dispatcher = _make_servicer()
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(channel_id="group:" + "x" * 300),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        assert ack.success is False
        assert "channel_id" in ack.error_message
        dispatcher.enqueue_inbound.assert_not_called()

    async def test_rejects_oversized_message_id(self):
        servicer, dispatcher = _make_servicer()
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(message_id="m" * 65),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        assert ack.success is False
        assert "message_id" in ack.error_message
        dispatcher.enqueue_inbound.assert_not_called()


# ─── Cascade-depth wire propagation (PR 3 of v0.3.0 test-findings) ─


class TestReceiveChannelMessageCascadeDepth:
    """RFC 0011 amendment "Cascade-depth wire propagation" (PR 3).

    The gRPC receive path reads ``request.cascade_depth`` (typed proto
    field added in PR 1 of the v0.3.0 channel test-findings plan) and
    seeds the resulting ``AgentEvent.metadata["cascade_depth"]`` so the
    downstream processing path (now the per-agent ``EventLoop``) sees the
    wire value instead of defaulting to zero. Without this seed the
    receiver-side cascade guard would be permanently armed at depth=0 on
    every inbound cross-process hop — the original F-1 failure mode.
    """

    async def test_wire_cascade_depth_seeds_event_metadata(self):
        servicer, dispatcher = _make_servicer()
        await servicer.ReceiveChannelMessage(
            _channel_event(cascade_depth=4),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = _enqueued_event(dispatcher)
        assert event.metadata.get("cascade_depth") == 4, (
            f"servicer must seed metadata.cascade_depth from the typed "
            f"proto field; got metadata={event.metadata!r}"
        )

    async def test_zero_cascade_depth_seeds_zero(self):
        """Proto3 implicit presence: unset == zero, and zero is cascade-origin.

        The servicer MUST still seed the metadata key explicitly so the
        loop's cascade guard (``event.metadata.get("cascade_depth", 0)``)
        and the eventual `+1` increment have a deterministic starting point.
        """
        servicer, dispatcher = _make_servicer()
        await servicer.ReceiveChannelMessage(
            _channel_event(cascade_depth=0),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = _enqueued_event(dispatcher)
        assert event.metadata.get("cascade_depth") == 0


class TestReceiveChannelMessageParticipantType:
    """ISSUE-0068 / RFC 0011 participant-type wire-propagation amendment.

    The gRPC receive path reads ``request.sender_participant_type`` (typed
    proto field) and seeds the resulting
    ``AgentEvent.metadata["sender_participant_type"]`` — the exact key the
    episode-routing close path reads to set ``other_participant_type`` on
    the relationship row. Before this, the field was dropped at the proto
    boundary and every channel-delivered (REST) chat peer was recorded as
    ``agent`` instead of ``user``.
    """

    async def test_wire_participant_type_seeds_event_metadata(self):
        servicer, dispatcher = _make_servicer()
        await servicer.ReceiveChannelMessage(
            _channel_event(sender_participant_type="user"),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = _enqueued_event(dispatcher)
        assert event.metadata.get("sender_participant_type") == "user", (
            f"servicer must seed metadata.sender_participant_type from the "
            f"typed proto field; got metadata={event.metadata!r}"
        )

    async def test_empty_participant_type_not_seeded(self):
        """Empty wire field (genuine agent-to-agent traffic) leaves the key
        absent, so the episode-routing read defaults to ``agent`` — the
        correct peer type for inter-agent channel messages."""
        servicer, dispatcher = _make_servicer()
        await servicer.ReceiveChannelMessage(
            _channel_event(sender_participant_type=""),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = _enqueued_event(dispatcher)
        assert "sender_participant_type" not in event.metadata, (
            f"empty wire field must not seed the metadata key; "
            f"got metadata={event.metadata!r}"
        )


class TestReceiveChannelMessageInteractionID:
    """RFC 0030 deterministic governance layers (v0.3.8), PR 1.

    The gRPC receive path reads ``request.interaction_id`` (typed proto field,
    lifted from the publish metadata bag) and seeds
    ``AgentEvent.metadata["interaction_id"]`` so later layer PRs (Layer 1 cost
    ceiling; Layers 2/4 reply budgets / end-votes) can attribute per
    interaction. Inert this PR — nothing reads the key yet.
    """

    async def test_wire_interaction_id_seeds_event_metadata(self):
        servicer, dispatcher = _make_servicer()
        await servicer.ReceiveChannelMessage(
            _channel_event(interaction_id="4e2b7c9a-1f3d-4a6b-8c2e-9d0f1a2b3c4d"),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = _enqueued_event(dispatcher)
        assert event.metadata.get("interaction_id") == "4e2b7c9a-1f3d-4a6b-8c2e-9d0f1a2b3c4d", (
            f"servicer must seed metadata.interaction_id from the typed proto "
            f"field; got metadata={event.metadata!r}"
        )

    async def test_empty_interaction_id_not_seeded(self):
        """Empty wire field (untracked publish) leaves the key absent."""
        servicer, dispatcher = _make_servicer()
        await servicer.ReceiveChannelMessage(
            _channel_event(interaction_id=""),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = _enqueued_event(dispatcher)
        assert "interaction_id" not in event.metadata, (
            f"empty wire field must not seed the metadata key; "
            f"got metadata={event.metadata!r}"
        )

    async def test_overlong_interaction_id_not_seeded(self):
        """An over-length wire id drops to untracked at the seed boundary (the
        receive-side counterpart to the Go publish bound) — it is seeded onto
        the metadata Layers 2/4 key maps on, so the bound must hold here too,
        not only at publish. Absent, not truncated."""
        servicer, dispatcher = _make_servicer()
        await servicer.ReceiveChannelMessage(
            _channel_event(interaction_id="x" * 129),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = _enqueued_event(dispatcher)
        assert "interaction_id" not in event.metadata, (
            f"over-length id MUST fall back to untracked; got {event.metadata!r}"
        )

    async def test_at_max_length_interaction_id_seeded(self):
        """A value exactly at the byte cap is legitimate and must be seeded."""
        servicer, dispatcher = _make_servicer()
        at_cap = "x" * 128
        await servicer.ReceiveChannelMessage(
            _channel_event(interaction_id=at_cap),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = _enqueued_event(dispatcher)
        assert event.metadata.get("interaction_id") == at_cap, event.metadata
