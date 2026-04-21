"""
Persatrix Agent gRPC Servicers.

Contains gRPC servicer implementations used by AgentServer:
- AgentServiceServicer: ExecuteTask, HealthCheck, ExecuteTaskStream, SendChatMessage
- ChannelServiceServicer: inbound AgentMessage routing
- _extract_chat_reply: helper for extracting chat replies from agent actions
"""

# ruff: noqa: N802

import asyncio
import json
import logging
import re
import time
import uuid

import grpc
import grpc.aio

from .base import BaseAgent, TaskInput, TaskInputConfig, TaskOutput, TaskStatus
from .dispatch import EventDispatcher
from .generated import agent_message_pb2, agent_message_pb2_grpc, task_pb2, task_pb2_grpc
from .participant import validate_participant_type
from .persona_types import ActionType, AgentAction, AgentEvent, EventType

logger = logging.getLogger("Persatrix.agent.server")


# ─── AgentServiceServicer ───────────────────────────────────


class AgentServiceServicer(task_pb2_grpc.AgentServiceServicer):
    """gRPC servicer: ExecuteTask, HealthCheck, ExecuteTaskStream, SendChatMessage."""

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

        Builds a MESSAGE_RECEIVED AgentEvent, dispatches it with
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
            return task_pb2.ChatResponse(
                agent_id=agent_id,
                reply="",
                reply_status="error",
            )

        agent = self._agents.get(agent_id)
        if agent is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"Agent not found: {agent_id}")
            return task_pb2.ChatResponse(
                agent_id=agent_id,
                reply="",
                reply_status="error",
            )

        # Validate participant_type — default to "user" when empty (OQ 3).
        participant_type = request.participant_type or "user"
        try:
            validate_participant_type(participant_type)
        except ValueError as exc:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(str(exc))
            return task_pb2.ChatResponse(
                agent_id=agent_id,
                reply="",
                reply_status="error",
            )

        # Generate or reuse session_id (OQ 9).
        # Cap length to prevent oversized values propagating into metadata/
        # logs. (Review fix: defence-in-depth for client-supplied session_id.)
        if request.session_id and len(request.session_id) > 128:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("session_id exceeds 128 characters")
            return task_pb2.ChatResponse(
                agent_id=agent_id,
                reply="",
                reply_status="error",
            )
        session_id = request.session_id or str(uuid.uuid4())

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
            return task_pb2.ChatResponse(
                agent_id=agent_id,
                reply="",
                reply_status="error",
            )

        user_id = request.user_id

        # Validate user_id length — defence-in-depth against oversized
        # payloads propagating into events and memory (review fix).
        if len(user_id) > 256:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("user_id exceeds 256 characters")
            return task_pb2.ChatResponse(
                agent_id=agent_id,
                reply="",
                reply_status="error",
            )

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={
                "content": request.message,
                "user_id": user_id,
                "participant_type": participant_type,
            },
            sender_id=user_id or None,
            metadata={
                "session_id": session_id,
                "sender_participant_type": participant_type,
            },
        )

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
            return task_pb2.ChatResponse(
                agent_id=agent_id,
                session_id=session_id,
                reply="",
                reply_status="error",
            )
        except Exception:
            logger.exception("SendChatMessage failed for agent %s", agent_id)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details("Internal error")
            return task_pb2.ChatResponse(
                agent_id=agent_id,
                session_id=session_id,
                reply="",
                reply_status="error",
            )

        # Extract reply with priority: user-targeted SEND_MESSAGE → any
        # SEND_MESSAGE → COMPLETE_TASK → empty (OQ 5).
        reply, reply_status = _extract_chat_reply(actions, user_id)

        # Execute remaining actions (side-effects) after reply is secured.
        # Wrapped in try/except so the already-extracted reply is never lost
        # if a downstream action raises (review fix: two-phase guarantee).
        # cascade_depth=1 is correct because SendChatMessage is always a
        # top-level call (not invoked from within a nested dispatch).  If
        # this changes in the future, derive depth from the dispatch context.
        # (PR 6 review fix: PR 3 finding #3.)
        try:
            await self._dispatcher.executor.execute(
                agent_id, actions, cascade_depth=1,
            )
        except Exception:
            logger.warning(
                "Post-reply action execution failed for agent %s",
                agent_id, exc_info=True,
            )

        # Record human→agent interaction in relationship memory (OQ 11).
        if hasattr(agent, "memory") and hasattr(agent.memory, "relationship"):
            try:
                await agent.memory.relationship.record_interaction(
                    other_id=user_id or "unknown",
                    interaction_type="chat",
                    outcome=reply or None,
                    other_participant_type=participant_type,
                )
            except Exception:
                logger.warning(
                    "Failed to record chat interaction for agent %s with %s",
                    agent_id, user_id, exc_info=True,
                )

        return task_pb2.ChatResponse(
            reply=reply,
            session_id=session_id,
            agent_id=agent_id,
            timestamp=int(time.time()),
            agent_display_name="",  # orchestrator fills from Registry (OQ 8/15)
            reply_status=reply_status,
        )


# ─── Chat Reply Extraction ────────────────────────────────────


