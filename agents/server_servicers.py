"""
Persatrix Agent gRPC Servicers.

Contains gRPC servicer implementations used by AgentServer:
- AgentServiceServicer: ExecuteTask, HealthCheck, ExecuteTaskStream,
  SendChatMessage, ReceiveChannelMessage, GetClosedInteractions

Chat-reply extraction (``_extract_chat_reply``) lives in
``agents/chat_reply.py`` since RFC 0011 PR 4a-i and is re-exported here
for backward compatibility with existing import sites.
"""

# ruff: noqa: N802

import asyncio
import json
import logging
import time
import uuid

import grpc
import grpc.aio

from .base import BaseAgent, TaskInput, TaskInputConfig, TaskOutput, TaskStatus
from .channel_validation import validate_channel_message_event
from .channel_wire_metadata import (
    DispatchContext,
    channel_event_payload,
    seed_wire_metadata,
)
from .chat_reply import chat_error_response as _chat_error_response
from .chat_reply import extract_chat_reply as _extract_chat_reply
from .closed_interactions_read import handle_get_closed_interactions
from .dispatch import EventDispatcher
from .epoch_id import EVENT_EPOCH_METADATA_KEY
from .generated import task_pb2, task_pb2_grpc
from .participant import validate_participant_type
from .persona_types import AgentEvent, EventType
from .principal_id import EVENT_PRINCIPAL_METADATA_KEY
from .session_id import EVENT_SESSION_METADATA_KEY
from .session_metadata import (
    _epoch_from_context,
    _principal_from_context,
    _session_from_context,
)
from .wallet_client import BudgetExceededError

logger = logging.getLogger("Persatrix.agent.server")


# ─── AgentServiceServicer ───────────────────────────────────


