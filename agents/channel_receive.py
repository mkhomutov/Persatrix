"""Channel-message receive handler (RFC 0011 PR 4a, RFC 0024 Phase 4).

Houses the body of ``AgentServiceServicer.ReceiveChannelMessage`` as a
free function so ``agents/server_servicers.py`` shrinks towards the 500
lines at which ``scripts/checks/file_size.py`` warns (it fails a file over
800) — the same extraction as :mod:`agents.closed_interactions_read`.
Moved when ISSUE-0205 gave the handler a second refusal; the servicer was
already over 500.

The handler refuses a message in a ``TaskAck`` body, never with a gRPC
status code, and each refusal says its own cause.  The orchestrator does
not retry on a ``TaskAck``; it logs the text.
"""

from __future__ import annotations

import logging

import grpc
import grpc.aio

from .base import BaseAgent
from .channel_validation import validate_channel_message_event
from .channel_wire_metadata import channel_event_payload, seed_wire_metadata
from .clock import to_agent_time
from .dispatch import EventDispatcher
from .epoch_id import EVENT_EPOCH_METADATA_KEY
from .generated import task_pb2
from .persona_types import AgentEvent, EventType
from .principal_id import EVENT_PRINCIPAL_METADATA_KEY
from .session_id import EVENT_SESSION_METADATA_KEY
from .session_metadata import (
    _epoch_from_context,
    _principal_from_context,
    _session_from_context,
)

__all__ = ["NO_DISPATCH_TARGET", "RECEIVER_OVERLOADED", "handle_receive_channel_message"]

logger = logging.getLogger("Persatrix.agent.channel_receive")

#: The target did not take the event: its event queue is full
#: (discard-not-block, RFC 0024 Decided §1), or its event loop is stopping
#: and takes nothing more.  The text names the first; the second is seen
#: only while an agent shuts down.
RECEIVER_OVERLOADED = (
    "receiver overloaded: agent event-loop queue full "
    "(discard-not-block backpressure)"
)

#: The hosted agent has no dispatch target, so no queue was ever involved
#: (ISSUE-0205, ``EventDispatcher.has_inbound_target``).  A persona is
#: given one once its memory is open; a task agent never has one.
NO_DISPATCH_TARGET = (
    "agent {agent_id} takes no channel messages: it has no event dispatch "
    "target on this server (a persona agent gets one once its memory is "
    "open; a task agent has none)"
)


