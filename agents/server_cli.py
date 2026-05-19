"""Command-line entry point for the Persatrix agent server.

Holds ``main()`` — argument parsing and the process bootstrap (structured
logging, OTEL tracing/metrics, signal handling, the run/shutdown loop).
Split out of :mod:`agents.server` so that module stays the gRPC server
implementation only and both files stay within the repo's file-size cap.

The ``Persatrix-agent`` console script and ``python -m persatrix_agents.server``
both resolve to this ``main()``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from opentelemetry.instrumentation.grpc import GrpcAioInstrumentorServer

from .observability.logging import configure_logging
from .observability.metrics import init_metrics, try_get_instruments
from .observability.metrics import shutdown as metrics_shutdown
from .observability.tracing import init_tracing
from .observability.tracing import shutdown as tracing_shutdown
from .server import AgentServer
from .server_persona import load_agent, setup_shared_pools

logger = logging.getLogger("Persatrix.agent.server")


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
