"""RFC 0051 PR 6 (v0.3.10) — the go-live deliberation telemetry.

The observability that makes an active ``reasoning`` default safe to run, emitted
from the salience seam on the structured (``bid``/``plan``) rungs:

* ``deliberation.total`` (rate) + ``deliberation.duration`` (latency) on every
  deliberation;
* ``deliberation.suppressed`` charted by ``reason_code`` on a silence verdict;
* ``deliberation.budget_starved`` when the silence was a denied lease / exhausted
  ``interaction_budget_tokens`` (``reason_code=lease_denied``), distinct from a
  semantic "nothing to add".

The instruments are module-owned by ``_metrics_salience`` (``metrics.py`` is at
the 500-line cap), so this drives the real :func:`record_deliberation` path
through :func:`run_salience_gate` with the bid patched to control the verdict.
Under ``mode: off`` (the scalar gate) **no** ``deliberation.*`` metric emits — the
dark path stays byte-for-byte v0.3.8.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

import agents.observability._metrics_salience as _metrics_salience
import agents.observability.metrics as pmetrics
from agents.persona_runtime.salience_gate import run_salience_gate
from agents.persona_types import AgentEvent, EventType
from agents.response_gate import POLICY_ALWAYS, GateDecision
from agents.salience_bid import SalienceDecision
from agents.salience_deliberation import (
    MODE_BID,
    MODE_OFF,
    REASON_ADDS_SUBSTANCE,
    REASON_ALREADY_ANSWERED,
)

pytestmark = pytest.mark.asyncio

_BID_PATH = "agents.persona_runtime.salience_gate.evaluate_salience"
_LEASE_DENIED = "lease_denied"  # the budget/lease starvation reason (salience_bid)


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


def _open_floor_decision() -> GateDecision:
    return GateDecision(respond=True, policy=POLICY_ALWAYS, reason="policy_always")


def _event() -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": "Which datastore?", "respond_policy": "always", "salience_gated": True},
        channel_id="group:planning",
        sender_id="alice",
    )


def _stub_agent() -> MagicMock:
    agent = MagicMock()
    agent.agent_id = "ember-owl"
    agent.name = "Ember Owl"
    agent.role = "Planner"
    agent._llm_client = MagicMock()
    agent._format_event = MagicMock(return_value="formatted message")
    agent._build_seed_messages = AsyncMock(return_value=[
        {"role": "user", "content": "We should pick a cache datastore."},
        {"role": "user", "content": "Which datastore?"},
    ])
    agent._store_event_episode = AsyncMock(return_value=None)
    return agent


async def _drive(monkeypatch: pytest.MonkeyPatch, decision: SalienceDecision, *, mode: str) -> None:
    monkeypatch.setattr(_BID_PATH, AsyncMock(return_value=decision))
    await run_salience_gate(_stub_agent(), _event(), _open_floor_decision(), mode=mode)


class TestDeliberationTelemetry:
    async def test_speak_emits_rate_and_latency_without_suppress(
        self, metric_reader: InMemoryMetricReader, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _drive(
            monkeypatch,
            SalienceDecision(speak=True, score=None, reason=REASON_ADDS_SUBSTANCE),
            mode=MODE_BID,
        )
        metrics = _collect(metric_reader)
        assert _points(metrics.get("deliberation.total")), "rate counter emits"
        assert _points(metrics.get("deliberation.duration")), "latency histogram emits"
        assert "deliberation.suppressed" not in metrics, "a speak verdict suppresses nothing"
        assert "deliberation.budget_starved" not in metrics

    async def test_silence_charts_suppress_by_reason_code(
        self, metric_reader: InMemoryMetricReader, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _drive(
            monkeypatch,
            SalienceDecision(speak=False, score=None, reason=REASON_ALREADY_ANSWERED),
            mode=MODE_BID,
        )
        metrics = _collect(metric_reader)
        suppressed = metrics.get("deliberation.suppressed")
        assert suppressed is not None, "a silence verdict charts a suppression"
        codes = {dp.attributes.get("reason_code") for dp in _points(suppressed)}
        assert codes == {REASON_ALREADY_ANSWERED}, "silence is charted by its reason_code"
        # …and by mode, so the silence fraction (suppressed/total) is computable
        # per rung, not only in aggregate — total/duration/budget_starved all carry
        # `mode`, so suppressed must too or a mode-filtered ratio is meaningless.
        modes = {dp.attributes.get("mode") for dp in _points(suppressed)}
        assert modes == {MODE_BID}, "suppression carries the mode dimension too"
        # A semantic silence is NOT a budget starvation.
        assert "deliberation.budget_starved" not in metrics

    async def test_budget_starvation_is_a_distinct_counter(
        self, metric_reader: InMemoryMetricReader, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _drive(
            monkeypatch,
            SalienceDecision(speak=False, score=None, reason=_LEASE_DENIED),
            mode=MODE_BID,
        )
        metrics = _collect(metric_reader)
        assert _points(metrics.get("deliberation.budget_starved")), (
            "a lease_denied silence increments the budget-starvation counter"
        )
        # It still rides the generic suppress counter too (charted by reason).
        suppressed = _points(metrics.get("deliberation.suppressed"))
        codes = {dp.attributes.get("reason_code") for dp in suppressed}
        assert codes == {_LEASE_DENIED}

    async def test_record_deliberation_never_propagates_instrument_errors(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A metric-export hiccup must never propagate out of the seam and undo
        the turn — the same best-effort contract _emit_deliberated_audit holds (the
        deliberation already happened, RFC 0051 §Security). record_deliberation now
        runs on the active-by-default ``bid`` path, so an unguarded OTel raise would
        break real turns. Drives both a speak and a silence verdict (the latter
        exercises every instrument, including budget_starved)."""

        class _Boom:
            def add(self, *a: Any, **k: Any) -> None:
                raise RuntimeError("otel exporter down")

            def record(self, *a: Any, **k: Any) -> None:
                raise RuntimeError("otel exporter down")

        boom = _metrics_salience._DeliberationInstruments(
            total=_Boom(), suppressed=_Boom(), duration=_Boom(), budget_starved=_Boom(),
        )
        monkeypatch.setattr(_metrics_salience, "_deliberation", boom)
        # Neither call may raise.
        _metrics_salience.record_deliberation(
            mode=MODE_BID, reason_code=REASON_ADDS_SUBSTANCE, spoke=True, duration_ms=1.0,
        )
        _metrics_salience.record_deliberation(
            mode=MODE_BID, reason_code=_LEASE_DENIED, spoke=False, duration_ms=2.0,
        )

    async def test_off_mode_emits_no_deliberation_metrics(
        self, metric_reader: InMemoryMetricReader, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The dark scalar gate (mode: off) must emit no deliberation.* telemetry."""
        await _drive(
            monkeypatch,
            SalienceDecision(speak=True, score=0.9, reason="salient"),
            mode=MODE_OFF,
        )
        metrics = _collect(metric_reader)
        for name in (
            "deliberation.total",
            "deliberation.duration",
            "deliberation.suppressed",
            "deliberation.budget_starved",
        ):
            assert name not in metrics, f"{name} must not emit under the off scalar gate"
