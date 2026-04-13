"""
Orchestr8 Agent gRPC Server.

Runs a single agent in a process, exposing it via gRPC for the orchestrator
to communicate with. Implements AgentServiceServicer (ExecuteTask, HealthCheck,
ExecuteTaskStream) from the generated protobuf stubs.
"""

import argparse
import asyncio
import json
import logging
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
import grpc
import grpc.aio
import yaml

from .base import BaseAgent, TaskInput, TaskInputConfig, TaskOutput, TaskStatus
from .generated import task_pb2, task_pb2_grpc
from .llm_client import LLMClient, create_provider
from .persona import (
    EventDispatcher,
    TickScheduler,
    _LLMPersonaAgent,
    create_persona_agent,
)
from .task_agent import TaskAgent
from .tools import builtin
from .tools.permissions import PermissionGate
from .tools.sandbox import PathValidator

logger = logging.getLogger("orchestr8.agent.server")

# Agent IDs must match the cross-component contract shared with the Go
# orchestrator registry.  Validated at load time to prevent routing mismatches.
_AGENT_ID_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


# ─── AgentServiceServicer ───────────────────────────────────


class AgentServiceServicer(task_pb2_grpc.AgentServiceServicer):
    """gRPC servicer implementing ExecuteTask, HealthCheck, and ExecuteTaskStream."""

    def __init__(self, agents: dict[str, BaseAgent]):
        self._agents = agents

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


# ─── Agent Loading ───────────────────────────────────────────


def _resolve_agent_type(agent_config: dict[str, Any]) -> str:
    """Resolve agent type string from the ``type`` field in agent config.

    Supported types:
    - ``task`` (default) — data-driven TaskAgent with YAML instructions
    - ``persona`` — LLM-powered PersonaAgent with memory and autonomy

    Agents without a ``type`` field default to ``task`` for backward
    compatibility with v0.1 configs.
    """
    agent_type = agent_config.get("type", "task")

    match agent_type:
        case "task":
            return "task"
        case "persona":
            return "persona"
        case _:
            raise SystemExit(
                f"Unknown agent type {agent_type!r} for agent "
                f"{agent_config['id']!r}. Supported types: task, persona"
            )


def load_agent(agent_id: str, config_path: str, workspace: str) -> BaseAgent:
    """Load an agent by ID from YAML config.

    Returns a fully-initialized BaseAgent with LLM client, tools, and
    permission configuration wired.
    """
    # MF-02: validate agent ID format against the cross-component contract
    # (^[a-z0-9][a-z0-9-]*[a-z0-9]$) shared with Go orchestrator registry.
    if not _AGENT_ID_PATTERN.match(agent_id):
        raise SystemExit(
            f"Invalid agent ID {agent_id!r}: "
            f"must match {_AGENT_ID_PATTERN.pattern}"
        )

    # PR-review m5: surface clear errors at startup
    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        raise SystemExit(f"Agent config not found: {config_path}")
    except yaml.YAMLError as exc:
        raise SystemExit(f"Invalid YAML in {config_path}: {exc}")

    # S-02: validate that 'agents' value is a list before iteration.
    # A malformed config like ``agents: "string"`` would otherwise fail
    # with an unclear TypeError during enumeration.
    agents_list = config.get("agents", [])
    if not isinstance(agents_list, list):
        raise SystemExit(
            f"'agents' key in {config_path} must be a list, "
            f"got {type(agents_list).__name__}"
        )

    # F-03: validate 'id' field presence before dict comprehension to
    # surface a clear SystemExit instead of a raw KeyError.
    for i, a in enumerate(agents_list):
        if "id" not in a:
            raise SystemExit(
                f"Agent config entry {i} missing required 'id' field"
            )
    # S-17: detect duplicate agent IDs — dict comprehension silently takes
    # the last entry, which may mask config errors.
    seen_ids: set[str] = set()
    for a in agents_list:
        aid = a["id"]
        if aid in seen_ids:
            raise SystemExit(
                f"Duplicate agent ID {aid!r} in {config_path}"
            )
        seen_ids.add(aid)
    agent_configs = {a["id"]: a for a in agents_list}
    if agent_id not in agent_configs:
        raise SystemExit(f"Agent {agent_id!r} not found in {config_path}")

    agent_config = agent_configs[agent_id]
    agent_type = _resolve_agent_type(agent_config)

    # SF-08: validate required 'model' field at startup so operators see a
    # clear message instead of a raw KeyError from create_provider().
    if "model" not in agent_config:
        raise SystemExit(
            f"Agent {agent_id!r} missing required 'model' field in config"
        )

    # Create LLM client
    provider = create_provider(agent_config)
    llm_client = LLMClient(provider)

    # Create agent based on type
    agent: BaseAgent
    if agent_type == "persona":
        agent = create_persona_agent(
            agent_id=agent_id,
            config=agent_config,
            llm_client=llm_client,
        )
    else:
        agent = TaskAgent(
            agent_id=agent_id,
            config=agent_config,
            llm_client=llm_client,
        )

    # Wire built-in tool dependencies
    permissions = agent_config.get("permissions", {})
    builtin.permission_gate = PermissionGate(permissions)
    fs = permissions.get("filesystem", {})
    builtin.path_validator = PathValidator(
        allow_read=fs.get("read", []),
        allow_write=fs.get("write", []),
        deny=fs.get("deny", []),
    )
    builtin.workspace_root = Path(workspace).resolve()

    return agent


