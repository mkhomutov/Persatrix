"""Pytest fixtures for agents/tests/.

Provides an ``InMemorySpanExporter`` fixture that replaces the global
``BatchSpanProcessor`` for the duration of a test.  Tests that need to assert
on emitted spans should declare ``span_exporter`` as a fixture parameter.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    """Return a fresh ``InMemorySpanExporter`` wired as the global OTEL provider.

    The provider is replaced for the duration of the test; the previous global
    provider is NOT restored because OTEL's global provider replacement is
    intentionally one-way (same pattern as Go's ``go.opentelemetry.io/otel``
    test helpers).  Each test that needs spans should request this fixture;
    tests that do not care about spans will keep whatever provider was set
    by a previous fixture or ``init_tracing()`` call.
    """
    import opentelemetry.trace as otel_trace

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    otel_trace.set_tracer_provider(provider)
    return exporter
