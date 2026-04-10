"""
Orchestr8 Agent gRPC Server.

Runs one or more agents in a single process, exposing them via gRPC
for the orchestrator to communicate with.
"""

import argparse
import asyncio
import logging
import signal
import sys

# TODO: Import generated gRPC stubs
# from .generated import task_pb2, task_pb2_grpc

logger = logging.getLogger("orchestr8.agent.server")


class AgentServer:
    """gRPC server hosting one or more agents."""

    def __init__(self, host: str = "127.0.0.1", port: int = 50051):
        self.host = host
        self.port = port
        self.agents: dict = {}  # agent_id -> BaseAgent instance
        self._server = None

    def register_agent(self, agent) -> None:
        """Register an agent instance with the server."""
        self.agents[agent.agent_id] = agent
        # Use %-formatting for lazy evaluation (avoid interpolation when log level is disabled)
        logger.info("Registered agent: %s (%s)", agent.agent_id, agent.name)

    async def start(self) -> None:
        """Start the gRPC server."""
        # TODO: Create gRPC server
        # TODO: Add AgentService servicer
        # TODO: Add health check servicer
        # TODO: Start serving
        logger.info("Agent server listening on %s:%s", self.host, self.port)
        logger.info("Serving %d agent(s): %s", len(self.agents), list(self.agents.keys()))

    async def stop(self) -> None:
        """Gracefully stop the server."""
        logger.info("Shutting down agent server...")
        for agent in self.agents.values():
            await agent.shutdown()
        if self._server:
            await self._server.stop(grace=5)
        logger.info("Agent server stopped.")


def load_agent(agent_id: str):
    """Load an agent by ID from config. Returns a BaseAgent instance."""
    # TODO: Load agent config from YAML
    # TODO: Determine agent type (task vs persona)
    # TODO: Instantiate appropriate class
    # TODO: Initialize LLM client
    # TODO: Register tools
    raise NotImplementedError(f"Agent loading not yet implemented: {agent_id}")


def main():
    parser = argparse.ArgumentParser(description="Orchestr8 Agent Server")
    parser.add_argument("--agent", required=True, help="Agent ID to run")
    parser.add_argument("--port", type=int, default=50051, help="gRPC port")
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Bind address (use 0.0.0.0 in containers)",
    )
    parser.add_argument("--config", default="../config/agents.yaml", help="Agent config path")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    server = AgentServer(host=args.host, port=args.port)

    # TODO: Load and register agent
    # agent = load_agent(args.agent)
    # server.register_agent(agent)

    async def _run() -> None:
        """Run server with asyncio.run() for proper event-loop management.

        asyncio.run() (Python 3.7+) creates, sets, and tears down the loop
        correctly — unlike the legacy new_event_loop() pattern which never
        called set_event_loop(), so any code using get_event_loop() elsewhere
        would silently get a *different* loop.
        """
        shutdown = asyncio.Event()

        def request_shutdown():
            shutdown.set()

        # loop.add_signal_handler() is POSIX-only and raises NotImplementedError on Windows.
        # Use platform detection to support both POSIX and Windows signal handling.
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