# ─── AgentServer ─────────────────────────────────────────────


class AgentServer:
    """gRPC server hosting one or more agents."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 50051,
        shutdown_grace: int = 30,
        orchestrator_url: str = "http://127.0.0.1:8080",
    ):
        self.host = host
        self.port = port
        self.shutdown_grace = shutdown_grace
        self.orchestrator_url = orchestrator_url.rstrip("/")
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
        servicer = AgentServiceServicer(self.agents)
        task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, self._server)

        bind_address = f"{self.host}:{self.port}"
        # TODO(security): enable TLS for production gRPC
        # SF-05: store the actual port so logging is correct when port=0
        # (dynamic allocation) and future self-registration uses the real port.
        actual_port = self._server.add_insecure_port(bind_address)
        self.port = actual_port
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
        for agent_id, agent in self.agents.items():
            if not isinstance(agent, _LLMPersonaAgent):
                continue

            # Memory initialization — must succeed before dispatch/tick.
            try:
                await agent.initialize_memory()
                logger.info("Initialized memory for persona agent %s", agent_id)
            except Exception:
                logger.exception(
                    "Failed to initialize memory for agent %s — "
                    "agent will NOT receive dispatched events or tick scheduling",
                    agent_id,
                )
                # (F-60-8: removed dead failed_memory_init set — continue
                # already skips the agent, no downstream loops need it.)
                continue

            # Register with event dispatcher.
            self._dispatcher.register_agent(agent_id, agent)

            # Start tick scheduler for autonomous agents.
            autonomy = agent.config.get("autonomy", {})
            level = autonomy.get("level", "reactive")
            if level in ("semi-autonomous", "autonomous"):
                interval = autonomy.get("tick_interval_seconds", 60)
                max_actions = autonomy.get("max_actions_per_tick", 3)
                idle_after = autonomy.get("idle_after_ticks", 10)
                scheduler = TickScheduler(
                    agent,
                    interval=float(interval),
                    max_actions_per_tick=max_actions,
                    idle_after_ticks=idle_after,
                    executor=self._dispatcher.executor,
                )
                self._tick_schedulers[agent_id] = scheduler
                self._dispatcher.register_tick_scheduler(agent_id, scheduler)
                scheduler.start()
                logger.info(
                    "Started tick scheduler for %s (interval=%ds)",
                    agent_id,
                    interval,
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
            # TODO(v0.2): support advertised address for container/K8s
            # service discovery — bind address may differ from the address
            # the orchestrator should use to reach this agent.
            address = f"{self.host}:{self.port}"
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
    parser = argparse.ArgumentParser(description="Orchestr8 Agent Server")
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
