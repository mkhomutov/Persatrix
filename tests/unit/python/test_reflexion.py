"""RFC 0051 Phase 5a (v0.3.10) — the reflexion critic→revise loop.

The reflexion loop runs *after* the Tier-C compose (RFC 0051 §Phase 5): a cheap
``fast`` **critic** re-reads the composed draft against the private
``CompositionPlan`` and, only if it flags weakness, a ``quality`` **revise** pass
rewrites it — bounded to ``reasoning.revise`` rounds (``≤ 2``) and **fail-soft**
(a parse/critic failure or an exhausted lease degrades to the last good draft
rather than blocking the post the gate already admitted).

These tests pin that contract in isolation with a mock provider (the same
scaffold the bid reasoning tests use): the no-op-on-strong-draft path, the
revise-on-weak path, the round limit, the cost shape (critic on ``fast``, revise
on the compose model), and every fail-soft degradation. The privacy wall (the
draft + critic note never leak) is pinned by the integration no-leak test
(``tests/integration/test_deliberation_no_leak.py``, extended in PR 9).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from agents.llm_client import LLMClient, LLMResponse
from agents.model_aliases import use_alias_map
from agents.persona_runtime.deliberation_plan import CompositionPlan
from agents.persona_runtime.reflexion import (
    MAX_REVISE_ROUNDS,
    ReflexionResult,
    maybe_revise_channel_message,
    run_reflexion,
)
from agents.persona_runtime.salience_gate import SalienceOutcome
from agents.persona_types import ActionType, AgentAction
from agents.wallet_client import BudgetExceededError

# The mock ``fast`` alias the critic resolves (mirrors the bid reasoning tests).
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
    key_points=("low-latency reads", "TTL eviction"),
    addressed_to="channel",
    avoid_restating=("that we need a cache",),
)

_DRAFT = "Redis is the obvious fit for a cache layer."


def _provider(*texts: str | None, raises: Exception | None = None) -> AsyncMock:
    """A mock provider whose ``create_message`` returns ``texts`` in order
    (one per LLM call), or raises ``raises`` on the first call."""
    provider = AsyncMock()
    if raises is not None:
        provider.create_message = AsyncMock(side_effect=raises)
    else:
        provider.create_message = AsyncMock(
            side_effect=[LLMResponse(text=t) for t in texts],
        )
    return provider


async def _reflect(
    provider: AsyncMock,
    *,
    revise: int = 1,
    draft: str = _DRAFT,
    plan: CompositionPlan | None = _PLAN,
) -> ReflexionResult:
    client = LLMClient(provider)
    with use_alias_map(_FAST_ALIAS_MAP):
        return await run_reflexion(
            llm_client=client,
            draft=draft,
            plan=plan,
            revise=revise,
            persona_name="Ember Owl",
            persona_role="VP of Engineering",
            compose_model=_COMPOSE_MODEL,
            agent_id="ember-owl",
        )


class TestNoOpPaths:
    """A loop that should not run makes zero LLM calls and returns the draft
    verbatim — the cheapest possible degenerate case."""

    async def test_revise_zero_is_a_noop(self):
        provider = _provider()
        result = await _reflect(provider, revise=0)
        assert result == ReflexionResult(text=_DRAFT, rounds=0, changed=False)
        provider.create_message.assert_not_awaited()

    async def test_no_plan_is_a_noop(self):
        """The critic re-reads the draft *against the plan* — with no plan there
        is nothing to critique against, so the loop is inert (it never blocks a
        post just because the plan failed to parse)."""
        provider = _provider()
        result = await _reflect(provider, revise=2, plan=None)
        assert result.text == _DRAFT
        assert result.changed is False
        provider.create_message.assert_not_awaited()

    async def test_blank_draft_is_a_noop(self):
        provider = _provider()
        result = await _reflect(provider, draft="   ")
        assert result.changed is False
        provider.create_message.assert_not_awaited()


class TestCriticGate:
    """The critic decides whether the (quality) revise pass runs at all — the
    efficiency win: a strong draft costs only the cheap critic, never a rewrite."""

    async def test_strong_draft_is_a_noop_after_one_critic_call(self):
        provider = _provider("weak: no")
        result = await _reflect(provider, revise=2)
        assert result == ReflexionResult(text=_DRAFT, rounds=0, changed=False)
        # The critic ran once; the (expensive) revise never did.
        assert provider.create_message.await_count == 1

    async def test_critic_runs_on_the_fast_model(self):
        """The critic is a cheap judgement, billed on the leased ``fast`` alias —
        it is NOT one of the ``N+1`` quality composes the RFC cost model counts."""
        provider = _provider("weak: no")
        await _reflect(provider, revise=1)
        assert provider.create_message.await_args.kwargs["model"] == "mock-fast"

    async def test_parse_failure_keeps_the_draft(self):
        """Fail-soft, opposite the gate's bias-to-silence: an unparseable critic
        verdict degrades to 'strong' (keep the draft), never blocks the post."""
        provider = _provider("the model rambled with no verdict line")
        result = await _reflect(provider, revise=1)
        assert result.text == _DRAFT
        assert result.changed is False
        assert provider.create_message.await_count == 1


class TestRevisePath:
    """A weak draft triggers exactly one quality rewrite per flagged round."""

    async def test_weak_draft_is_revised(self):
        revised = "Redis fits: sub-millisecond reads and native TTL eviction."
        provider = _provider(
            "weak: yes\ncritique: doesn't land the key points", revised, "weak: no",
        )
        result = await _reflect(provider, revise=2)
        assert result.text == revised
        assert result.changed is True
        assert result.rounds == 1
        # critic(weak) → revise → critic(strong) → stop.
        assert provider.create_message.await_count == 3

    async def test_revise_runs_on_the_compose_model(self):
        revised = "A sharper draft."
        provider = _provider("weak: yes", revised, "weak: no")
        await _reflect(provider, revise=1)
        # call 0 = critic (fast); call 1 = revise (compose/quality model).
        models = [c.kwargs["model"] for c in provider.create_message.await_args_list]
        assert models[0] == "mock-fast"
        assert models[1] == _COMPOSE_MODEL

    async def test_round_limit_caps_revisions(self):
        """``revise`` bounds the rounds; with every round flagged weak the loop
        stops at the cap and never exceeds it (cost ceiling)."""
        # 2 rounds requested, critic always weak → critic,revise,critic,revise.
        provider = _provider(
            "weak: yes", "draft v1",
            "weak: yes", "draft v2",
        )
        result = await _reflect(provider, revise=2)
        assert result.text == "draft v2"
        assert result.rounds == 2
        assert provider.create_message.await_count == 4

    async def test_revise_count_is_hard_capped(self):
        """A request above ``MAX_REVISE_ROUNDS`` is clamped — the config validate
        also rejects it, but the loop is defensive in depth."""
        assert MAX_REVISE_ROUNDS == 2
        # 9 requested; critic always weak. Must stop at MAX_REVISE_ROUNDS rounds.
        provider = _provider(
            "weak: yes", "v1",
            "weak: yes", "v2",
            "weak: yes", "v3",  # never reached
        )
        result = await _reflect(provider, revise=9)
        assert result.rounds == MAX_REVISE_ROUNDS
        assert provider.create_message.await_count == 2 * MAX_REVISE_ROUNDS


class TestFailSoft:
    """Every failure degrades to the last good draft — never blocks the post."""

    async def test_critic_lease_denied_keeps_draft(self):
        provider = _provider(raises=BudgetExceededError("no budget"))
        result = await _reflect(provider, revise=2)
        assert result.text == _DRAFT
        assert result.changed is False

    async def test_revise_error_degrades_to_last_good_draft(self):
        # critic flags weak, but the revise call errors → keep the pre-revise draft.
        provider = AsyncMock()
        provider.create_message = AsyncMock(
            side_effect=[LLMResponse(text="weak: yes"), RuntimeError("provider down")],
        )
        result = await _reflect(provider, revise=2)
        assert result.text == _DRAFT
        assert result.changed is False

    async def test_revise_empty_output_degrades_to_last_good_draft(self):
        provider = _provider("weak: yes", "   ")
        result = await _reflect(provider, revise=1)
        assert result.text == _DRAFT
        assert result.changed is False

    async def test_lease_exhausted_mid_loop_keeps_first_revision(self):
        """A multi-round loop that exhausts its lease on a later round keeps the
        last *successful* revision rather than discarding it."""
        v1 = "first improved draft"
        provider = AsyncMock()
        provider.create_message = AsyncMock(
            side_effect=[
                LLMResponse(text="weak: yes"), LLMResponse(text=v1),  # round 1 ok
                BudgetExceededError("exhausted"),                      # round 2 critic denied
            ],
        )
        result = await _reflect(provider, revise=2)
        assert result.text == v1
        assert result.changed is True
        assert result.rounds == 1

    async def test_unresolvable_fast_alias_is_a_noop(self):
        """If the ``fast`` critic alias does not resolve the loop degrades to a
        no-op (keep the draft) rather than crashing the compose hot path."""
        provider = _provider("weak: yes", "revised")
        client = LLMClient(provider)
        # No use_alias_map → 'fast' is unresolvable.
        result = await run_reflexion(
            llm_client=client,
            draft=_DRAFT,
            plan=_PLAN,
            revise=1,
            persona_name="Ember Owl",
            persona_role="VP of Engineering",
            compose_model=_COMPOSE_MODEL,
            agent_id="ember-owl",
        )
        assert result.text == _DRAFT
        assert result.changed is False
        provider.create_message.assert_not_awaited()


def _agent(provider: AsyncMock) -> Any:
    """A minimal stand-in for ``_LLMPersonaAgent`` exposing exactly the surface
    the glue reads — identity, config, and the leased client."""
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


async def _glue(
    provider: AsyncMock, actions: list[AgentAction], salience: SalienceOutcome | None,
) -> list[AgentAction]:
    with use_alias_map(_FAST_ALIAS_MAP):
        return await maybe_revise_channel_message(
            _agent(provider), actions, salience,
            cause=0, agent_id="ember-owl", interaction_id="i-1", max_tokens=4096,
        )


class TestActionLoopGlue:
    """``maybe_revise_channel_message`` — the only AgentAction-aware seam."""

    async def test_noop_when_no_salience(self):
        provider = _provider()
        actions = _channel_actions()
        out = await _glue(provider, actions, None)
        assert out is actions  # identity-preserved no-op
        provider.create_message.assert_not_awaited()

    async def test_noop_when_revise_zero(self):
        provider = _provider()
        actions = _channel_actions()
        out = await _glue(provider, actions, SalienceOutcome(silence=False, plan=_PLAN, revise=0))
        assert out is actions
        provider.create_message.assert_not_awaited()

    async def test_noop_when_no_plan(self):
        provider = _provider()
        actions = _channel_actions()
        out = await _glue(provider, actions, SalienceOutcome(silence=False, plan=None, revise=2))
        assert out is actions
        provider.create_message.assert_not_awaited()

    async def test_noop_when_no_channel_message(self):
        """A turn with no SEND_CHANNEL_MESSAGE (e.g. a DO_NOTHING) is untouched."""
        provider = _provider()
        actions = [AgentAction(action_type=ActionType.DO_NOTHING, payload={})]
        out = await _glue(provider, actions, SalienceOutcome(silence=False, plan=_PLAN, revise=1))
        assert out is actions
        provider.create_message.assert_not_awaited()

    async def test_revises_the_message_content(self):
        revised = "Redis fits: sub-ms reads, native TTL eviction."
        provider = _provider("weak: yes", revised, "weak: no")
        actions = _channel_actions()
        out = await _glue(provider, actions, SalienceOutcome(silence=False, plan=_PLAN, revise=2))
        assert out[0].payload["content"] == revised
        # Other payload keys preserved; original action object not mutated.
        assert out[0].payload["channel_id"] == "group:planning"
        assert actions[0].payload["content"] == _DRAFT

    async def test_strong_draft_returns_actions_unchanged(self):
        provider = _provider("weak: no")
        actions = _channel_actions()
        out = await _glue(provider, actions, SalienceOutcome(silence=False, plan=_PLAN, revise=1))
        assert out is actions  # no rewrite → identity-preserved
        assert provider.create_message.await_count == 1

    async def test_only_the_message_action_is_replaced(self):
        """A revise replaces only the channel message; sibling actions (memory
        writes, votes) keep their identity and order."""
        revised = "sharper"
        provider = _provider("weak: yes", revised, "weak: no")
        other = AgentAction(action_type=ActionType.DO_NOTHING, payload={"k": "v"})
        actions = [*_channel_actions(), other]
        out = await _glue(provider, actions, SalienceOutcome(silence=False, plan=_PLAN, revise=1))
        assert out[0].payload["content"] == revised
        assert out[1] is other
