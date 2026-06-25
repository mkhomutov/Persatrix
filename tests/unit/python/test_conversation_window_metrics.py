"""RFC 0034 Phase 3 — conversation-window instrumentation.

Phase 1 shipped no telemetry ("shipping inert counters now would invite
premature dashboard work" — RFC 0034 PR plan). Phase 3 adds the OTEL metrics
that make the fetch cache and the fetch-failure fall-back observable, so the
``max_turns`` / ``max_tokens`` defaults and the LRU cache bound can be re-tuned
from real data rather than guessed:

* ``conversation_window.cache_access`` — charted by ``result`` (hit|miss); the
  cache-hit rate is ``hit / (hit + miss)`` over consulted look-ups;
* ``conversation_window.cache_evictions`` — LRU evictions, so an undersized
  bound is visible rather than silently thrashing;
* ``conversation_window.fetch_duration`` — the wall-clock cost of a real
  history fetch (the cost the cache exists to avoid);
* ``conversation_window.fallback`` — charted by ``reason`` (fetch_failed|
  fetch_none); the silent degrade-to-current-event-only the §F risk table
  flagged as masking a real outage.

These drive the real emit sites through :func:`build_conversation_messages`,
mirroring how ``test_reflexion_metrics.py`` drives the real reflexion glue, and
assert the never-propagate guard on the module-owned ``record_*`` helpers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

import agents.observability._metrics_conversation_window as _cwm
import agents.observability.metrics as pmetrics
import agents.persona_runtime._conversation_window_cache as cwc
from agents.persona_runtime._conversation_window_cache import _WindowCache
from agents.persona_runtime.conversation_window import ConversationWindowConfig

from ._conversation_window_test_helpers import (
    _CURRENT,
    _build,
    _event,
    _FakeChannelHistoryFetcher,
    _row,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clear_window_cache() -> Iterator[None]:
    """The fetch cache is module-level (RFC §F); clear it around every test so
    cache-hit / miss / eviction counts do not bleed between cases."""
    cwc._WINDOW_CACHE.clear()
    yield
    cwc._WINDOW_CACHE.clear()


@pytest.fixture
def metric_reader() -> Iterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    pmetrics.init_metrics(reader=reader)
    try:
        yield reader
    finally:
        asyncio.run(pmetrics.shutdown())


def _collect(reader: InMemoryMetricReader) -> dict[str, Any]:
    data = reader.get_metrics_data()
    out: dict[str, Any] = {}
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                out[m.name] = m
    return out


def _points(metric: Any) -> list[Any]:
    return list(metric.data.data_points) if metric is not None else []


def _by_attr(metric: Any, key: str) -> dict[str, float]:
    """Sum a counter's points keyed by one attribute value."""
    summed: dict[str, float] = {}
    for dp in _points(metric):
        attr = dp.attributes.get(key)
        summed[attr] = summed.get(attr, 0.0) + dp.value
    return summed


class TestCacheAccessTelemetry:
    async def test_miss_then_hit_charts_both(self, metric_reader: InMemoryMetricReader) -> None:
        fetcher = _FakeChannelHistoryFetcher([_row("m-old", "user", "hello")])
        await _build(fetcher)  # first turn: cache miss → fetch
        await _build(fetcher)  # same event: cache hit → no fetch

        access = _by_attr(_collect(metric_reader).get("conversation_window.cache_access"), "result")
        assert access == {"hit": 1, "miss": 1}
        assert len(fetcher.calls) == 1, "the hit must skip the network fetch"

    async def test_real_fetch_records_duration(self, metric_reader: InMemoryMetricReader) -> None:
        await _build(_FakeChannelHistoryFetcher([_row("m-old", "user", "hi")]))

        duration = _collect(metric_reader).get("conversation_window.fetch_duration")
        assert duration is not None, "a real fetch records its latency"
        assert sum(dp.count for dp in _points(duration)) == 1


