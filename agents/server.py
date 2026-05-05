"""
Persatrix Agent gRPC Server.

Runs a single agent in a process, exposing it via gRPC for the orchestrator
to communicate with. Implements AgentServiceServicer (ExecuteTask, HealthCheck,
ExecuteTaskStream, SendChatMessage, ReceiveChannelMessage) from the generated
protobuf stubs.
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
from .channel_publisher import HTTPChannelPublisher
from .dispatch import EventDispatcher
from .generated import task_pb2_grpc
from .memory import SharedPoolRegistry
from .observability.grpc_logging import LoggingMetadataInterceptor
from .observability.log_shipper import (
    Shipper,
    queue_capacity_from_env,
    set_active_shipper,
)
from .observability.logging import configure_logging
from .observability.metrics import init_metrics, try_get_instruments
from .observability.metrics import shutdown as metrics_shutdown
from .observability.tracing import init_tracing
from .observability.tracing import shutdown as tracing_shutdown
from .persona_runtime import _LLMPersonaAgent
from .server_persona import (
    initialize_persona_agents,
    load_agent,
    setup_shared_pools,
    stop_shared_pools,
)
from .server_servicers import (  # noqa: F401
    AgentServiceServicer,
    _extract_chat_reply,
)
from .tick import TickScheduler

logger = logging.getLogger("Persatrix.agent.server")


def _default_grpc_target(orchestrator_url: str) -> str:
    """Derive the default gRPC target from the orchestrator REST URL.

    Strips the URL scheme + path and replaces the (REST, default 8080)
    port with the canonical orchestrator gRPC port (9090).  Matches the
    docker-compose service layout where the same host serves both
    REST and gRPC, so a single ``--orchestrator-url`` argument is
    sufficient for the common case.  Operators with a non-standard
    layout pass ``--orchestrator-grpc=<host:port>`` explicitly.
    """
    from urllib.parse import urlparse

    parsed = urlparse(orchestrator_url)
    host = parsed.hostname or "127.0.0.1"
    return f"{host}:9090"


class AgentServer:
    """gRPC server hosting one or more agents."""
    _shared_pools: SharedPoolRegistry | None = None  # RFC 0008 PR 4; set by main()

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 50051,
        shutdown_grace: int = 30,
        orchestrator_url: str = "http://127.0.0.1:8080",
        advertise_address: str | None = None,
        orchestrator_grpc: str | None = None,
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
        # gRPC target for the orchestrator-side LogService (RFC 0018 PR 5).
        # Defaults to the orchestrator REST host on the canonical 9090 port;
        # operators in containerised deployments override via
        # --orchestrator-grpc=<service>:9090.
        self.orchestrator_grpc = orchestrator_grpc or _default_grpc_target(
            self.orchestrator_url,
        )
        self.agents: dict[str, BaseAgent] = {}
        self._server: grpc.aio.Server | None = None
        self._session: aiohttp.ClientSession | None = None
        self._dispatcher = EventDispatcher()
        self._tick_schedulers: dict[str, TickScheduler] = {}
        self._log_shipper: Shipper | None = None

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
            self.agents, self._dispatcher, self._tick_schedulers, shared_pools=self._shared_pools,
        )

        # RFC 0008 PR 2: open MemoryFacade for opt-in non-persona task agents.
        for agent_id, agent in self.agents.items():
            if isinstance(agent, _LLMPersonaAgent):
                continue
            try:
                await agent.initialize_memory(shared_pools=self._shared_pools)
            except Exception:
                logger.exception(
                    "Failed to init memory for task agent %s", agent_id,
                )

        # Deep-review D4: shared aiohttp session for self-registration and
        # http_request tool (via builtin.http_session). RFC 0011 PR 4a-ii-β-1
        # wires the REST channel publisher onto it (chat-reply path keeps
        # using the in-process cascade until PR 4a-ii-β-2).
        self._session = aiohttp.ClientSession()
        self._dispatcher.set_channel_publisher(HTTPChannelPublisher(
            orchestrator_url=self.orchestrator_url, session=self._session,
        ))

        # RFC 0018 PR 5 — start the log shipper after the structlog chain
        # is configured (configure_logging runs in main()) so the tail
        # processor's first record (typically "Agent server listening")
        # already has somewhere to enqueue.  The agent_id used for the
        # batch-level field is the first registered agent (v0.1 hosts a
        # single agent per process); a future multi-agent process would
        # set per-entry agent_id on the structlog contextvars.
        first_agent_id = next(iter(self.agents.keys()), "unknown")
        self._log_shipper = Shipper(
            self.orchestrator_grpc,
            first_agent_id,
            max_queue=queue_capacity_from_env(),
        )
        await self._log_shipper.start()
        set_active_shipper(self._log_shipper)

        # Self-register with orchestrator after gRPC server is listening.
        # PR #173 review fix: a duplicate self-register call was introduced
        # alongside the shipper-start block, causing every agent to POST
        # /api/v1/agents/register twice on startup.
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
                # Close persona-agent three-tier memory or task-agent
                # MemoryFacade (RFC 0008 PR plan PR 2).  ``close_memory``
                # is a no-op when no facade was opened.  PR 2a follow-up
                # L3: single call site \u2014 the persona/task distinction is
                # only relevant for logging.
                await agent.close_memory()
                if isinstance(agent, _LLMPersonaAgent):
                    logger.info("Closed memory for persona agent %s", agent_id)
                await agent.shutdown()
            except Exception:
                logger.exception("Error shutting down agent %s", agent_id)
        await stop_shared_pools(self._shared_pools)  # RFC 0008 PR 4
        # Deep-review D4: close shared session after all agents are stopped.
        if self._session:
            await self._session.close()
            self._session = None
        # Stop the log shipper last so the agent's own teardown logs
        # are still drained to the orchestrator (best-effort within
        # the configured shutdown timeout).
        if self._log_shipper is not None:
            await self._log_shipper.stop()
            self._log_shipper = None
        logger.info("Agent server stopped.")


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
        "--orchestrator-grpc",
        default=None,
        help="Orchestrator gRPC target for the LogService stream (host:port). "
             "Defaults to the orchestrator REST host on port 9090.",
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
    # Initialise metrics alongside tracing so instrument handles exist before
    # the first LLM / tool / event call site records.  Any failure here is
    # tolerated — the recording helpers are nil-safe.
    try:
        init_metrics()
    except Exception:  # pragma: no cover — startup resilience
        logger.exception(
            "failed to initialize OTEL metrics, continuing without metric recording",
        )
        # PR-170 N3: a partial init (provider constructed, ``_Instruments``
        # ctor raised) leaves the module-level provider live and exporting
        # empty payloads on its periodic interval.  Tear it down inline
        # (sync — no event loop yet) so we do not leak a background
        # exporter for the lifetime of the process.  Done via a direct
        # provider-handle reset rather than ``metrics.shutdown()`` (which
        # is async and requires an event loop) to keep this in the
        # synchronous startup path.
        from agents.observability import metrics as _pmetrics

        if _pmetrics._provider is not None:
            try:
                _pmetrics._provider.force_flush()
                _pmetrics._provider.shutdown()
            except Exception:  # pragma: no cover — best-effort cleanup
                logger.exception("failed to clean up partial metrics provider")
            _pmetrics._provider = None
            _pmetrics._instruments = None
    GrpcAioInstrumentorServer().instrument()

    agent = load_agent(args.agent, args.config, args.workspace)
    server = AgentServer(
        host=args.host,
        port=args.port,
        shutdown_grace=args.shutdown_grace,
        orchestrator_url=args.orchestrator_url,
        advertise_address=args.advertise_address,
        orchestrator_grpc=args.orchestrator_grpc,
    )
    server.register_agent(agent)
    setup_shared_pools(server, args.config, agent)  # RFC 0008 PR 4

    # PR-170 M2: bump the ``agent.active`` UpDownCounter so dashboards built on
    # the metric reflect the actual live agent population.  Paired with the
    # ``-1`` decrement in ``_run()``'s teardown below.  Guarded by
    # ``try_get_instruments()`` so a metrics-init failure (already swallowed
    # above for startup resilience) does not break agent startup.
    _inst = try_get_instruments()
    if _inst is not None:
        _inst.agent_active.add(1, attributes={"agent.id": agent.agent_id})

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
        # PR-170 M2: decrement ``agent.active`` before flushing so the final
        # exported value reflects the agent leaving the live set.  Read the
        # instruments bag again rather than capturing ``_inst`` from the
        # enclosing scope — keeps the symmetry with the ``+1`` site explicit
        # and tolerates any post-init mutation of module state.
        _inst_shutdown = try_get_instruments()
        if _inst_shutdown is not None:
            _inst_shutdown.agent_active.add(
                -1, attributes={"agent.id": agent.agent_id},
            )
        await tracing_shutdown()
        await metrics_shutdown()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
