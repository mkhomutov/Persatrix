"""Integration test for the RFC 0030 relevance amendment Tier A (v0.3.7).

Reproduces the v0.3.6 manual-test Trigger at the persona-runtime level: on
a multi-``participant`` channel a message ``@``-mentioning one persona must
draw **exactly one** reply (the addressed persona), not a pile-on from every
``always`` member. The directedness decision is the response gate's
(``agents/response_gate.py`` — ``directed_elsewhere``); this test drives it
through the real action loop (``_LLMPersonaAgent.on_event`` →
``_on_event_inner`` → gate) so the suppression is observed end-to-end, not
just as a unit return value.

The observable is **whether the persona's LLM provider is invoked**: an
admitted persona reaches the turn and calls ``create_message``; a gated one
returns ``DO_NOTHING`` before any provider or memory-recall round-trip (the
RFC 0023/0024 idle-cost-zero invariant — D5). Counting provider calls is
therefore a faithful, deterministic proxy for "who replied" with no real
LLM and no nondeterminism.

Tier B (the salience bid that decides who, among the admitted, actually has
something to add) is a v0.3.8 concern: in v0.3.7 every admitted persona
reaches the turn, so an open-floor message admits all participants here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import AgentEvent, EventType
from agents.response_gate import MENTION_EVERYONE
from agents.tools.registry import clear_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _persona_config(agent_id: str) -> dict:
    return {
        "id": agent_id,
        "model": "test-model",
        "role": "Channel relevance (Tier A) test persona",
        "type": "persona",
        "max_llm_calls": 5,
        "max_tokens": 1024,
        "tools": [],
        "persona": {
            "name": agent_id,
            "background": "Used by the RFC 0030 Tier A directedness test.",
            "behavior": {
                "directness": "balanced",
                "formality": "professional",
                "risk_tolerance": "moderate",
            },
        },
        "autonomy": {
            "level": "semi-autonomous",
            "tick_interval_seconds": 1,
            "max_actions_per_tick": 3,
            "idle_after_ticks": 5,
        },
        "memory": {
            "db_path": ":memory:",
            "working": {"max_tokens": 50000},
            "interaction_idle_timeout_sec": 5.0,
        },
        "relationships": [],
    }


def _tracking_client() -> tuple[LLMClient, MagicMock]:
    """An LLM client that records every ``create_message`` call.

    The persona replies with a single channel message when it reaches the
    turn; the recorded call count is the test's "did this persona reply?"
    signal. A gated persona never reaches ``create_message``.
    """
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


async def _make_agent(agent_id: str) -> tuple[_LLMPersonaAgent, MagicMock]:
    client, create_message = _tracking_client()
    agent = create_persona_agent(
        agent_id=agent_id,
        config=_persona_config(agent_id),
        llm_client=client,
    )
    await agent.initialize_memory()
    return agent, create_message


def _channel_payload(*, mentions: list[str]) -> dict:
    return {
        "content": "team, how about you @ember-owl?",
        "channel_type": "group",
        "respond_policy": "always",  # both personas are participants
        "mentions": list(mentions),
        "thread_parent_sender_id": "",
    }


async def _deliver(agent: _LLMPersonaAgent, *, mentions: list[str]) -> None:
    await agent.on_event(AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload=_channel_payload(mentions=mentions),
        channel_id="group:planning",
        sender_id="alice",  # a third member sends; neither persona is the sender
    ))


# ─── Tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_directed_mention_draws_exactly_one_reply() -> None:
    """The Trigger repro: ``@ember-owl`` admits only Ember Owl.

    Both personas are ``participant`` (``always``). A message naming only
    ember-owl must reach ember-owl's turn (its provider is called) and be
    suppressed for iron-fox (``directed_elsewhere`` — provider never called,
    zero tokens).
    """
    ember, ember_calls = await _make_agent("ember-owl")
    iron, iron_calls = await _make_agent("iron-fox")

    await _deliver(ember, mentions=["ember-owl"])
    await _deliver(iron, mentions=["ember-owl"])

    assert ember_calls.await_count >= 1, (
        "the addressed persona (ember-owl) must reach the turn"
    )
    assert iron_calls.await_count == 0, (
        "the un-addressed participant (iron-fox) must be gated "
        "directed_elsewhere — no LLM call, idle-cost zero (D5)"
    )


@pytest.mark.asyncio
async def test_open_floor_admits_all_participants() -> None:
    """No mentions → open floor → every participant reaches the turn.

    v0.3.7 has no Tier B silence, so an un-addressed message admits all
    participants (no regression vs. today's open-floor admit-all).
    """
    ember, ember_calls = await _make_agent("ember-owl")
    iron, iron_calls = await _make_agent("iron-fox")

    await _deliver(ember, mentions=[])
    await _deliver(iron, mentions=[])

    assert ember_calls.await_count >= 1
    assert iron_calls.await_count >= 1


@pytest.mark.asyncio
async def test_broadcast_admits_all_participants() -> None:
    """An explicit ``@everyone`` broadcast disables the directed filter (D3)."""
    ember, ember_calls = await _make_agent("ember-owl")
    iron, iron_calls = await _make_agent("iron-fox")

    await _deliver(ember, mentions=[MENTION_EVERYONE, "ember-owl"])
    await _deliver(iron, mentions=[MENTION_EVERYONE, "ember-owl"])

    assert ember_calls.await_count >= 1
    assert iron_calls.await_count >= 1, (
        "a broadcast admits the un-named participant too (D3)"
    )


@pytest.mark.asyncio
async def test_directed_elsewhere_member_still_remembers() -> None:
    """A directed-elsewhere participant is silent but **not** amnesiac.

    The gate decides *whether to respond*, not *whether to remember*: a
    suppression whose policy is not ``defense_in_depth`` still drives
    ``_store_event_episode`` (``agents/persona_runtime/action_loop.py``).
    So iron-fox, gated ``directed_elsewhere`` on ``@ember-owl``, makes zero
    LLM calls yet still ingests the turn into memory.

    This is the load-bearing reason the Go concurrent-fanout path keeps
    *dispatching* to un-addressed participants instead of pre-filtering them
    the way the floor path does (``internal/channels/floor_control.go``):
    the dispatch is what feeds their memory. A future change that folded
    ``directed_elsewhere`` into the ingest-skip exception — or dropped these
    members from concurrent fanout — would silently make un-addressed
    participants forget everything said while they were not the target,
    breaking cross-mention context. Pin it.
    """
    iron, iron_calls = await _make_agent("iron-fox")

    with patch.object(iron, "_store_event_episode", new=AsyncMock()) as store_mock:
        await _deliver(iron, mentions=["ember-owl"])

    iron_calls.assert_not_awaited()  # gated: no reply, idle-cost zero (D5)
    store_mock.assert_awaited_once()  # but the turn still lands in memory
    assert store_mock.await_args is not None  # narrow _Call | None for mypy
    ingested_event, ingested_actions = store_mock.await_args.args
    assert ingested_event.payload["mentions"] == ["ember-owl"]
    assert ingested_actions == []  # suppressed → no actions produced the turn