class TestFallbackTelemetry:
    async def test_fetch_failure_charts_fallback_and_degrades(
        self, metric_reader: InMemoryMetricReader,
    ) -> None:
        fetcher = _FakeChannelHistoryFetcher(raises=RuntimeError("history endpoint down"))
        messages = await _build(fetcher)

        assert messages == [{"role": "user", "content": _CURRENT}], (
            "a raised fetch degrades to current-event-only"
        )
        fallback = _by_attr(_collect(metric_reader).get("conversation_window.fallback"), "reason")
        assert fallback == {"fetch_failed": 1}

    async def test_fetch_none_charts_fallback(self, metric_reader: InMemoryMetricReader) -> None:
        # The fetcher's own best-effort failure surfaces as a ``None`` return —
        # and in production this, not a raised exception, is the *dominant*
        # failure mode: HttpChannelHistoryFetcher catches HTTP errors and
        # timeouts and returns None rather than raising.
        messages = await _build(_FakeChannelHistoryFetcher(None))

        assert len(messages) == 1, "a None fetch degrades to current-event-only"
        metrics = _collect(metric_reader)
        fallback = _by_attr(metrics.get("conversation_window.fallback"), "reason")
        assert fallback == {"fetch_none": 1}
        # A None return is a failed fetch: charted by the fallback counter only,
        # never the latency histogram. Folding a timeout's latency into
        # fetch_duration would skew the steady-state cost the defaults are
        # re-tuned against — the same contract test_failed_fetch_records_no_duration
        # pins for the raises path.
        assert "conversation_window.fetch_duration" not in metrics

    async def test_failed_fetch_records_no_duration(
        self, metric_reader: InMemoryMetricReader,
    ) -> None:
        # A failed fetch is charted by the fallback counter, not the latency
        # histogram — mixing timeout latency into the steady-state fetch cost
        # would skew the signal the defaults are re-tuned against.
        await _build(_FakeChannelHistoryFetcher(raises=RuntimeError("down")))
        assert "conversation_window.fetch_duration" not in _collect(metric_reader)


class TestEvictionTelemetry:
    async def test_overflow_charts_cache_eviction(
        self, metric_reader: InMemoryMetricReader, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Force the bound down to 1 so two distinct channels overflow it. The
        # cache global lives in _conversation_window_cache (where _fetch_window
        # reads it), so patch it there.
        monkeypatch.setattr(cwc, "_WINDOW_CACHE", _WindowCache(capacity=1))
        fetcher = _FakeChannelHistoryFetcher([_row("m-old", "user", "hi")])
        await _build(fetcher, event=_event(channel_id="dm:a", message_id="m-a"))
        await _build(fetcher, event=_event(channel_id="dm:b", message_id="m-b"))

        evictions = _collect(metric_reader).get("conversation_window.cache_evictions")
        assert evictions is not None, "an LRU eviction is charted"
        assert sum(dp.value for dp in _points(evictions)) == 1


class TestNoEmitPaths:
    async def test_disabled_config_emits_nothing(self, metric_reader: InMemoryMetricReader) -> None:
        fetcher = _FakeChannelHistoryFetcher([_row("m-old", "user", "hi")])
        await _build(fetcher, config=ConversationWindowConfig(enabled=False))

        metrics = _collect(metric_reader)
        assert "conversation_window.cache_access" not in metrics
        assert "conversation_window.fetch_duration" not in metrics
        assert "conversation_window.fallback" not in metrics
        assert not fetcher.calls, "a disabled window issues no fetch"

    async def test_channelless_event_emits_nothing(
        self, metric_reader: InMemoryMetricReader,
    ) -> None:
        # A TICK-shaped event with no channel scope is not a degradation — there
        # is nothing to reconstruct, so it charts no fallback.
        fetcher = _FakeChannelHistoryFetcher([_row("m-old", "user", "hi")])
        await _build(fetcher, event=_event(channel_id=None))
        assert "conversation_window.fallback" not in _collect(metric_reader)


class TestEmitNeverPropagates:
    async def test_record_helpers_swallow_instrument_errors(self) -> None:
        """A metric-export hiccup must never propagate out of the window build and
        undo the turn — the same best-effort contract :func:`record_deliberation`
        holds. Inject instruments that raise and prove every helper swallows it."""

        class _Boom:
            def add(self, *a: Any, **k: Any) -> None:
                raise RuntimeError("otel exporter down")

            def record(self, *a: Any, **k: Any) -> None:
                raise RuntimeError("otel exporter down")

        boom = _cwm._ConversationWindowInstruments(
            cache_access=_Boom(),  # type: ignore[arg-type]
            cache_evictions=_Boom(),  # type: ignore[arg-type]
            fetch_duration=_Boom(),  # type: ignore[arg-type]
            fallback=_Boom(),  # type: ignore[arg-type]
        )
        original = _cwm._instruments
        _cwm._instruments = boom
        try:
            _cwm.record_cache_access(hit=True)
            _cwm.record_cache_access(hit=False)
            _cwm.record_cache_eviction(1)
            _cwm.record_fetch_duration(12.5)
            _cwm.record_fallback(reason="fetch_failed")
        finally:
            _cwm._instruments = original