class AgentServiceServicer(task_pb2_grpc.AgentServiceServicer):
    """gRPC servicer: ExecuteTask, HealthCheck, ExecuteTaskStream,
    SendChatMessage, ReceiveChannelMessage (RFC 0011 PR 4a),
    GetClosedInteractions (v0.3.8 interaction-summary surface).
    """

    def __init__(self, agents: dict[str, BaseAgent], dispatcher: EventDispatcher | None = None):
        self._agents = agents
        self._dispatcher = dispatcher or EventDispatcher()

    async def ExecuteTask(
        self,
        request: task_pb2.TaskRequest,
        context: grpc.aio.ServicerContext,
    ) -> task_pb2.TaskResponse:
        agent = self._agents.get(request.agent_id)
        if agent is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"Agent not found: {request.agent_id}")
            return task_pb2.TaskResponse(
                task_id=request.task_id,
                status=task_pb2.FAILED,
                error_message=f"Agent not found: {request.agent_id}",
            )

        task_input = TaskInput(
            task_id=request.task_id,
            workflow_id=request.workflow_id,
            payload=request.payload,
            context=dict(request.context),
            config=TaskInputConfig(
                max_llm_calls=request.config.max_llm_calls or 0,
                max_tokens=request.config.max_tokens or 0,
                allowed_tools=list(request.config.allowed_tools),
            ),
        )

        try:
            start = time.monotonic()
            timeout = request.config.timeout_seconds or None
            output: TaskOutput = await asyncio.wait_for(
                agent.handle(task_input),
                timeout=timeout,
            )
            duration_ms = int((time.monotonic() - start) * 1000)

            # Review-fix P2: use json.dumps for non-str metadata values.
            metadata: dict[str, str] = {}
            for k, v in output.metadata.items():
                metadata[k] = v if isinstance(v, str) else json.dumps(v)
            metadata["duration_ms"] = str(duration_ms)

            return task_pb2.TaskResponse(
                task_id=request.task_id,
                status=(
                    task_pb2.COMPLETED
                    if output.status == TaskStatus.COMPLETED
                    else task_pb2.FAILED
                ),
                result=output.result,
                metadata=metadata,
                error_message=(
                    "" if output.status == TaskStatus.COMPLETED else output.result
                ),
            )
        except TimeoutError:
            context.set_code(grpc.StatusCode.DEADLINE_EXCEEDED)
            context.set_details("Task execution timed out")
            return task_pb2.TaskResponse(
                task_id=request.task_id,
                status=task_pb2.FAILED,
                error_message="Task execution timed out",
            )
        except Exception:
            logger.exception("Task execution failed: %s", request.task_id)
            # S-01: return fixed string — type(exc).__name__ leaked internal
            # implementation details (e.g. "SSLError", "ConnectionResetError").
            # Full exception details are already logged above for debugging.
            return task_pb2.TaskResponse(
                task_id=request.task_id,
                status=task_pb2.FAILED,
                error_message="Internal error",
            )

    async def HealthCheck(
        self,
        request: task_pb2.HealthCheckRequest,
        context: grpc.aio.ServicerContext,
    ) -> task_pb2.HealthCheckResponse:
        # PR-review B4: delegate to agent.health_check()
        if request.service:
            agent = self._agents.get(request.service)
            if agent is None:
                # F-01: unknown agent ID must return NOT_SERVING so the
                # orchestrator doesn't route tasks to a non-existent agent.
                # N-01: log at debug level to help operators diagnose routing
                # issues when the orchestrator probes a non-loaded agent.
                logger.debug(
                    "HealthCheck for unknown agent: %s", request.service
                )
                return task_pb2.HealthCheckResponse(
                    status=task_pb2.NOT_SERVING
                )
            healthy = await agent.health_check()
        else:
            healthy = len(self._agents) > 0
        return task_pb2.HealthCheckResponse(
            status=task_pb2.SERVING if healthy else task_pb2.NOT_SERVING,
        )

    async def ExecuteTaskStream(
        self,
        request: task_pb2.TaskRequest,
        context: grpc.aio.ServicerContext,
    ) -> None:
        # TODO(v0.2): implement streaming execution with TaskProgress messages
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("Streaming execution not implemented in v0.1")

    async def SendChatMessage(
        self,
        request: task_pb2.ChatRequest,
        context: grpc.aio.ServicerContext,
    ) -> task_pb2.ChatResponse:
        """Handle a synchronous chat message from a human participant.

        Builds a CHANNEL_MESSAGE AgentEvent, dispatches it with
        ``execute_actions=False`` to extract the reply before firing
        side-effects, then executes remaining actions. Records the
        interaction in relationship memory (OQ 11). (RFC 0016, PR 3)
        """
        agent_id = request.agent_id

        # Early check: empty agent_id is a client error (INVALID_ARGUMENT),
        # not a "not found" condition. (PR 6 review fix: PR 3 finding #2.)
        if not agent_id:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("agent_id is required")
            return _chat_error_response(agent_id)

        agent = self._agents.get(agent_id)
        if agent is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"Agent not found: {agent_id}")
            return _chat_error_response(agent_id)

        # Validate participant_type — default to "user" when empty (OQ 3).
        participant_type = request.participant_type or "user"
        try:
            validate_participant_type(participant_type)
        except ValueError as exc:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(str(exc))
            return _chat_error_response(agent_id)

        # Generate or reuse the chat-session id (RFC 0016, OQ 9). Wire field
        # was `session_id` pre-v0.3.1; renamed for RFC 0031 OQ #8. Cap
        # length to prevent oversized values propagating into metadata/logs.
        if request.chat_session_id and len(request.chat_session_id) > 128:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("chat_session_id exceeds 128 characters")
            return _chat_error_response(agent_id)
        session_id = request.chat_session_id or str(uuid.uuid4())

        # Clamp timeout: at least 1s, at most 300s, default 30s (OQ 6/13).
        # `or 30` treats protobuf zero-default as "use server default".
        # Negative int32 values are truthy, so they pass through to the
        # clamp where max(1, ...) normalises them to the 1s minimum.
        raw_timeout = request.timeout_seconds or 30
        clamped_timeout = max(1, min(raw_timeout, 300))

        # Validate message length — defence-in-depth against oversized
        # payloads propagating through event dispatch into the LLM client.
        # 32 768 chars is generous for a single chat message and aligned with
        # typical LLM context-window limits. (Review fix: DoS via multi-MB message.)
        if len(request.message) > 32768:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("message exceeds 32768 characters")
            return _chat_error_response(agent_id)

        user_id = request.user_id

        # Validate user_id length — defence-in-depth against oversized
        # payloads propagating into events and memory (review fix).
        if len(user_id) > 256:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("user_id exceeds 256 characters")
            return _chat_error_response(agent_id)

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={
                "content": request.message,
                "user_id": user_id,
                "participant_type": participant_type,
            },
            sender_id=user_id or None,
            metadata={
                "chat_session_id": session_id,  # RFC 0031 OQ #8
                "sender_participant_type": participant_type,
            },
        )

        # ISSUE-0081 PR 2: bind the orchestrator-authored per-request
        # session onto the event so ``on_event`` enters a ``session_scope``
        # for the handler.  Absent header → key omitted → handler runs
        # under its construction snapshot (single-session / legacy path).
        request_session = _session_from_context(context)
        if request_session is not None:
            event.metadata[EVENT_SESSION_METADATA_KEY] = request_session
        # ISSUE-0081 PR 3: same rail for the tenant/principal axis.
        request_principal = _principal_from_context(context)
        if request_principal is not None:
            event.metadata[EVENT_PRINCIPAL_METADATA_KEY] = request_principal
        # ISSUE-0085 PR 4: the per-process epoch rides the same rail so
        # ``on_event`` enters an ``epoch_scope`` for run/test isolation.
        request_epoch = _epoch_from_context(context)
        if request_epoch is not None:
            event.metadata[EVENT_EPOCH_METADATA_KEY] = request_epoch

        try:
            # dispatch(execute_actions=False) returns actions without firing
            # side-effects so we can extract the reply first (OQ 5/7).
            actions = await asyncio.wait_for(
                self._dispatcher.dispatch(agent_id, event, execute_actions=False),
                timeout=clamped_timeout,
            )
        except TimeoutError:
            context.set_code(grpc.StatusCode.DEADLINE_EXCEEDED)
            context.set_details("Chat dispatch timed out")
            return _chat_error_response(agent_id, chat_session_id=session_id)
        except BudgetExceededError as exc:
            # RFC 0023 § F — wallet denied (or unreachable). gRPC status
            # stays OK; the structured denial rides in reply_status/reply.
            logger.warning(
                "SendChatMessage budget-denied for agent %s (%s): %s",
                agent_id, exc.scope or exc.reason, exc.message,
            )
            return _chat_error_response(
                agent_id, chat_session_id=session_id, reply=exc.message,
            )
        except Exception:
            logger.exception("SendChatMessage failed for agent %s", agent_id)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details("Internal error")
            return _chat_error_response(agent_id, chat_session_id=session_id)

        # Extract reply with priority: user-targeted SEND_CHANNEL_MESSAGE → any
        # SEND_CHANNEL_MESSAGE → COMPLETE_TASK → empty (OQ 5).
        reply, reply_status = _extract_chat_reply(actions, user_id)

        # Execute remaining actions (side-effects) after reply is secured.
        # Wrapped in try/except so the already-extracted reply is never lost
        # if a downstream action raises (review fix: two-phase guarantee).
        # cascade_depth=1 is correct because SendChatMessage is always a
        # top-level call (not invoked from within a nested dispatch).  If
        # this changes in the future, derive depth from the dispatch context.
        # (PR 6 review fix: PR 3 finding #3.)  Origin-less by design: the
        # chat surface has no channel-dispatched event to echo as the RFC
        # 0052 no-reopen claim.
        try:
            await self._dispatcher.executor.execute(
                agent_id, actions, context=DispatchContext(cascade_depth=1),
            )
        except Exception:
            logger.warning(
                "Post-reply action execution failed for agent %s",
                agent_id, exc_info=True,
            )

        # RFC 0020 PR 4: per-event ``record_interaction`` removed.
        # Relationship-row bumps now happen once per closed interaction
        # in :meth:`_StatePersistenceMixin._persist_closed_interaction`,
        # so ``interaction_count`` reflects N closed interactions rather
        # than N inbound chat events.

        return task_pb2.ChatResponse(
            reply=reply,
            chat_session_id=session_id,
            agent_id=agent_id,
            timestamp=int(time.time()),
            agent_display_name="",  # orchestrator fills from Registry (OQ 8/15)
            reply_status=reply_status,
        )

    async def ReceiveChannelMessage(
        self,
        request: task_pb2.ChannelMessageEvent,
        context: grpc.aio.ServicerContext,
    ) -> task_pb2.TaskAck:
        """Receive a channel message and enqueue it for the local agent.

        RFC 0011 PR 4a shipped the receiver-side handler; RFC 0024 Phase 4
        inverts its dispatch shape. Validates the wire-side
        ``ChannelMessageEvent`` (mentions cap, content size, channel-type
        prefix agreement, thread_id length, sender_id pattern, RFC 3339
        timestamp, channel/message id length), resolves the target agent
        on the single-agent-per-process server (``agents/server.py``
        does not enforce single-agent today; ``register_agent`` only
        warns and overwrites — this handler enforces it for the channels
        surface), constructs an ``AgentEvent(event_type=CHANNEL_MESSAGE)``,
        and enqueues it **fire-and-forget** onto the agent's per-agent
        :class:`~agents.event_loop.EventLoop` via
        ``EventDispatcher.enqueue_inbound``. The loop owns processing
        (decide → execute → recover, incl. the ISSUE-0065/0066 chat-error
        recovery) when it drains; the handler returns ``TaskAck`` as soon
        as the wake is accepted. No ``scheduler.wake()`` call and no
        ``SyncDispatchHandle`` on the channel path — the orchestrator's
        only consumer of the return value (the ``TaskAck``) does not need
        the agent's actions (RFC 0024 §E).

        Validation failures return ``TaskAck(success=False, error_message=...)``
        with a taxonomised reason so operators reading the wire trace can
        locate the failure class. A full event-loop queue likewise returns
        ``success=False`` (discard-not-block backpressure, RFC 0024
        Decided §1). The orchestrator does not retry per ``TaskAck``
        semantics.
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
        if not self._agents:
            return task_pb2.TaskAck(
                success=False,
                error_message="no agents registered on this server",
            )
        if len(self._agents) > 1:
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
        target_agent_id = next(iter(self._agents))

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
            timestamp=publish_ts,
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

        # Fire-and-forget: enqueue onto the agent's EventLoop and return
        # immediately. The loop owns decide → execute → recover when it
        # drains (RFC 0024 Phase 4). ``enqueue_inbound`` returns ``False``
        # only when the loop's bounded queue is full (discard-not-block).
        accepted = self._dispatcher.enqueue_inbound(target_agent_id, event)

        # ``context`` is read above for the session header but is never
        # used to set a gRPC status code: the ack is conveyed in the
        # ``TaskAck`` body, never via a status code. Other rejection
        # branches above also do not touch ``context``.
        if not accepted:
            return task_pb2.TaskAck(
                success=False,
                error_message=(
                    "receiver overloaded: agent event-loop queue full "
                    "(discard-not-block backpressure)"
                ),
            )
        return task_pb2.TaskAck(success=True)

    async def GetClosedInteractions(
        self,
        request: task_pb2.ClosedInteractionsRequest,
        context: grpc.aio.ServicerContext,
    ) -> task_pb2.ClosedInteractionsResponse:
        """Read closed-interaction summaries (v0.3.8 surface); thin seam over
        ``closed_interactions_read.handle_get_closed_interactions``."""
        return await handle_get_closed_interactions(self._agents, request, context)


# ``_extract_chat_reply`` is re-exported (PR 4a-i) for back-compat with
# ``agents/server.py`` and ``test_extract_chat_reply.py``; module-private.
__all__ = ["AgentServiceServicer"]

