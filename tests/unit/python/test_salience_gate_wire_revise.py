"""RFC 0051 PR 8 (v0.3.10) — the reflexion round count crosses the wire.

The seam lifts the channel's resolved ``reasoning_revise`` off the
``ChannelMessageEvent`` payload onto ``SalienceOutcome.revise`` — but only on the
``mode: plan`` speak path, since the critic re-reads the draft *against* the plan
(a revise without one is inert). This pins that read: the count survives under
``plan``, is forced to ``0`` on the silence-only ``bid`` rung and the scalar
``off`` gate, is clamped to the hard cap, and degrades to ``0`` on a junk value or
an unparseable plan — additive across a mixed-version deployment, exactly like
``reasoning_mode`` (``test_salience_gate_wire_mode.py``).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.llm_client import LLMClient, LLMResponse
from agents.model_aliases import use_alias_map
from agents.persona_runtime.reflexion import MAX_REVISE_ROUNDS
from agents.persona_runtime.salience_gate import run_salience_gate
from agents.response_gate import POLICY_ALWAYS, GateDecision

pytestmark = pytest.mark.asyncio

_FAST_ALIAS_MAP: dict[str, dict[str, Any]] = {
    "fast": {
        "provider": "mock",
        "model": "mock-fast",
        "input_per_1m_tokens": 0.0,
        "output_per_1m_tokens": 0.0,
    },
}

_PLAN_RESPONSE = (
    "should_post: yes\n"
    "reason_code: adds_substance\n"
    "intent: surface the write-path risk no one has named\n"
    "key_points: Redis serializes writes\n"
)
# A speak verdict with NO parseable intent → plan parses to None (fail-open).
_PLAN_RESPONSE_NO_INTENT = "should_post: yes\nreason_code: adds_substance\n"
_SCALAR_SPEAK = "speak: yes\nscore: 0.9\n"


def _open_floor_decision() -> GateDecision:
    return GateDecision(respond=True, policy=POLICY_ALWAYS, reason="policy_always")


def _event(*, reasoning_mode: str | None, reasoning_revise: Any = None) -> Any:
    from agents.persona_types import AgentEvent, EventType  # noqa: PLC0415

    payload: dict[str, Any] = {
        "content": "What datastore should we pick for the cache?",
        "respond_policy": "always",
        "salience_gated": True,
    }
    if reasoning_mode is not None:
        payload["reasoning_mode"] = reasoning_mode
    if reasoning_revise is not None:
        payload["reasoning_revise"] = reasoning_revise
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload=payload,
        channel_id="group:planning",
        sender_id="alice",
    )


def _agent(response_text: str | None) -> MagicMock:
    provider = AsyncMock()
    provider.create_message = AsyncMock(return_value=LLMResponse(text=response_text))
    agent = MagicMock()
    agent.agent_id = "ember-owl"
    agent.name = "Ember Owl"
    agent.role = "Planner"
    agent._llm_client = LLMClient(provider)
    agent._format_event = MagicMock(return_value="formatted message")
    agent._build_seed_messages = AsyncMock(return_value=[
        {"role": "user", "content": "We should pick a cache datastore."},
        {"role": "user", "content": "What datastore should we pick for the cache?"},
    ])
    agent._store_event_episode = AsyncMock(return_value=None)
    return agent


async def _gate(response_text: str | None, *, mode: str | None, revise: Any = None):
    with use_alias_map(_FAST_ALIAS_MAP):
        return await run_salience_gate(
            _agent(response_text),
            _event(reasoning_mode=mode, reasoning_revise=revise),
            _open_floor_decision(),
        )


class TestReviseFromWire:
    async def test_plan_rung_carries_the_revise_count(self):
        outcome = await _gate(_PLAN_RESPONSE, mode="plan", revise=2)
        assert outcome is not None and outcome.plan is not None
        assert outcome.revise == 2

    async def test_plan_rung_with_revise_one(self):
        outcome = await _gate(_PLAN_RESPONSE, mode="plan", revise=1)
        assert outcome is not None
        assert outcome.revise == 1

    async def test_bid_rung_forces_revise_zero(self):
        """``bid`` is the silence-only rung — no plan rides back, so revise is
        pinned to 0 even if the wire carries a count (reflexion needs a plan)."""
        outcome = await _gate(_PLAN_RESPONSE, mode="bid", revise=2)
        assert outcome is not None and outcome.plan is None
        assert outcome.revise == 0

    async def test_off_rung_forces_revise_zero(self):
        outcome = await _gate(_SCALAR_SPEAK, mode=None, revise=2)
        assert outcome is not None
        assert outcome.revise == 0

    async def test_absent_revise_key_is_zero(self):
        outcome = await _gate(_PLAN_RESPONSE, mode="plan", revise=None)
        assert outcome is not None and outcome.plan is not None
        assert outcome.revise == 0

    async def test_revise_above_cap_is_clamped(self):
        outcome = await _gate(_PLAN_RESPONSE, mode="plan", revise=9)
        assert outcome is not None
        assert outcome.revise == MAX_REVISE_ROUNDS

    async def test_unparseable_plan_forces_revise_zero(self):
        """A speak verdict whose plan fails to parse composes unrevised (revise 0)
        rather than reflecting against a plan that does not exist (fail-open)."""
        outcome = await _gate(_PLAN_RESPONSE_NO_INTENT, mode="plan", revise=2)
        assert outcome is not None and outcome.plan is None
        assert outcome.revise == 0

    @pytest.mark.parametrize("junk", [True, "2", 1.5, -1, 0])
    async def test_junk_revise_value_is_zero(self, junk: Any):
        """A bool (int subclass), a string, a float, a negative, or an explicit 0
        all resolve to single-pass — only a positive int is a round count."""
        outcome = await _gate(_PLAN_RESPONSE, mode="plan", revise=junk)
        assert outcome is not None
        assert outcome.revise == 0
