"""RFC 0033 PR 5 / §G — ``persatrix.llm.model_alias`` span attribute.

Split out of ``test_llm_client.py`` to keep that module under the 500-line
file-size cap (same discipline as the PR 3 / PR 4 test-module splits).

The ``agent.llm.call`` span carries ``persatrix.llm.model_alias`` when the
request came in via a ``models.aliases`` name, *alongside* (never replacing)
the physical ``gen_ai.request.model`` (RFC 0019 contract). The alias is
telemetry-only — it must never reach the provider's ``create_message``, so the
vendor API receives the physical id only.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest

from agents.llm_client import LLMClient, LLMResponse


@pytest.fixture
def span_exporter() -> Iterator[object]:
    """Install a fresh InMemorySpanExporter on the active tracer provider.

    Same shape as ``test_channel_publish_otel.py::exporter`` — avoids
    ``init_tracing`` because that would build a competing provider and break
    downstream tests sharing the global.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    exp = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exp)
    provider.add_span_processor(processor)
    yield exp
    processor.shutdown()


class TestModelAliasSpanAttribute:
    async def test_alias_sets_span_attribute_and_keeps_physical_model(
        self, span_exporter,
    ) -> None:
        provider = AsyncMock()
        provider.create_message.return_value = LLMResponse(text="ok")
        client = LLMClient(provider)

        await client.create_message(
            model="claude-sonnet-4-6",
            model_alias="quality",
            messages=[],
            system="",
            tools=[],
            max_tokens=100,
            temperature=0.0,
        )

        spans = [
            s for s in span_exporter.get_finished_spans()
            if s.name == "agent.llm.call"
        ]
        assert len(spans) == 1
        attrs = spans[0].attributes
        # The alias is added; the physical id stays on gen_ai.request.model
        # (RFC 0019 — vendor backends render the physical model).
        assert attrs["persatrix.llm.model_alias"] == "quality"
        assert attrs["gen_ai.request.model"] == "claude-sonnet-4-6"

        # The alias must not leak into the provider call.
        _, call_kwargs = provider.create_message.call_args
        assert "model_alias" not in call_kwargs
        assert call_kwargs["model"] == "claude-sonnet-4-6"

    async def test_raw_path_omits_span_attribute(self, span_exporter) -> None:
        provider = AsyncMock()
        provider.create_message.return_value = LLMResponse(text="ok")
        client = LLMClient(provider)

        # No model_alias passed — the raw-ID pass-through path.
        await client.create_message(
            model="claude-sonnet-4-20250514",
            messages=[],
            system="",
            tools=[],
            max_tokens=100,
            temperature=0.0,
        )

        spans = [
            s for s in span_exporter.get_finished_spans()
            if s.name == "agent.llm.call"
        ]
        assert len(spans) == 1
        assert "persatrix.llm.model_alias" not in spans[0].attributes
        assert (
            spans[0].attributes["gen_ai.request.model"] == "claude-sonnet-4-20250514"
        )
