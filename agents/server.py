"""
Persatrix Agent gRPC Server.

Runs a single agent in a process, exposing it via gRPC for the orchestrator
to communicate with. Implements AgentServiceServicer (ExecuteTask, HealthCheck,
ExecuteTaskStream) from the generated protobuf stubs.
"""

import argparse
import asyncio
import logging
import signal
import sys

import aiohttp
import grpc
import grpc.aio
from opentelemetry.instrumentation.grpc import GrpcAioInstrumentorServer

from .base import BaseAgent
from .dispatch import EventDispatcher
from .generated import agent_message_pb2_grpc, task_pb2_grpc
from .observability.grpc_logging import LoggingMetadataInterceptor
from .observability.logging import configure_logging
from .observability.tracing import init_tracing
from .observability.tracing import shutdown as tracing_shutdown
from .persona_runtime import _LLMPersonaAgent
from .server_persona import (
    initialize_persona_agents,
    load_agent,
)
from .server_servicers import (  # noqa: F401
    AgentServiceServicer,
    ChannelServiceServicer,
    _extract_chat_reply,
)
from .tick import TickScheduler

logger = logging.getLogger("Persatrix.agent.server")


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
        # RFC 0018 Phase 3 — register the LoggingMetadataInterceptor on the
        # gRPC server.  Order matters: GrpcAioInstrumentorServer (RFC 0019
        # Phase 1, applied globally in main()) installs the OTEL trace
        # context for incoming RPCs *before* per-server interceptors run, so
        # by the time LoggingMetadataInterceptor's wrapped handler executes
        # both the OTEL span and the structlog contextvars are bound.  This
        # gives log records inside the handler all six correlation fields:
        # the four IDs from the metadata interceptor and the trace_id /
        # span_id from the OTEL processor.
        self._server = grpc.aio.server(interceptors=[LoggingMetadataInterceptor()])
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

    # RFC 0018 Phase 1 — structured logging via structlog + ProcessorFormatter
    # bridge.  Replaces the prior ``logging.basicConfig`` call so all records
    # (including those from third-party libs that still emit through stdlib
    # ``logging``) flow through the schema chain documented in
    # ``docs/observability.md``.  Existing call sites that use
    # ``logging.getLogger(__name__)`` continue to work — they hit the
    # ``foreign_pre_chain`` and are rendered with the same JSON schema.
    # The mechanical ``getLogger`` -> ``get_logger`` swap and printf -> kwargs
    # migration ship in a follow-up PR (RFC 0018 PR 1b).
    configure_logging(
        service_kind="agent",
        service_instance=args.agent,
        level=args.log_level,
    )

    # Initialise OTEL tracing before any gRPC or async code starts.
    init_tracing()
    GrpcAioInstrumentorServer().instrument()

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
        await tracing_shutdown()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