def _extract_chat_reply(
    actions: list[AgentAction],
    user_id: str,
) -> tuple[str, str]:
    """Extract a chat reply text from a list of agent actions.

    Priority (OQ 5):
    1. ``SEND_MESSAGE`` whose ``mentions`` list contains ``user_id``.
    2. Any ``SEND_MESSAGE`` action.
    3. ``COMPLETE_TASK`` result payload.
    4. Empty string (reply_status="empty").

    Returns ``(reply_text, reply_status)`` where ``reply_status`` is one of
    ``"ok"``, ``"empty"``.
    """
    def _sanitize_reply(text: str) -> str:
        """Strip internal delimiter tags that should never be visible to users.

        The persona runtime wraps user messages in ``<|user_message …|>`` /
        ``<|/user_message|>`` delimiters for prompt-injection mitigation.
        If the LLM echoes these back in its response, strip them so the
        raw markup never reaches the end user.
        """
        # Primary precise sweep: known ``user_message`` delimiter shapes.
        cleaned = re.sub(
            r"<\|/?user_message[^|]*\|>",
            "",
            text,
        )
        # Defense-in-depth: strip any other ``<|…|>`` token-like fragments
        # that might slip through if the runtime adds new delimiter names
        # (e.g. ``<|system|>``, ``<|assistant|>``) or if the LLM hallucinates
        # one.  Allows inner pipes (e.g. ``user_id="a|b"``) by using a
        # non-greedy body bounded to 128 chars to avoid catastrophically
        # eating real reply content that happens to contain ``|>``.
        cleaned = re.sub(
            r"<\|/?[a-zA-Z_].{0,128}?\|>",
            "",
            cleaned,
        )
        # Fallback: strip a torn opening fragment at the very end of the
        # string (no closing ``|>``), which can happen if the LLM cuts off
        # mid-tag.  Anchored to end-of-string so we don't touch legitimate
        # ``<|`` substrings elsewhere in the reply.
        cleaned = re.sub(
            r"<\|/?[a-zA-Z_][^|>\s]{0,64}\Z",
            "",
            cleaned,
        )
        return cleaned.strip()

    send_messages = [
        a for a in actions if a.action_type == ActionType.SEND_MESSAGE
    ]

    # Priority 1: user-targeted SEND_MESSAGE
    if user_id:
        for action in send_messages:
            mentions = action.payload.get("mentions", [])
            if user_id in mentions:
                return _sanitize_reply(action.payload.get("content", "")), "ok"

    # Priority 2: any SEND_MESSAGE
    if send_messages:
        return _sanitize_reply(send_messages[0].payload.get("content", "")), "ok"

    # Priority 3: COMPLETE_TASK result
    complete = next(
        (a for a in actions if a.action_type == ActionType.COMPLETE_TASK), None
    )
    if complete is not None:
        result = complete.payload.get("result", "")
        return _sanitize_reply(result), "ok"

    # Priority 4: empty — only warn when the agent returned actions but
    # none were reply-extractable; an empty action list is expected for
    # agents that legitimately produce no reply (review fix: log noise).
    if actions:
        logger.warning("SendChatMessage: no reply action found in agent response")
    return "", "empty"


# ─── ChannelServiceServicer ──────────────────────────────────


class ChannelServiceServicer(agent_message_pb2_grpc.ChannelServiceServicer):
    """Receives inbound AgentMessage and routes it to persona agents.

    Routes to agents listed in ``mentions``; if empty, broadcasts to all
    agents on this server. Returns delivered=True as soon as the event is
    queued — LLM processing happens asynchronously via the EventDispatcher.
    """

    def __init__(self, agents: dict[str, BaseAgent], dispatcher: EventDispatcher) -> None:
        self._agents = agents
        self._dispatcher = dispatcher
        # PR #101 review: Python 3.11+ asyncio docs warn that the event loop
        # only holds weak references to tasks, so a fire-and-forget
        # ``asyncio.create_task(...)`` can be garbage-collected mid-flight if
        # the caller does not retain a strong reference. SendMessage returns
        # immediately after queueing, which is exactly that hazard. Keep a
        # strong-ref set and drop tasks via a done-callback once they finish.
        self._pending_dispatches: set[asyncio.Task] = set()

    async def SendMessage(
        self,
        request: agent_message_pb2.AgentMessage,
        context: grpc.aio.ServicerContext,
    ) -> agent_message_pb2.SendMessageResponse:
        targets = list(request.mentions) if request.mentions else list(self._agents.keys())
        if not targets:
            return agent_message_pb2.SendMessageResponse(
                message_id=request.message_id,
                delivered=False,
            )
        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": request.content, "channel_id": request.channel_id},
            channel_id=request.channel_id or None,
            sender_id=request.sender_id or None,
            message_id=request.message_id or None,
        )
        for target_id in targets:
            task = asyncio.create_task(
                self._dispatch_and_log(target_id, event),
                name=f"channel-dispatch-{target_id}-{request.message_id or 'anon'}",
            )
            self._pending_dispatches.add(task)
            task.add_done_callback(self._pending_dispatches.discard)
        return agent_message_pb2.SendMessageResponse(
            message_id=request.message_id,
            delivered=True,
        )

    async def _dispatch_and_log(self, target_id: str, event: AgentEvent) -> None:
        """Wrapper around ``EventDispatcher.dispatch`` that logs failures.

        PR #101 review: fire-and-forget ``create_task`` surfaces exceptions only
        as ``Task exception was never retrieved`` warnings at GC time, which is
        easy to miss in production logs. Wrapping in a try/except at the task
        boundary ensures dispatch failures are recorded with enough context to
        correlate them back to the inbound message.
        """
        try:
            await self._dispatcher.dispatch(target_id, event)
        except Exception:
            logger.exception(
                "Channel dispatch to agent %s failed (message_id=%s, sender=%s)",
                target_id,
                event.message_id,
                event.sender_id,
            )

    async def Subscribe(
        self,
        request: agent_message_pb2.SubscribeRequest,
        context: grpc.aio.ServicerContext,
    ) -> None:
        # TODO(v0.3): implement server-side streaming channel subscriptions
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("Channel subscriptions not yet implemented")
