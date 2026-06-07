"""RFC 0030 Tier B (v0.3.8) — the action-loop seam for the salience bid.

PR 2 of the Tier B PR plan. ``test_tier_b_salience.py`` pins the bid's
bias-to-silence decision in isolation; this file pins the *wiring* —
**when** the leased bid runs and **what the action loop does** with its
verdict — by driving the real ``_LLMPersonaAgent.on_event`` →
``_on_event_inner`` path.

The bid (``agents.tier_b_salience.evaluate_salience``) is patched here so the
wiring assertions are deterministic and independent of the bid's prompt /
model resolution. The observables:

* The bid runs **only** on the open-floor admit (TB1) of a **Tier-B-governed**
  channel — never for a directed ``@``-mention, an ``observer``, the
  self-sender, or (the PR-2a default) an un-governed channel.
* A "stay silent" verdict suppresses the turn (``DO_NOTHING``) **before** the
  quality LLM call — the no-pile-on / idle-cost-zero win — while still
  ingesting the message.
* A "speak" verdict proceeds to the quality turn (the provider is called).
* TB6: above ``tier_b_max_channel_members`` the bid is skipped entirely and
  the un-addressed participant stays silent (``addressed``-only fallback).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import ActionType, AgentEvent, EventType
from agents.tier_b_salience import SalienceDecision
from agents.tools.registry import clear_registry

pytestmark = pytest.mark.asyncio

_BID_PATH = "agents.persona_runtime.tier_b_gate.evaluate_salience"


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _persona_config(agent_id: str) -> dict:
    return {
        "id": agent_id,
        "model": "test-model",
        "role": "Tier B action-loop test persona",
        "type": "persona",
        "max_llm_calls": 3,
        "max_tokens": 512,
        "tools": [],
        "persona": {
            "name": agent_id,
            "background": "Used by the RFC 0030 Tier B wiring test.",
            "behavior": {
                "directness": "balanced",
                "formality": "professional",
                "risk_tolerance": "moderate",
            },
        },
        "memory": {"db_path": ":memory:", "working": {"max_tokens": 50000}},
        "relationships": [],
    }


def _tracking_client() -> tuple[LLMClient, MagicMock]:
    """A client whose quality turn emits one channel reply; the recorded
    ``create_message`` count is the "did the persona reach the turn?" signal."""
    provider = AsyncMock()
    provider.create_message = AsyncMock(
        return_value=LLMResponse(
            text=(
                '```json\n[{"action_type": "send_channel_message", '
                '"payload": {"content": "ack"}}]\n```'
            ),
            stop_reason=StopReason.END_TURN,
            usage=Usage(10, 5),
        ),
    )
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(side_effect=lambda msgs, resp, results: msgs)
    return LLMClient(provider), provider.create_message


async def _make_agent(agent_id: str = "ember-owl") -> tuple[_LLMPersonaAgent, MagicMock]:
    client, create_message = _tracking_client()
    agent = create_persona_agent(
        agent_id=agent_id,
        config=_persona_config(agent_id),
        llm_client=client,
    )
    await agent.initialize_memory()
    return agent, create_message


def _payload(
    *,
    respond_policy: str = "always",
    mentions: list[str] | None = None,
    tier_b_active: bool | None = None,
    threshold: float | None = None,
    channel_size: int | None = None,
    max_members: int | None = None,
) -> dict:
    payload: dict = {
        "content": "What database should we pick for the cache?",
        "channel_type": "group",
        "respond_policy": respond_policy,
        "mentions": list(mentions or []),
        "thread_parent_sender_id": "",
    }
    # The PR-2b wire fields. Omitted entirely by default so the seam is
    # dormant (the PR-2a additive default).
    if tier_b_active is not None:
        payload["tier_b_active"] = tier_b_active
    if threshold is not None:
        payload["threshold"] = threshold
    if channel_size is not None:
        payload["channel_size"] = channel_size
    if max_members is not None:
        payload["tier_b_max_channel_members"] = max_members
    return payload


async def _deliver(agent: _LLMPersonaAgent, payload: dict) -> list:
    return await agent.on_event(AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload=payload,
        channel_id="group:planning",
        sender_id="alice",
    ))


def _silent() -> SalienceDecision:
    return SalienceDecision(speak=False, score=0.1, reason="below_threshold")


def _speak() -> SalienceDecision:
    return SalienceDecision(speak=True, score=0.9, reason="salient")


class TestBidRunsOnlyOnOpenFloorGoverned:
    async def test_governed_open_floor_runs_the_bid(self):
        agent, _ = await _make_agent()
        with patch(_BID_PATH, new=AsyncMock(return_value=_silent())) as bid:
            await _deliver(agent, _payload(tier_b_active=True))
        bid.assert_awaited_once()

    async def test_ungoverned_open_floor_is_dormant(self):
        """PR-2a default: with no ``tier_b_active`` flag the bid never runs —
        v0.3.7 behaviour (every open-floor participant replies)."""
        agent, quality = await _make_agent()
        with patch(_BID_PATH, new=AsyncMock(return_value=_silent())) as bid:
            await _deliver(agent, _payload())  # no tier_b_active
        bid.assert_not_called()
        assert quality.await_count >= 1, "ungoverned persona still replies"

    async def test_directed_mention_skips_the_bid(self):
        """A directed ``@``-mention is the persona's lane (TB1) — no bid even
        when the channel is governed."""
        agent, quality = await _make_agent("iron-fox")
        with patch(_BID_PATH, new=AsyncMock(return_value=_silent())) as bid:
            await _deliver(agent, _payload(
                respond_policy="when_mentioned",
                mentions=["iron-fox"],
                tier_b_active=True,
            ))
        bid.assert_not_called()
        assert quality.await_count >= 1

    async def test_directed_mention_of_an_always_member_skips_the_bid(self):
        """TB1 regression: a *participant* (``always``) member named
        explicitly is its lane just as a ``when_mentioned`` member is — the
        salience bid must not run on (and so can never silence) a
        directly-asked persona, even on a governed channel. Before the gate
        fix the ``always`` branch admitted this with ``reason=policy_always``,
        so ``is_open_floor_admit`` matched and the bid ran."""
        agent, quality = await _make_agent("iron-fox")
        with patch(_BID_PATH, new=AsyncMock(return_value=_silent())) as bid:
            await _deliver(agent, _payload(
                respond_policy="always",
                mentions=["iron-fox"],
                tier_b_active=True,
            ))
        bid.assert_not_called()
        assert quality.await_count >= 1

    async def test_observer_never_reaches_the_bid(self):
        """An ``observer`` (``never``) is gated before Tier B — cost zero."""
        agent, quality = await _make_agent()
        with patch(_BID_PATH, new=AsyncMock(return_value=_speak())) as bid:
            actions = await _deliver(agent, _payload(
                respond_policy="never", tier_b_active=True,
            ))
        bid.assert_not_called()
        assert quality.await_count == 0
        assert all(a.action_type is ActionType.DO_NOTHING for a in actions)

    async def test_self_sender_never_reaches_the_bid(self):
        agent, quality = await _make_agent("alice")  # sender == agent
        with patch(_BID_PATH, new=AsyncMock(return_value=_speak())) as bid:
            await _deliver(agent, _payload(tier_b_active=True))
        bid.assert_not_called()
        assert quality.await_count == 0


class TestBidVerdictRouting:
    async def test_silence_suppresses_before_the_quality_turn(self):
        """No-pile-on: a silent bid → DO_NOTHING, and the expensive quality
        LLM call never happens (idle-cost-zero on the quality turn)."""
        agent, quality = await _make_agent()
        with patch(_BID_PATH, new=AsyncMock(return_value=_silent())):
            actions = await _deliver(agent, _payload(tier_b_active=True))
        assert all(a.action_type is ActionType.DO_NOTHING for a in actions)
        assert quality.await_count == 0, "silent bid must not reach the quality turn"

    async def test_speak_proceeds_to_the_quality_turn(self):
        agent, quality = await _make_agent()
        with patch(_BID_PATH, new=AsyncMock(return_value=_speak())):
            await _deliver(agent, _payload(tier_b_active=True))
        assert quality.await_count >= 1, "a speak verdict must reach the turn"


class TestChannelSizeCap:
    async def test_oversized_channel_skips_bid_and_stays_silent(self):
        """TB6: above the cap the bid is skipped *and* the un-addressed
        participant stays silent (``addressed``-only fallback)."""
        agent, quality = await _make_agent()
        with patch(_BID_PATH, new=AsyncMock(return_value=_speak())) as bid:
            actions = await _deliver(agent, _payload(
                tier_b_active=True, channel_size=50, max_members=20,
            ))
        bid.assert_not_called()
        assert all(a.action_type is ActionType.DO_NOTHING for a in actions)
        assert quality.await_count == 0

    async def test_under_cap_still_runs_the_bid(self):
        agent, _ = await _make_agent()
        with patch(_BID_PATH, new=AsyncMock(return_value=_silent())) as bid:
            await _deliver(agent, _payload(
                tier_b_active=True, channel_size=4, max_members=20,
            ))
        bid.assert_awaited_once()


class TestThresholdParsing:
    """The seam parses the per-member ``threshold`` off the payload before
    handing it to the bid. A valid value passes through; an out-of-range
    value degrades to ``None`` (unset → the decisive bar), so a future wire
    bug biases to silence rather than permanently muting (or over-admitting)
    a persona."""

    async def test_in_range_threshold_passes_through(self):
        agent, _ = await _make_agent()
        with patch(_BID_PATH, new=AsyncMock(return_value=_silent())) as bid:
            await _deliver(agent, _payload(tier_b_active=True, threshold=0.4))
        bid.assert_awaited_once()
        assert bid.await_args.kwargs["threshold"] == pytest.approx(0.4)

    async def test_out_of_range_threshold_degrades_to_unset(self):
        agent, _ = await _make_agent()
        with patch(_BID_PATH, new=AsyncMock(return_value=_silent())) as bid:
            await _deliver(agent, _payload(tier_b_active=True, threshold=5.0))
        bid.assert_awaited_once()
        assert bid.await_args.kwargs["threshold"] is None
