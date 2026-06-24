"""RFC 0051 PR 6 (v0.3.10) — the go-live wire: the seam resolves the reasoning
rung off the ``ChannelMessageEvent`` payload when the caller pins no ``mode``.

The production ``action_loop`` call site passes no ``mode`` (``mode=None``), so
:func:`agents.persona_runtime.salience_gate.run_salience_gate` reads the channel's
resolved ``reasoning_mode`` off the wire payload — the field
``channel_wire_metadata.channel_event_payload`` lifts off the proto. This is the
line that flips the dark Phases 1–2 mechanism live. The sibling
``test_salience_gate_plan.py`` pins the structured paths via an explicit ``mode=``
override; this file pins that those same paths are reached *from the wire*, and
that an absent / empty / unknown rung fails safe to the ``off`` scalar gate.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.llm_client import LLMClient, LLMResponse
from agents.model_aliases import use_alias_map
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
    "addressed_to: iron-fox\n"
)
_SCALAR_SPEAK = "speak: yes\nscore: 0.9\n"


def _open_floor_decision() -> GateDecision:
    return GateDecision(respond=True, policy=POLICY_ALWAYS, reason="policy_always")


def _event(reasoning_mode: str | None) -> Any:
    from agents.persona_types import AgentEvent, EventType  # noqa: PLC0415

    payload: dict[str, Any] = {
        "content": "What datastore should we pick for the cache?",
        "respond_policy": "always",
        "salience_gated": True,
    }
    if reasoning_mode is not None:
        payload["reasoning_mode"] = reasoning_mode
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


async def _gate_from_wire(response_text: str | None, *, reasoning_mode: str | None):
    """Drive the seam the way production does — no ``mode=`` override, so the rung
    is read off the wire payload."""
    with use_alias_map(_FAST_ALIAS_MAP):
        return await run_salience_gate(
            _agent(response_text), _event(reasoning_mode), _open_floor_decision(),
        )


class TestWireDrivenReasoningMode:
    async def test_plan_rung_on_the_wire_reaches_the_plan_path(self):
        """``reasoning_mode: plan`` on the payload → the seam parses the plan,
        no explicit ``mode=`` needed (the go-live path)."""
        outcome = await _gate_from_wire(_PLAN_RESPONSE, reasoning_mode="plan")
        assert outcome is not None
        assert outcome.silence is False
        assert outcome.plan is not None
        assert outcome.plan.intent == "surface the write-path risk no one has named"

    async def test_bid_rung_on_the_wire_is_structured_but_carries_no_plan(self):
        """``reasoning_mode: bid`` → structured silence verdict only; the same
        plan-shaped response yields no plan (bid is the silence-only rung)."""
        outcome = await _gate_from_wire(_PLAN_RESPONSE, reasoning_mode="bid")
        assert outcome is not None
        assert outcome.silence is False
        assert outcome.plan is None

    async def test_absent_rung_falls_back_to_the_scalar_off_gate(self):
        """No ``reasoning_mode`` key (a pre-v0.3.10 producer) → the off scalar
        score gate: a scalar speak verdict speaks, and no plan is produced."""
        outcome = await _gate_from_wire(_SCALAR_SPEAK, reasoning_mode=None)
        assert outcome is not None
        assert outcome.silence is False
        assert outcome.plan is None

    async def test_empty_rung_is_off(self):
        """An empty-string rung (the proto3 zero value) is the off scalar gate."""
        outcome = await _gate_from_wire(_SCALAR_SPEAK, reasoning_mode="")
        assert outcome is not None
        assert outcome.silence is False
        assert outcome.plan is None

    async def test_unknown_rung_fails_safe_to_off(self):
        """An unrecognised rung (forward/garbled value) resolves to off rather
        than reaching the structured path — fail-safe, not fail-structured."""
        outcome = await _gate_from_wire(_SCALAR_SPEAK, reasoning_mode="ponder")
        assert outcome is not None
        assert outcome.silence is False
        assert outcome.plan is None