async def handle_receive_channel_message(
    agents: dict[str, BaseAgent],
    dispatcher: EventDispatcher,
    request: task_pb2.ChannelMessageEvent,
    context: grpc.aio.ServicerContext,
) -> task_pb2.TaskAck:
    """Validate a channel message and enqueue it for the hosted agent.

    The contract is on ``AgentServiceServicer.ReceiveChannelMessage``,
    which calls this.
    """
    # ─── Validation (defence-in-depth; mirrors proto/task.proto bounds) ──
    # Validator returns ``(error, parsed_timestamp)`` so the RFC 3339
    # parse happens exactly once. PR #248 deep review M finding
    # (single-source-of-truth + assert-stripped-under-O elimination).
    err, publish_ts = validate_channel_message_event(request)
    if err is not None:
        return task_pb2.TaskAck(success=False, error_message=err)
    if publish_ts is None:
        # Defensive: the validator's tuple contract guarantees that a
        # ``None`` error implies a parsed timestamp. This branch is
        # unreachable barring an internal contract violation, but is
        # an explicit guard rather than a bare ``assert`` so the
        # invariant survives ``python -O`` (which strips asserts).
        return task_pb2.TaskAck(
            success=False,
            error_message="internal: validator returned no timestamp",
        )

    # ─── Target agent resolution (single-agent-per-process in v0.3.0) ──
    if not agents:
        return task_pb2.TaskAck(
            success=False,
            error_message="no agents registered on this server",
        )
    if len(agents) > 1:
        # `ChannelMessageEvent` carries no recipient_id; multi-agent
        # disambiguation requires an additive proto field landing
        # alongside the chat-path migration in RFC 0011 PR 4a-ii
        # (see docs/rfcs/0011-pr-plan.md). Until then a multi-agent
        # server cannot route channel messages unambiguously and MUST
        # reject rather than broadcast.
        return task_pb2.TaskAck(
            success=False,
            error_message=(
                "multi-agent server: ChannelMessageEvent has no "
                "recipient_id field; deferred to RFC 0011 PR 4a-ii"
            ),
        )
    target_agent_id = next(iter(agents))

    # ─── Build AgentEvent and enqueue fire-and-forget ───────────────
    # Propagate the orchestrator-authored RFC 3339 ``timestamp`` rather
    # than re-stamping with ``time.time()`` — preserves publish-time
    # ordering for cross-agent correlation and replay. ``publish_ts``
    # is the validator's parsed value; the validator's tuple-return
    # contract pins "validation succeeded ⇒ timestamp is float" in
    # the type system, so no runtime assert is needed (asserts are
    # stripped under ``python -O``).
    event = AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        # Gate + salience-bid + (v0.3.8) floor-mentions decision inputs,
        # lifted off the wire shape in the carve-out module — see
        # ``channel_wire_metadata.channel_event_payload``.
        payload=channel_event_payload(request),
        channel_id=request.channel_id,
        sender_id=request.sender_id,
        message_id=request.message_id,
        thread_id=request.thread_id or None,
        # Moved into agent time, which memory reads (agents.clock).
        timestamp=to_agent_time(publish_ts),
        # Seed cascade_depth from the typed proto field (RFC 0011
        # cascade-depth wire-propagation amendment, PR 3) so the dispatcher
        # sees the wire value instead of resetting to zero on every hop.
        # Receiver-side advisory: the Go orchestrator clamps on outbound.
        metadata={"cascade_depth": request.cascade_depth},
    )

    # Lift the typed wire fields (sender peer type — ISSUE-0068, which has
    # a real producer; RFC 0020 interaction_id — RFC 0030 governance layers
    # PR 1, no producer yet) onto the event-metadata keys the downstream
    # read paths consume. See ``channel_wire_metadata.seed_wire_metadata``.
    seed_wire_metadata(event, request)

    # ISSUE-0081 PR 2: carry the per-request session through to the
    # deferred EventLoop drain.  The fire-and-forget path processes the
    # event in a different (boot-created) task, so the scope cannot ride
    # a ContextVar set here — it must travel on the event envelope and
    # be re-entered inside ``on_event`` when the loop drains.
    request_session = _session_from_context(context)
    if request_session is not None:
        event.metadata[EVENT_SESSION_METADATA_KEY] = request_session
    # ISSUE-0081 PR 3: the tenant/principal rides the same envelope.
    request_principal = _principal_from_context(context)
    if request_principal is not None:
        event.metadata[EVENT_PRINCIPAL_METADATA_KEY] = request_principal
    # ISSUE-0085 PR 4: the per-process epoch rides the same envelope so
    # the deferred EventLoop drain re-enters the run-isolation scope.
    request_epoch = _epoch_from_context(context)
    if request_epoch is not None:
        event.metadata[EVENT_EPOCH_METADATA_KEY] = request_epoch

    # ISSUE-0205: an agent with no dispatch target takes no events here.
    # Say so, before the enqueue: the dispatcher answers ``False`` for this
    # and for a full queue alike, and the caller was told "overloaded" for
    # both.  A task agent is the case that remains; a persona whose memory
    # did not open no longer gets this far (the process stops at start-up).
    if not dispatcher.has_inbound_target(target_agent_id):
        logger.warning(
            "Refused a channel message on %s for %s: "
            "the agent has no event dispatch target on this server",
            request.channel_id, target_agent_id,
        )
        return task_pb2.TaskAck(
            success=False,
            error_message=NO_DISPATCH_TARGET.format(agent_id=target_agent_id),
        )

    # Fire-and-forget: enqueue onto the agent's EventLoop and return
    # immediately. The loop owns decide → execute → recover when it
    # drains (RFC 0024 Phase 4). With a target, ``enqueue_inbound``
    # returns ``False`` when the receiver is full (the loop's bounded queue,
    # or the in-flight set of an agent with no loop: discard-not-block) and
    # when the agent's event loop is stopping.
    accepted = dispatcher.enqueue_inbound(target_agent_id, event)

    # ``context`` is read above for the session header but is never
    # used to set a gRPC status code: the ack is conveyed in the
    # ``TaskAck`` body, never via a status code. Other rejection
    # branches above also do not touch ``context``.
    if not accepted:
        return task_pb2.TaskAck(success=False, error_message=RECEIVER_OVERLOADED)
    return task_pb2.TaskAck(success=True)

