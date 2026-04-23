"""Integration test for RFC 0018 Phase 3 — cross-process log correlation.

Verifies the end-to-end flow:

1. Outbound side: ``grpcmeta.InjectIDs`` (Go) — simulated here via direct
   gRPC metadata injection — places the four ``persatrix-*`` keys on an
   ExecuteTask call.
2. Inbound side: :class:`agents.observability.grpc_logging.LoggingMetadataInterceptor`
   binds those keys to ``structlog`` contextvars before the agent handler
   runs.
3. Log emission: a ``logger.info(...)`` call from within the handler emits
   a JSON record carrying ``execution_id``, ``step_id``, ``agent_id``,
   ``workflow_id`` (the four IDs from metadata) — and, when an OTEL span
   is active for the call, also ``trace_id`` and ``span_id``.
4. Cleanup: after the RPC returns, contextvars are unbound so a log
   record emitted *outside* the handler scope carries none of those four
   IDs.

This is the production correlation invariant the operator-facing
``persatrix logs`` CLI in RFC 0018 Phase 4 will rely on.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import grpc
import grpc.aio
import pytest
import structlog
from opentelemetry import trace
from opentelemetry.instrumentation.grpc import GrpcAioInstrumentorServer
from opentelemetry.sdk.trace import TracerProvider

from agents.generated import task_pb2, task_pb2_grpc
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.observability import logging as logging_mod
from agents.observability.grpc_logging import LoggingMetadataInterceptor
from agents.observability.logging import configure_logging, get_logger
from agents.observability.redact import NoopRedactor
from agents.server_servicers import AgentServiceServicer
from agents.task_agent import TaskAgent

# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Iterator[None]:
    structlog.contextvars.clear_contextvars()
    logging_mod._configured = False
    logging_mod._redactor = NoopRedactor()
    structlog.reset_defaults()
    yield
    structlog.contextvars.clear_contextvars()
    logging_mod._configured = False
    logging_mod._redactor = NoopRedactor()
    structlog.reset_defaults()


@pytest.fixture
def captured_stderr(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Capture stderr output from the structlog handler."""
    import sys as _real_sys

    buf = io.StringIO()

    class _SysShim:
        stderr = buf

        def __getattr__(self, name: str) -> Any:
            return getattr(_real_sys, name)

    monkeypatch.setattr("sys.stderr", buf)
    monkeypatch.setattr(logging_mod, "sys", _SysShim())
    return buf


@pytest.fixture
def grpc_aio_server_instrumented() -> Iterator[GrpcAioInstrumentorServer]:
    """Install/uninstall the gRPC aio server OTEL instrumentor for one test.

    The ``LoggingMetadataInterceptor`` does not require this — but the
    trace_id / span_id pivot does.  Installing it here also asserts the
    documented ordering invariant: OTEL instrumentor first, then
    LoggingMetadataInterceptor, so the OTEL processor in
    :mod:`agents.observability.logging` sees an active span when the
    handler emits a record.
    """
    # Ensure a real SDK provider is installed so the OTEL processor reads
    # a valid SpanContext.  (``DefaultTracerProvider`` returns invalid
    # SpanContexts which the processor correctly skips.)
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())

    instrumentor = GrpcAioInstrumentorServer()
    instrumentor.instrument()
    try:
        yield instrumentor
    finally:
        instrumentor.uninstrument()


def _mock_llm() -> LLMClient:
    provider = AsyncMock()
    provider.create_message = AsyncMock(
        return_value=LLMResponse(
            text="ok",
            stop_reason=StopReason.END_TURN,
            usage=Usage(1, 1),
        )
    )
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(side_effect=lambda msgs, resp, res: msgs)
    return LLMClient(provider)


def _agent() -> TaskAgent:
    return TaskAgent(
        agent_id="ember-owl",
        config={
            "model": "test-model",
            "role": "Correlation probe",
            "max_llm_calls": 1,
            "max_tokens": 64,
            "instructions": "ack",
        },
        llm_client=_mock_llm(),
    )


# ─── Tests ───────────────────────────────────────────────────────────────────


