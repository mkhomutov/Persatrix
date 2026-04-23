"""Polish-PR regression tests (RFC 0019 PR 6).

Covers the small follow-up items captured in
``docs/rfcs/0019-pr-plan.md`` that did not warrant a dedicated test
module.  Each block names the source review item.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import Link, SpanContext, TraceFlags

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage, _classify_llm_error
from agents.observability import tracing as _tracing_module
from agents.observability.spans import LLM_CALL_SPAN
from agents.observability.tracing import init_tracing


@pytest.fixture
def in_process_exporter() -> Iterator[InMemorySpanExporter]:
    """Attach an InMemorySpanExporter to the active tracer provider.

    Mirrors the pattern in ``test_observability_spans.py`` so we share
    the global tracer provider with neighbouring tests instead of
    racing :func:`init_tracing` for the OTEL one-shot global slot.
    """
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    exp = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exp)
    provider.add_span_processor(processor)
    yield exp
    processor.shutdown()

# ─── PR #170 review nice-to-have: parametrised _classify_llm_error ─────────


class TestClassifyLLMError:
    """Lock the low-cardinality buckets that feed ``llm.error_type``.

    A regression here would silently change metric cardinality; the
    classifier has no other callers.
    """

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            # rate-limit bucket — by class name
            (type("RateLimitError", (Exception,), {})("hit"), "rate_limit"),
            (type("APIRateLimitError", (Exception,), {})("hit"), "rate_limit"),
            # rate-limit bucket — by message
            (RuntimeError("HTTP 429: too many requests"), "rate_limit"),
            (RuntimeError("provider says rate limit reached"), "rate_limit"),
            # timeout bucket — by class name + builtin
            (type("APITimeoutError", (Exception,), {})("slow"), "timeout"),
            (TimeoutError("deadline"), "timeout"),
            # timeout bucket — by message
            (RuntimeError("connection timeout after 30s"), "timeout"),
            # default bucket
            (RuntimeError("unrelated provider failure"), "provider_error"),
            (ValueError("bad input"), "provider_error"),
        ],
    )
    def test_buckets(self, exc: BaseException, expected: str) -> None:
        assert _classify_llm_error(exc) == expected


# ─── PR #167 review nice-to-have: finish_reasons is always a list ──────────


class TestFinishReasonsListShape:
    """The ``gen_ai.response.finish_reasons`` attribute MUST be a list per
    OTEL spec, even when only one reason is present.  A future change that
    emits a scalar string would silently break vendor backends that parse
    the attribute.
    """

    async def test_finish_reasons_is_list_not_scalar(
        self, in_process_exporter: InMemorySpanExporter,
    ) -> None:
        provider = AsyncMock()
        provider.name = "openai"
        provider.create_message = AsyncMock(
            return_value=LLMResponse(
                text="x",
                stop_reason=StopReason.END_TURN,
                usage=Usage(input_tokens=1, output_tokens=1),
            ),
        )
        await LLMClient(provider).create_message(
            model="gpt-4o", messages=[], system="", tools=[],
            max_tokens=10, temperature=0.0,
        )

        spans = in_process_exporter.get_finished_spans()
        span = next(s for s in spans if s.name == LLM_CALL_SPAN)
        attrs = span.attributes or {}
        reasons = attrs["gen_ai.response.finish_reasons"]
        # OTEL serialises sequences as a tuple at attribute-set time, so
        # the runtime type is ``tuple`` — but it MUST not be a bare string.
        assert isinstance(reasons, (list, tuple))
        assert not isinstance(reasons, str)
        assert len(reasons) == 1
        assert reasons[0] == "stop"


# ─── PR #163 review nice-to-have: init_tracing() re-call regression ────────


class TestInitTracingRecall:
    """Lock the documented one-way ``set_tracer_provider`` behaviour.

    A second ``init_tracing()`` call returns a fresh tracer (the function
    rebuilds the module-level ``_provider``) and OTEL's own
    ``set_tracer_provider`` logs a warning the second time around, since
    the global slot is one-shot per process.
    """

    def test_second_call_returns_fresh_tracer_and_warns(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        exporter1 = InMemorySpanExporter()
        tracer1 = init_tracing(exporter=exporter1)
        provider1 = _tracing_module._provider

        exporter2 = InMemorySpanExporter()
        with caplog.at_level(logging.WARNING):
            tracer2 = init_tracing(exporter=exporter2)

        # Module-level provider is replaced (so shutdown() flushes the latest).
        assert _tracing_module._provider is not provider1
        # Returned tracer is a fresh handle.
        assert tracer2 is not tracer1
        # OTEL warns when the global tracer provider is set more than once.
        # Match defensively on the substring rather than the exact text so
        # SDK patch releases that reword the warning are not test-breaking.
        # Current SDK emits "Overriding of current TracerProvider is not
        # allowed" — both anchors are stable enough to survive minor wording
        # changes.
        messages = [r.getMessage().lower() for r in caplog.records]
        assert any(
            "tracerprovider" in m or "tracer provider" in m for m in messages
        ), messages


# ─── PR #163 review nice-to-have: OTLP endpoint path-normalisation ─────────


class TestOTLPEndpointNormalisation:
    """The ``/v1/traces`` double-suffix guard added during PR 1 review has no
    explicit test today.  Verify both the bare-host case (suffix appended)
    and the already-suffixed case (suffix not duplicated)."""

    def _exporter_endpoint(self) -> str:
        # Reach into the BSP -> SimpleSpanProcessor exporter to read the
        # configured endpoint.  Tests use SimpleSpanProcessor (test path)
        # whose ``span_exporter`` is the InMemoryExporter; instead probe
        # the OTLP path by passing exporter=None and inspecting the live
        # provider's processor chain.
        provider = _tracing_module._provider
        assert provider is not None
        # Provider keeps a SynchronousMultiSpanProcessor; walk it.
        active = provider._active_span_processor  # type: ignore[attr-defined]
        # ``_span_processors`` is a tuple of registered processors.
        span_processors = active._span_processors  # type: ignore[attr-defined]
        for proc in span_processors:
            real_exporter = getattr(proc, "span_exporter", None)
            endpoint = getattr(real_exporter, "_endpoint", None)
            if endpoint is not None:
                return str(endpoint)
        raise AssertionError("no OTLP exporter found in provider")

    def test_bare_endpoint_gets_v1_traces_appended(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        init_tracing()  # exporter=None → real OTLP path
        assert self._exporter_endpoint() == "http://collector:4318/v1/traces"

    def test_already_suffixed_endpoint_not_doubled(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318/v1/traces",
        )
        init_tracing()
        # The guard MUST not produce ``…/v1/traces/v1/traces``.
        assert self._exporter_endpoint() == "http://collector:4318/v1/traces"


# ─── PR #167 review Should-Fix: bounded _pending_tick_links ────────────────


class TestPendingTickLinksBound:
    """A high event rate without a tick must not leak memory."""

    def test_buffer_caps_at_32_with_oldest_drop(self) -> None:
        # Build a minimal stand-in that has the same buffer + add method
        # contract as ``_LLMPersonaAgent`` without dragging in the LLM
        # client / memory tier dependencies a full instance would need.
        from agents.persona_runtime import (
            _PENDING_TICK_LINKS_CAP,
            _LLMPersonaAgent,
        )

        ctx = SpanContext(
            trace_id=0x01234567890123456789012345678901,
            span_id=0x0123456789012345,
            is_remote=False,
            trace_flags=TraceFlags(0x01),
        )

        # Bind ``add_pending_tick_link`` to a bare object holding the list.
        class _FakeAgent:
            _pending_tick_links: list[Link] = []

        fake = _FakeAgent()
        fake._pending_tick_links = []  # per-instance list
        bound = _LLMPersonaAgent.add_pending_tick_link.__get__(fake, _FakeAgent)

        # Push well over the cap with sequence-tagged attrs so we can verify
        # oldest-drop semantics (the *most recent* CAP entries survive).
        for i in range(_PENDING_TICK_LINKS_CAP + 64):
            bound(Link(ctx, attributes={"link.kind": "trigger", "seq": i}))

        assert len(fake._pending_tick_links) == _PENDING_TICK_LINKS_CAP
        # Oldest dropped → first surviving seq is exactly 64.
        first_attrs = fake._pending_tick_links[0].attributes or {}
        last_attrs = fake._pending_tick_links[-1].attributes or {}
        assert first_attrs["seq"] == 64
        assert last_attrs["seq"] == _PENDING_TICK_LINKS_CAP + 63


# ─── Cleanup: reset module-level tracing provider between tests ────────────


@pytest.fixture(autouse=True)
def _reset_tracing_provider() -> Iterator[None]:
    """Every test in this module installs a new provider; flush + clear so
    later tests see a clean slate.  The OTEL global provider itself is
    one-shot per process by design — we only reset the module-level
    reference so ``shutdown()`` flushes the most recent one.
    """
    yield
    # Best-effort flush; ``shutdown()`` is async and idempotent.
    if _tracing_module._provider is not None:
        asyncio.run(_tracing_module.shutdown())
