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
import signal
import sys
import time
from pathlib import Path
from typing import Any

import grpc
import grpc.aio
import yaml

from .base import BaseAgent, TaskInput, TaskInputConfig, TaskOutput, TaskStatus
from .coder import CoderAgent
from .generated import task_pb2, task_pb2_grpc
from .llm_client import LLMClient, create_provider
from .planner_agent import PlannerAgent
from .reviewer import ReviewerAgent
from .tools import builtin
from .tools.permissions import PermissionGate
from .tools.sandbox import PathValidator

logger = logging.getLogger("orchestr8.agent.server")


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
        except Exception as exc:
            logger.exception("Task execution failed: %s", request.task_id)
            return task_pb2.TaskResponse(
                task_id=request.task_id,
                status=task_pb2.FAILED,
                error_message=f"Internal error: {type(exc).__name__}",
            )

    async def HealthCheck(
        self,
        request: task_pb2.HealthCheckRequest,
        context: grpc.aio.ServicerContext,
    ) -> task_pb2.HealthCheckResponse:
        # PR-review B4: delegate to agent.health_check()
        agent = self._agents.get(request.service) if request.service else None
        if agent is not None:
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


def _resolve_agent_type(agent_config: dict[str, Any]) -> type[BaseAgent]:
    """Resolve agent type from capabilities.

    # TODO(v0.2): add explicit 'type' field to agent config for direct
    # type resolution instead of capability-based inference.
    """
    caps = set(agent_config.get("capabilities", []))

    if "planning" in caps:
        return PlannerAgent
    if "code_generation" in caps:
        return CoderAgent
    if "code_review" in caps:
        return ReviewerAgent

    raise SystemExit(
        f"Cannot determine agent type for {agent_config['id']!r} "
        f"from capabilities: {caps}"
    )


def load_agent(agent_id: str, config_path: str, workspace: str) -> BaseAgent:
    """Load an agent by ID from YAML config.

    Returns a fully-initialized BaseAgent with LLM client, tools, and
    permission configuration wired.
    """
    # PR-review m5: surface clear errors at startup
    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        raise SystemExit(f"Agent config not found: {config_path}")
    except yaml.YAMLError as exc:
        raise SystemExit(f"Invalid YAML in {config_path}: {exc}")

    agent_configs = {a["id"]: a for a in config.get("agents", [])}
    if agent_id not in agent_configs:
        raise SystemExit(f"Agent {agent_id!r} not found in {config_path}")

    agent_config = agent_configs[agent_id]
    agent_type = _resolve_agent_type(agent_config)

    # Create LLM client
    provider = create_provider(agent_config)
    llm_client = LLMClient(provider)

    # Create agent
    agent = agent_type(
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
    ):
        self.host = host
        self.port = port
        self.shutdown_grace = shutdown_grace
        self.agents: dict[str, BaseAgent] = {}
        self._server: grpc.aio.Server | None = None

    def register_agent(self, agent: BaseAgent) -> None:
        """Register an agent instance with the server."""
        self.agents[agent.agent_id] = agent
        logger.info("Registered agent: %s (%s)", agent.agent_id, agent.name)

    async def start(self) -> None:
        """Start the gRPC server."""
        self._server = grpc.aio.server()
        servicer = AgentServiceServicer(self.agents)
        task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, self._server)

        bind_address = f"{self.host}:{self.port}"
        # TODO(security): enable TLS for production gRPC
        self._server.add_insecure_port(bind_address)
        await self._server.start()

        logger.info("Agent server listening on %s", bind_address)
        logger.info(
            "Serving %d agent(s): %s",
            len(self.agents),
            list(self.agents.keys()),
        )

    async def stop(self) -> None:
        """Gracefully stop the server."""
        logger.info("Shutting down agent server...")
        if self._server:
            await self._server.stop(grace=self.shutdown_grace)
        for agent in self.agents.values():
            await agent.shutdown()
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

        await server.start()
        await shutdown.wait()
        await server.stop()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
