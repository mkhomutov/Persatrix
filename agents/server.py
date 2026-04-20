"""
Persatrix Agent gRPC Server.

Runs a single agent in a process, exposing it via gRPC for the orchestrator
to communicate with. Implements AgentServiceServicer (ExecuteTask, HealthCheck,
ExecuteTaskStream) from the generated protobuf stubs.
"""

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
import uuid

import aiohttp
import grpc
import grpc.aio

from .base import BaseAgent, TaskInput, TaskInputConfig, TaskOutput, TaskStatus
from .dispatch import EventDispatcher
from .generated import agent_message_pb2, agent_message_pb2_grpc, task_pb2, task_pb2_grpc
from .participant import validate_participant_type
from .persona_runtime import _LLMPersonaAgent
from .persona_types import ActionType, AgentEvent, EventType
from .server_persona import (
    initialize_persona_agents,
    load_agent,
)
from .tick import TickScheduler

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
        session_id = request.session_id or str(uuid.uuid4())

        # Clamp timeout: at least 1s, at most 300s, default 30s (OQ 6/13).
        raw_timeout = request.timeout_seconds or 30
        clamped_timeout = max(1, min(raw_timeout, 300))

        user_id = request.user_id

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={
                "content": request.message,
                "user_id": user_id,
                "participant_type": participant_type,
            },
            sender_id=user_id or None,
            metadata={"session_id": session_id},
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
        await self._dispatcher.executor.execute(
            agent_id, actions, cascade_depth=1,
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
    actions: list,
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
    send_messages = [
        a for a in actions if a.action_type == ActionType.SEND_MESSAGE
    ]

    # Priority 1: user-targeted SEND_MESSAGE
    if user_id:
        for action in send_messages:
            mentions = action.payload.get("mentions", [])
            if user_id in mentions:
                return action.payload.get("content", ""), "ok"

    # Priority 2: any SEND_MESSAGE
    if send_messages:
        return send_messages[0].payload.get("content", ""), "ok"

    # Priority 3: COMPLETE_TASK result
    complete = next(
        (a for a in actions if a.action_type == ActionType.COMPLETE_TASK), None
    )
    if complete is not None:
        result = complete.payload.get("result", "")
        return result, "ok"

    # Priority 4: empty
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


# ─── AgentServer ─────────────────────────────────────────────


class AgentServer:
    """gRPC server hosting one or more agents."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 50051,
        shutdown_grace: int = 30,
        orchestrator_url: str = "http://127.0.0.1:8080",
        advertise_address: str | None = None,
    ):
        self.host = host
        self.port = port
        self.shutdown_grace = shutdown_grace
        self.orchestrator_url = orchestrator_url.rstrip("/")
        # advertise_address is the address the orchestrator uses to reach this
        # agent via gRPC. Defaults to host:port (correct for local runs). In
        # Docker/K8s, pass the service name:port so the orchestrator can connect
        # back (e.g. "agent-planner:50051").
        self._advertise_address_explicit = advertise_address is not None
        self.advertise_address = advertise_address or f"{host}:{port}"
        self.agents: dict[str, BaseAgent] = {}
        self._server: grpc.aio.Server | None = None
        self._session: aiohttp.ClientSession | None = None
        self._dispatcher = EventDispatcher()
        self._tick_schedulers: dict[str, TickScheduler] = {}

    def register_agent(self, agent: BaseAgent) -> None:
        """Register an agent instance with the server."""
        # S-03: v0.1 uses module-level state for tool permissions
        # (builtin.permission_gate, builtin.path_validator), so loading
        # multiple agents silently overwrites the first agent's security
        # config.  Warn until per-agent DI is added in v0.2.
        if self.agents:
            logger.warning(
                "v0.1 supports single-agent-per-process; tool permissions "
                "apply to the last-loaded agent only"
            )
        self.agents[agent.agent_id] = agent
        logger.info("Registered agent: %s (%s)", agent.agent_id, agent.name)

    async def start(self) -> None:
        """Start the gRPC server."""
        self._server = grpc.aio.server()
        servicer = AgentServiceServicer(self.agents, self._dispatcher)
        task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, self._server)
        channel_servicer = ChannelServiceServicer(self.agents, self._dispatcher)
        agent_message_pb2_grpc.add_ChannelServiceServicer_to_server(
            channel_servicer, self._server
        )

        bind_address = f"{self.host}:{self.port}"
        # TODO(security): enable TLS for production gRPC
        # SF-05: store the actual port so logging is correct when port=0
        # (dynamic allocation) and future self-registration uses the real port.
        try:
            actual_port = self._server.add_insecure_port(bind_address)
        except (RuntimeError, OSError) as exc:
            # RuntimeError: grpc-internal bind failure.
            # OSError: address already in use (more common in practice).
            raise SystemExit(
                f"Failed to bind gRPC server to {bind_address} — "
                f"is port {self.port} already in use? ({exc})"
            ) from exc
        self.port = actual_port
        # When port=0 (dynamic allocation) and no explicit advertise_address was
        # provided, the default advertise_address still contains ":0".  Update it
        # to the actual allocated port so _self_register() advertises a reachable
        # address.  (PR #71 review finding §4.)
        # Uses _advertise_address_explicit to avoid clobbering an intentional
        # advertise address that happens to contain a different host (e.g.
        # --advertise-address=localhost:0 with --host=0.0.0.0).  The old check
        # compared against f"{self.host}:0" which missed that case.  (PR #71
        # deep-review §2.4.5.)
        if not self._advertise_address_explicit and self.advertise_address.endswith(":0"):
            self.advertise_address = f"{self.host}:{actual_port}"
        await self._server.start()

        logger.info("Agent server listening on %s:%d", self.host, actual_port)
        logger.info(
            "Serving %d agent(s): %s",
            len(self.agents),
            list(self.agents.keys()),
        )

        # Initialize memory, register with dispatcher, and start tick
        # schedulers for persona agents in a single pass.
        # (F-5b-9: consolidated three separate agent-iteration loops.)
        await initialize_persona_agents(
            self.agents, self._dispatcher, self._tick_schedulers,
        )

        # Deep-review D4: shared aiohttp session for self-registration and
        # http_request tool (via builtin.http_session).
        self._session = aiohttp.ClientSession()

        # Self-register with orchestrator after gRPC server is listening.
        await self._self_register()

    async def _self_register(self) -> None:
        """Register all hosted agents with the orchestrator (best-effort).

        POST /api/v1/agents/register with id, address, capabilities.
        Status is NOT sent — the orchestrator sets ``healthy`` on registration
        (review-fix P1).
        """
        if self._session is None:
            return
        for agent_id, agent in self.agents.items():
            address = self.advertise_address
            payload = {
                "id": agent_id,
                "name": agent.name,
                "address": address,
                "capabilities": agent.capabilities,
            }
            url = f"{self.orchestrator_url}/api/v1/agents/register"
            try:
                async with self._session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status in (200, 201):
                        logger.info(
                            "Registered agent %s with orchestrator at %s",
                            agent_id,
                            self.orchestrator_url,
                        )
                    elif resp.status == 409:
                        # Agent already registered (CONFLICT) — not an error,
                        # may happen on restart.
                        logger.info(
                            "Agent %s already registered with orchestrator",
                            agent_id,
                        )
                    else:
                        body = await resp.text()
                        logger.warning(
                            "Failed to register agent %s: HTTP %d — %s",
                            agent_id,
                            resp.status,
                            body[:200],
                        )
            except Exception:
                # Best-effort: log and continue serving even if orchestrator
                # is unreachable.
                logger.warning(
                    "Could not reach orchestrator at %s for agent %s registration",
                    self.orchestrator_url,
                    agent_id,
                )

    async def _self_deregister(self) -> None:
        """De-register all hosted agents from the orchestrator (best-effort).

        DELETE /api/v1/agents/{id}. Failure logged at WARNING (PR-review B5).
        """
        if self._session is None:
            return
        for agent_id in self.agents:
            url = f"{self.orchestrator_url}/api/v1/agents/{agent_id}"
            try:
                async with self._session.delete(
                    url, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status in (200, 204):
                        logger.info(
                            "De-registered agent %s from orchestrator",
                            agent_id,
                        )
                    else:
                        logger.warning(
                            "Failed to de-register agent %s: HTTP %d",
                            agent_id,
                            resp.status,
                        )
            except Exception:
                logger.warning(
                    "Could not reach orchestrator for agent %s de-registration",
                    agent_id,
                )

    async def stop(self) -> None:
        """Gracefully stop the server."""
        logger.info("Shutting down agent server...")
        # Stop tick schedulers first (before stopping gRPC)
        for agent_id, scheduler in self._tick_schedulers.items():
            try:
                await scheduler.stop()
                logger.info("Stopped tick scheduler for %s", agent_id)
            except Exception:
                logger.exception("Error stopping tick scheduler for %s", agent_id)
        self._tick_schedulers.clear()
        # De-register from orchestrator before stopping gRPC server.
        await self._self_deregister()
        if self._server:
            await self._server.stop(grace=self.shutdown_grace)
        # F-02: isolate per-agent shutdown errors so one agent's failure
        # doesn't prevent cleanup of remaining agents.
        for agent_id, agent in self.agents.items():
            try:
                # Close persona agent memory before generic shutdown
                if isinstance(agent, _LLMPersonaAgent):
                    await agent.close_memory()
                    logger.info("Closed memory for persona agent %s", agent_id)
                await agent.shutdown()
            except Exception:
                logger.exception("Error shutting down agent %s", agent_id)
        # Deep-review D4: close shared session after all agents are stopped.
        if self._session:
            await self._session.close()
            self._session = None
        logger.info("Agent server stopped.")


# ─── CLI + main ──────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Persatrix Agent Server")
    parser.add_argument("--agent", required=True, help="Agent ID to run")
    parser.add_argument("--port", type=int, default=50051, help="gRPC port")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (use 0.0.0.0 in containers)",
    )
    parser.add_argument(
        "--config",
        default="config/agents.yaml",
        help="Agent config path",
    )
    parser.add_argument(
        "--workspace",
        default="/workspace",
        help="Workspace root for path validation",
    )
    parser.add_argument(
        "--orchestrator-url",
        default="http://127.0.0.1:8080",
        help="Orchestrator REST API URL for self-registration",
    )
    parser.add_argument(
        "--advertise-address",
        default=None,
        help="gRPC address advertised to the orchestrator (host:port). "
             "Defaults to bind host:port. Set to Docker service name in containers.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    parser.add_argument(
        "--shutdown-grace",
        type=int,
        default=30,
        help="Graceful shutdown timeout in seconds",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    agent = load_agent(args.agent, args.config, args.workspace)
    server = AgentServer(
        host=args.host,
        port=args.port,
        shutdown_grace=args.shutdown_grace,
        orchestrator_url=args.orchestrator_url,
        advertise_address=args.advertise_address,
    )
    server.register_agent(agent)

    async def _run() -> None:
        shutdown = asyncio.Event()

        def request_shutdown():
            shutdown.set()

        # loop.add_signal_handler() is POSIX-only and raises NotImplementedError on Windows.
        if sys.platform != "win32":
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, request_shutdown)
        else:
            signal.signal(signal.SIGINT, lambda s, f: request_shutdown())
            # MF-01: SIGTERM is available on Windows in Python; registering a
            # handler makes os.kill(pid, SIGTERM) trigger graceful shutdown
            # instead of immediate process termination.
            signal.signal(signal.SIGTERM, lambda s, f: request_shutdown())

        await server.start()
        await shutdown.wait()
        await server.stop()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
