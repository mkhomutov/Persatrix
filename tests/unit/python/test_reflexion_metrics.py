"""RFC 0051 PR 9 (v0.3.10, Phase 5b) — reflexion-loop telemetry.

The Phase 5 reflexion loop (critic→revise, default off) sharpens a ``mode: plan``
post before it ships. PR 9 adds the observability that makes the opt-in loop
legible, reusing the Phase 3 module-owned instrument shapes in
:mod:`agents.observability._metrics_salience`:

* ``reflexion.runs`` — every loop that ran on an admitted ``mode: plan`` +
  ``reasoning.revise ≥ 1`` turn, charted by ``outcome`` (``revised`` when the
  critic flagged the draft and a revise rewrote it; ``noop`` when a strong draft
  or a fail-soft degradation kept the composed draft) — the **draft-changed /
  no-op-revise** signal;
* ``reflexion.rounds`` — the **revise-round counter** (the sum of rounds that
  actually rewrote the draft; a no-op contributes 0).

The instruments are module-owned by ``_metrics_salience`` (``metrics.py`` is at
the 500-line cap), so this drives the real :func:`record_reflexion` emit through
:func:`agents.persona_runtime.reflexion.maybe_revise_channel_message` — the only
emit site — and asserts the never-propagate guard directly. A turn that does NOT
reach the loop (no plan, ``revise = 0``, no channel message) emits nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

import agents.observability._metrics_salience as _metrics_salience
import agents.observability.metrics as pmetrics
from agents.generated import wallet_pb2 as walletpb
from agents.llm_client import LLMClient, LLMResponse
from agents.model_aliases import use_alias_map
from agents.persona_runtime.deliberation_plan import CompositionPlan
from agents.persona_runtime.reflexion import maybe_revise_channel_message
from agents.persona_runtime.salience_gate import SalienceOutcome
from agents.persona_types import ActionType, AgentAction
from agents.wallet_client import BudgetExceededError

pytestmark = pytest.mark.asyncio

# The mock ``fast`` alias the critic resolves (mirrors the reflexion unit tests).
_FAST_ALIAS_MAP: dict[str, dict[str, Any]] = {
    "fast": {
        "provider": "mock",
        "model": "mock-fast",
        "input_per_1m_tokens": 0.0,
        "output_per_1m_tokens": 0.0,
    },
}
_COMPOSE_MODEL = "mock-quality"
_PLAN = CompositionPlan(
    intent="explain why Redis fits the cache layer",
    key_points=("low-latency reads",),
    addressed_to="channel",
    avoid_restating=(),
)
_DRAFT = "Redis is the obvious fit for a cache layer."


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


def _provider(*texts: str, raises: Exception | None = None) -> AsyncMock:
    provider = AsyncMock()
    if raises is not None:
        provider.create_message = AsyncMock(side_effect=raises)
    else:
        provider.create_message = AsyncMock(side_effect=[LLMResponse(text=t) for t in texts])
    return provider


def _agent(provider: AsyncMock) -> Any:
    agent = AsyncMock()
    agent.name = "Ember Owl"
    agent.role = "VP of Engineering"
    agent.config = {"model": _COMPOSE_MODEL}
    agent._llm_client = LLMClient(provider)
    return agent


def _channel_actions(content: str = _DRAFT) -> list[AgentAction]:
    return [
        AgentAction(
            action_type=ActionType.SEND_CHANNEL_MESSAGE,
            payload={"channel_id": "group:planning", "content": content, "mentions": []},
        ),
    ]


async def _glue(provider: AsyncMock, salience: SalienceOutcome | None) -> list[AgentAction]:
    with use_alias_map(_FAST_ALIAS_MAP):
        return await maybe_revise_channel_message(
            _agent(provider), _channel_actions(), salience,
            cause=walletpb.CAUSE_CHANNEL_MESSAGE, agent_id="ember-owl",
            interaction_id="i-1", max_tokens=4096,
        )


class TestReflexionTelemetry:
    async def test_revised_turn_charts_outcome_and_rounds(
        self, metric_reader: InMemoryMetricReader,
    ) -> None:
        # critic(weak) → revise → critic(strong) → stop: one real rewrite.
        provider = _provider("weak: yes\ncritique: bury the lede", "A sharper draft.", "weak: no")
        await _glue(provider, SalienceOutcome(silence=False, plan=_PLAN, revise=2))

        metrics = _collect(metric_reader)
        runs = metrics.get("reflexion.runs")
        assert runs is not None, "a reflexion loop that ran charts a run"
        outcomes = {dp.attributes.get("outcome") for dp in _points(runs)}
        assert outcomes == {"revised"}, "a rewrite is charted as outcome=revised"
        rounds = metrics.get("reflexion.rounds")
        assert rounds is not None, "a rewrite charts its revise-round count"
        assert sum(dp.value for dp in _points(rounds)) == 1, "exactly one round rewrote"

    async def test_multi_round_rewrite_sums_each_round(
        self, metric_reader: InMemoryMetricReader,
    ) -> None:
        """``revise=2`` with both rounds flagged weak rewrites twice; ``reflexion.rounds``
        is the *sum* (2), the numerator of the mean-rounds-per-rewrite ratio. The
        single-round test pins ``add(1)``; this pins the per-round accumulation."""
        # critic(weak) → revise → critic(weak) → revise: two real rewrites, capped at 2.
        provider = _provider(
            "weak: yes\ncritique: first", "draft v1",
            "weak: yes\ncritique: second", "draft v2",
        )
        await _glue(provider, SalienceOutcome(silence=False, plan=_PLAN, revise=2))

        metrics = _collect(metric_reader)
        outcomes = {dp.attributes.get("outcome") for dp in _points(metrics.get("reflexion.runs"))}
        assert outcomes == {"revised"}
        rounds = metrics.get("reflexion.rounds")
        assert sum(dp.value for dp in _points(rounds)) == 2, "both rewrite rounds are summed"

    async def test_strong_draft_charts_noop_without_rounds(
        self, metric_reader: InMemoryMetricReader,
    ) -> None:
        provider = _provider("weak: no")  # strong draft: critic passes, no rewrite
        await _glue(provider, SalienceOutcome(silence=False, plan=_PLAN, revise=1))

        metrics = _collect(metric_reader)
        runs = metrics.get("reflexion.runs")
        assert runs is not None, "a loop that ran but kept the draft still charts a run"
        outcomes = {dp.attributes.get("outcome") for dp in _points(runs)}
        assert outcomes == {"noop"}, "a kept draft is charted as outcome=noop"
        # A no-op contributes nothing to the round counter (mean rounds stays honest).
        assert "reflexion.rounds" not in metrics, "a no-op adds no revise rounds"

    async def test_fail_soft_degradation_charts_degraded_not_noop(
        self, metric_reader: InMemoryMetricReader,
    ) -> None:
        """A fail-soft degradation (here a denied critic lease) must chart as its own
        ``outcome=degraded``, NOT fold into ``noop`` with a genuinely strong draft. A
        silent fast-model outage / budget starvation that disables the loop is not the
        feature working as intended — the same signal-separation the sibling
        ``deliberation.budget_starved`` counter exists to give."""
        provider = _provider(raises=BudgetExceededError("no budget"))
        await _glue(provider, SalienceOutcome(silence=False, plan=_PLAN, revise=1))

        metrics = _collect(metric_reader)
        outcomes = {dp.attributes.get("outcome") for dp in _points(metrics.get("reflexion.runs"))}
        assert outcomes == {"degraded"}, "a fail-soft degradation is its own outcome, not noop"
        assert "reflexion.rounds" not in metrics, "a degradation adds no revise rounds"

    async def test_rewrite_back_to_draft_charts_noop_with_zero_rounds(
        self, metric_reader: InMemoryMetricReader,
    ) -> None:
        """A later round rewrites byte-for-byte back to the original (A→B→A): the net
        draft is unchanged, so it charts ``noop`` and contributes **0** rounds. Counting
        the rounds here would inflate ``reflexion.rounds / runs{outcome=revised}`` with a
        turn the denominator never counts (the round counter gates on the net change)."""
        provider = _provider(
            "weak: yes\ncritique: first", "a different draft",
            "weak: yes\ncritique: second", _DRAFT,  # round 2 returns the original draft
        )
        await _glue(provider, SalienceOutcome(silence=False, plan=_PLAN, revise=2))

        metrics = _collect(metric_reader)
        outcomes = {dp.attributes.get("outcome") for dp in _points(metrics.get("reflexion.runs"))}
        assert outcomes == {"noop"}, "a net-unchanged draft is a noop even after real rewrites"
        assert "reflexion.rounds" not in metrics, "a net-unchanged turn contributes 0 rounds"

    async def test_loop_not_reached_emits_nothing(
        self, metric_reader: InMemoryMetricReader,
    ) -> None:
        """A turn that never reaches the loop — ``revise = 0`` (the single-pass
        default), no plan, or no channel message — charts no reflexion telemetry."""
        provider = _provider()
        for salience in (
            None,
            SalienceOutcome(silence=False, plan=_PLAN, revise=0),
            SalienceOutcome(silence=False, plan=None, revise=2),
        ):
            await _glue(provider, salience)
        metrics = _collect(metric_reader)
        assert "reflexion.runs" not in metrics
        assert "reflexion.rounds" not in metrics
        provider.create_message.assert_not_awaited()

    async def test_record_reflexion_never_propagates_instrument_errors(self) -> None:
        """A metric-export hiccup must never propagate out of the glue and undo the
        turn — the same best-effort contract :func:`record_deliberation` holds (the
        rewrite already happened). Inject instruments that raise on ``add`` and prove
        :func:`record_reflexion` swallows them."""

        class _Boom:
            def add(self, *a: Any, **k: Any) -> None:
                raise RuntimeError("otel exporter down")

        boom = _metrics_salience._ReflexionInstruments(
            runs=_Boom(),  # type: ignore[arg-type]
            rounds=_Boom(),  # type: ignore[arg-type]
        )
        original = _metrics_salience._reflexion
        _metrics_salience._reflexion = boom
        try:
            # No outcome — revised, noop, or degraded — may raise.
            _metrics_salience.record_reflexion(rounds=1, changed=True, degraded=False)
            _metrics_salience.record_reflexion(rounds=0, changed=False, degraded=False)
            _metrics_salience.record_reflexion(rounds=0, changed=False, degraded=True)
        finally:
            _metrics_salience._reflexion = original