class TestLogCorrelation:
    async def test_handler_log_carries_all_four_ids(
        self,
        captured_stderr: io.StringIO,
        grpc_aio_server_instrumented: GrpcAioInstrumentorServer,
    ) -> None:
        """A logger.info() call inside the agent handler must emit a JSON
        record carrying execution_id, step_id, agent_id, workflow_id."""
        configure_logging(
            service_kind="agent",
            service_instance="ember-owl",
            level="DEBUG",
        )

        # Inject a logger.info() call into the agent's handler so we have
        # a controlled emission point inside the contextvar scope.  The
        # production handler emits its own log records via stdlib logging
        # which flow through the same chain, but those records are noisy
        # and harder to assert against.
        from agents.server_servicers import AgentServiceServicer as _Servicer

        marker = "CORRELATION_PROBE_INSIDE_HANDLER"

        agent = _agent()
        original_handle = agent.handle

        async def instrumented_handle(payload: str, *args: Any, **kwargs: Any) -> Any:
            get_logger("test.handler").info(marker)
            return await original_handle(payload, *args, **kwargs)

        agent.handle = instrumented_handle  # type: ignore[method-assign]

        servicer = _Servicer({"ember-owl": agent})
        server = grpc.aio.server(interceptors=[LoggingMetadataInterceptor()])
        task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()

        try:
            md = grpc.aio.Metadata(
                ("persatrix-execution-id", "exec-42"),
                ("persatrix-step-id", "step-A"),
                ("persatrix-agent-id", "ember-owl"),
                ("persatrix-workflow-id", "wf-7"),
            )
            channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
            stub = task_pb2_grpc.AgentServiceStub(channel)
            req = task_pb2.TaskRequest(
                task_id="t-1",
                workflow_id="wf-7",
                agent_id="ember-owl",
                payload="ping",
            )
            await stub.ExecuteTask(req, metadata=md)
            await channel.close()
        finally:
            await server.stop(grace=0)

        # Force any buffered stdlib handlers to flush so the StringIO sink
        # has the marker line.
        for h in logging.getLogger().handlers:
            h.flush()

        marker_line: dict[str, Any] | None = None
        for raw in captured_stderr.getvalue().splitlines():
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if rec.get("message") == marker:
                marker_line = rec
                break
        assert marker_line is not None, (
            f"marker log line not found; stderr was:\n{captured_stderr.getvalue()}"
        )

        assert marker_line["execution_id"] == "exec-42"
        assert marker_line["step_id"] == "step-A"
        assert marker_line["agent_id"] == "ember-owl"
        assert marker_line["workflow_id"] == "wf-7"
        # OTEL pivot: with GrpcAioInstrumentorServer installed there is an
        # active server-side span when the handler runs, so the OTEL
        # processor in logging.py emits trace_id / span_id too.
        assert "trace_id" in marker_line
        assert "span_id" in marker_line
        assert len(marker_line["trace_id"]) == 32
        assert len(marker_line["span_id"]) == 16

    async def test_log_outside_handler_omits_correlation_ids(
        self,
        captured_stderr: io.StringIO,
        grpc_aio_server_instrumented: GrpcAioInstrumentorServer,
    ) -> None:
        """After the RPC returns the contextvars must be unbound so log
        records emitted from background tasks (or post-call cleanup) do
        not inherit stale IDs."""
        configure_logging(
            service_kind="agent",
            service_instance="ember-owl",
            level="DEBUG",
        )

        agent = _agent()
        servicer = AgentServiceServicer({"ember-owl": agent})
        server = grpc.aio.server(interceptors=[LoggingMetadataInterceptor()])
        task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()

        try:
            md = grpc.aio.Metadata(
                ("persatrix-execution-id", "exec-99"),
                ("persatrix-agent-id", "ember-owl"),
            )
            channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
            stub = task_pb2_grpc.AgentServiceStub(channel)
            req = task_pb2.TaskRequest(
                task_id="t-2",
                workflow_id="wf-1",
                agent_id="ember-owl",
                payload="ping",
            )
            await stub.ExecuteTask(req, metadata=md)
            await channel.close()
        finally:
            await server.stop(grace=0)

        # Now emit a log line *after* the handler returned.
        marker = "POST_HANDLER_PROBE"
        get_logger("test.posthandler").info(marker)
        for h in logging.getLogger().handlers:
            h.flush()

        post_line: dict[str, Any] | None = None
        for raw in captured_stderr.getvalue().splitlines():
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if rec.get("message") == marker:
                post_line = rec
                break

        assert post_line is not None
        assert "execution_id" not in post_line
        assert "step_id" not in post_line
        assert "workflow_id" not in post_line
        # The interceptor unbound agent_id too — it was set by the metadata,
        # not by configure_logging.  The service.* fields persist.
        assert "agent_id" not in post_line
        assert post_line["service.kind"] == "agent"
        assert post_line["service.instance"] == "ember-owl"
