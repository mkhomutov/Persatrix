"""RFC 0051 Phase 2 (v0.3.10) — the plan rides back on ``SalienceOutcome``.

PR 3 of the RFC 0051 PR plan (``docs/rfcs/0051-pr-plan.md``). The pure parser /
renderer are pinned in ``test_deliberation_plan.py``; this file pins the *seam*
wiring: :func:`agents.persona_runtime.salience_gate.run_salience_gate` carrying a
:class:`CompositionPlan` back on :class:`SalienceOutcome` on the
``should_post=true`` path under ``mode: plan`` — and **not** under ``bid`` /
``off`` / on a silence verdict ([RFC 0051 §C](../../docs/rfcs/0051-reasoning-before-posting.md)).

Unlike ``test_salience_gate_deliberation_audit.py`` (which patches the bid),
this drives the **real** bid through a mock ``fast`` provider so the full
dataflow is exercised end-to-end: the bid's structured response → the verdict →
the raw text surfaced to the seam → ``parse_plan`` → ``SalienceOutcome.plan``.

Load-bearing contracts:

* **Plan only on the ``plan`` rung.** ``mode: bid`` is the silence-verdict-only
  rung — even a plan-shaped response yields ``plan=None`` there.
* **Plan only when speaking.** A ``should_post: no`` verdict short-circuits to
  silence; no plan is carried (there is no post to plan).
* **Fail-closed to "no plan", never blocking the post.** A ``should_post: yes``
  with no parseable ``intent`` still speaks (``silence=False``) but carries
  ``plan=None`` — compose proceeds *unplanned* rather than being blocked.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.llm_client import LLMClient, LLMResponse
from agents.model_aliases import use_alias_map
from agents.persona_runtime.salience_gate import run_salience_gate
from agents.response_gate import POLICY_ALWAYS, GateDecision
from agents.salience_deliberation import MODE_BID, MODE_OFF, MODE_PLAN

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
    "key_points: Redis serializes writes; our p99 is write-heavy\n"
    "addressed_to: iron-fox\n"
    "avoid_restating: that Redis is fast for reads\n"
)


def _open_floor_decision() -> GateDecision:
    return GateDecision(respond=True, policy=POLICY_ALWAYS, reason="policy_always")


def _event() -> Any:
    from agents.persona_types import AgentEvent, EventType  # noqa: PLC0415

    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={
            "content": "What datastore should we pick for the cache?",
            "respond_policy": "always",
            "salience_gated": True,
        },
        channel_id="group:planning",
        sender_id="alice",
    )


def _agent(response_text: str | None) -> MagicMock:
    """A stub persona with a **real** ``LLMClient`` so the seam runs the real
    bid. The provider returns ``response_text`` for the ``fast`` deliberation."""
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
        {"role": "assistant", "content": "Redis is the obvious fit."},
        {"role": "user", "content": "What datastore should we pick for the cache?"},
    ])
    agent._store_event_episode = AsyncMock(return_value=None)
    return agent


async def _gate(response_text: str | None, *, mode: str):
    with use_alias_map(_FAST_ALIAS_MAP):
        return await run_salience_gate(
            _agent(response_text), _event(), _open_floor_decision(), mode=mode,
        )


class TestPlanRidesOnSpeakUnderPlanMode:
    async def test_plan_mode_speak_carries_the_parsed_plan(self):
        outcome = await _gate(_PLAN_RESPONSE, mode=MODE_PLAN)
        assert outcome is not None
        assert outcome.silence is False
        assert outcome.plan is not None
        assert outcome.plan.intent == "surface the write-path risk no one has named"
        assert outcome.plan.key_points == (
            "Redis serializes writes",
            "our p99 is write-heavy",
        )
        assert outcome.plan.addressed_to == "iron-fox"

    async def test_speak_path_still_reuses_user_message_and_seed(self):
        """The plan is *additive* — the speak outcome still hands back the
        reusable ``user_message`` + ``seed`` it always did (PR 2 contract)."""
        outcome = await _gate(_PLAN_RESPONSE, mode=MODE_PLAN)
        assert outcome is not None and outcome.silence is False
        assert outcome.user_message == "formatted message"
        assert outcome.seed is not None and len(outcome.seed) == 3


class TestNoPlanOffTheSpeakPlanPath:
    async def test_bid_mode_carries_no_plan(self):
        """``bid`` is the silence-verdict-only rung — a plan-shaped response is
        ignored for plan purposes (the plan threads only under ``plan``)."""
        outcome = await _gate(_PLAN_RESPONSE, mode=MODE_BID)
        assert outcome is not None
        assert outcome.silence is False
        assert outcome.plan is None

    async def test_silence_verdict_carries_no_plan(self):
        outcome = await _gate(
            "should_post: no\nreason_code: already_answered\n"
            "intent: this should never be read on a silence verdict\n",
            mode=MODE_PLAN,
        )
        assert outcome is not None
        assert outcome.silence is True
        assert outcome.plan is None

    async def test_speak_with_unparseable_plan_speaks_with_no_plan(self):
        """Fail-closed to "no plan", not to silence: a speak verdict with no
        parseable ``intent`` still posts (``silence=False``) but unplanned
        (``plan=None``) — the post is never blocked (RFC 0051 §Phase 2)."""
        outcome = await _gate(
            "should_post: yes\nreason_code: adds_substance\n", mode=MODE_PLAN,
        )
        assert outcome is not None
        assert outcome.silence is False
        assert outcome.plan is None


class TestScalarPathUnaffected:
    async def test_off_mode_carries_no_plan(self):
        """``mode: off`` is the scalar score gate — no deliberation text is even
        surfaced, so there is never a plan."""
        outcome = await _gate("speak: yes\nscore: 0.95", mode=MODE_OFF)
        assert outcome is not None
        assert outcome.silence is False
        assert outcome.plan is None
