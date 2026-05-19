"""Unit tests for ``tests._otel_test_helpers``.

The helpers module exists so OTel-counter assertions across the test
tree share one implementation instead of accumulating copies (PR 298
slice-4 review N5: a third copy was about to land — extracting the
helper deletes that copy *and* the two pre-existing ones in
``test_interaction_tracker`` / ``test_summarize_close_helpers``).

These tests pin the contract of :func:`counter_total` so a future SDK
shape change (``MetricsData`` → ``data_points``) surfaces immediately
rather than as a silent zero in every consumer's assertion.
"""

from __future__ import annotations

import pytest
from _otel_test_helpers import counter_total


@pytest.fixture(autouse=True)
def _reset_metrics_state():
    from agents.observability import metrics as metrics_mod

    saved_provider = metrics_mod._provider
    saved_instruments = metrics_mod._instruments
    metrics_mod._provider = None
    metrics_mod._instruments = None
    try:
        yield
    finally:
        import asyncio

        if metrics_mod._provider is not None:
            asyncio.run(metrics_mod.shutdown())
        metrics_mod._provider = saved_provider
        metrics_mod._instruments = saved_instruments


def _build_meter():
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from agents.observability import metrics as metrics_mod

    reader = InMemoryMetricReader()
    metrics_mod.init_metrics(reader=reader)
    return reader, metrics_mod


def test_counter_total_returns_zero_for_unrecorded_counter():
    reader, _ = _build_meter()
    assert counter_total(reader, "agent.interactions.opened") == 0


def test_counter_total_sums_recorded_increments():
    from agents.observability.metrics import get_instruments

    reader, _ = _build_meter()
    instruments = get_instruments()
    instruments.interactions_opened.add(1)
    instruments.interactions_opened.add(2)
    assert counter_total(reader, "agent.interactions.opened") == 3


def test_counter_total_filters_by_metric_name():
    from agents.observability.metrics import get_instruments

    reader, _ = _build_meter()
    instruments = get_instruments()
    instruments.interactions_opened.add(1)
    instruments.interactions_closed.add(5)
    assert counter_total(reader, "agent.interactions.opened") == 1
    assert counter_total(reader, "agent.interactions.closed") == 5


def test_counter_total_returns_zero_when_reader_has_no_data():
    class _EmptyReader:
        def get_metrics_data(self):
            return None

    assert counter_total(_EmptyReader(), "any.counter") == 0
