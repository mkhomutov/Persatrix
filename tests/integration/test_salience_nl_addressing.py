"""RFC 0030 Tier B (v0.3.8) — PR 3 NL-addressing, end-to-end through the seam.

``tests/unit/python/test_salience_addressing.py`` pins the pure extractor and
``test_salience_bid.py`` pins the bar shift in :func:`evaluate_salience`. This
file drives the *real* bid (not patched) through the action-loop seam to prove
the emergent story: a free-text invitation ("let's hear from Iron Fox") draws
the *named* persona but not an un-named one — and the bias is **never** a hard
filter (an un-named persona with a decisive contribution still speaks; TB4 /
amendment OQ #2). Structured ``@``-mentions remain Tier A's deterministic drop.

Self-contained (its own small harness) rather than importing the sibling
``test_salience_action_loop`` helpers — ``tests/integration`` is not a package,
so bare-name cross-module imports are fragile.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.model_aliases import use_alias_map
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import ActionType, AgentEvent, EventType
from agents.tools.registry import clear_registry

pytestmark = pytest.mark.asyncio

# A `fast` alias routed to the in-test provider so the *real* bid's
# `resolve("fast")` does not hit the shipped `unconfigured` provider.
_FAST_ALIAS_MAP: dict[str, dict[str, Any]] = {
    "fast": {
        "provider": "mock",
        "model": "mock-fast",
        "input_per_1m_tokens": 0.0,
        "output_per_1m_tokens": 0.0,
    },
}


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


def _persona_config(agent_id: str, display_name: str) -> dict:
    return {
        "id": agent_id,
        "name": display_name,  # `agent.name` reads the top-level config name
        "model": "test-model",
        "role": "Tier B NL-addressing test persona",
        "type": "persona",
        "max_llm_calls": 3,
        "max_tokens": 512,
        "tools": [],
        "persona": {
            "name": display_name,
            "background": "Used by the RFC 0030 Tier B NL-addressing test.",
            "behavior": {
                "directness": "balanced",
                "formality": "professional",
                "risk_tolerance": "moderate",
            },
        },
        "memory": {"db_path": ":memory:", "working": {"max_tokens": 50000}},
        "relationships": [],
    }


def _routing_client(*, bid_text: str) -> tuple[LLMClient, MagicMock]:
    """A client that routes by model: the ``fast`` bid call (``mock-fast``)
    returns ``bid_text``; the persona's quality turn (``test-model``) returns
    one channel reply. The recorded quality-turn count is the "did this
    persona actually reach the turn?" signal."""
    quality = LLMResponse(
        text=(
            '```json\n[{"action_type": "send_channel_message", '
            '"payload": {"content": "ack"}}]\n```'
        ),
        stop_reason=StopReason.END_TURN,
        usage=Usage(10, 5),
    )
    bid = LLMResponse(text=bid_text)
    quality_calls = MagicMock()

    async def _create_message(**kwargs: Any) -> LLMResponse:
        if kwargs.get("model") == "mock-fast":
            return bid
        quality_calls(**kwargs)
        return quality

    provider = AsyncMock()
    provider.create_message = AsyncMock(side_effect=_create_message)
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(side_effect=lambda msgs, resp, results: msgs)
    return LLMClient(provider), quality_calls


async def _make_agent(
    *, agent_id: str, display_name: str, bid_text: str,
) -> tuple[_LLMPersonaAgent, MagicMock]:
    client, quality_calls = _routing_client(bid_text=bid_text)
    agent = create_persona_agent(
        agent_id=agent_id,
        config=_persona_config(agent_id, display_name),
        llm_client=client,
    )
    await agent.initialize_memory()
    return agent, quality_calls


def _payload(content: str) -> dict:
    return {
        "content": content,
        "channel_type": "group",
        "respond_policy": "always",
        "mentions": [],
        "thread_parent_sender_id": "",
        # The PR-2b wire fields that flip the bid live + carry the threshold.
        "salience_gated": True,
        "threshold": 0.4,
    }


async def _deliver(agent: _LLMPersonaAgent, payload: dict) -> list:
    return await agent.on_event(AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload=payload,
        channel_id="group:planning",
        sender_id="alice",
        metadata={},
    ))


_INVITE = "let's hear from Iron Fox on this — what cache should we pick?"


class TestNLAddressingEndToEnd:
    async def test_nl_addressing_draws_the_named_persona(self):
        """"let's hear from Iron Fox" → Iron Fox reaches the quality turn; the
        un-named Ember Owl, on the same middling bid score, stays silent."""
        bid_text = "speak: yes\nscore: 0.5"  # clears 0.4 alone; the bias decides

        iron, iron_quality = await _make_agent(
            agent_id="iron-fox", display_name="Iron Fox", bid_text=bid_text,
        )
        ember, ember_quality = await _make_agent(
            agent_id="ember-owl", display_name="Ember Owl", bid_text=bid_text,
        )

        with use_alias_map(_FAST_ALIAS_MAP):
            await _deliver(iron, _payload(_INVITE))
            ember_actions = await _deliver(ember, _payload(_INVITE))

        # The quality-turn count is the "did this persona reach the turn?"
        # signal (the canned action carries no channel_id, so the action itself
        # downgrades to DO_NOTHING — that is not what we assert here).
        assert iron_quality.call_count >= 1, "the named persona should speak"
        assert ember_quality.call_count == 0, "the un-named persona should defer"
        assert all(
            a.action_type is ActionType.DO_NOTHING for a in ember_actions
        ), "a deferred persona emits no action"

    async def test_nl_addressing_is_not_a_hard_filter(self):
        """The un-named persona with a *decisive* score still speaks — proving
        the NL signal biases the bid rather than pre-dropping the turn
        (TB4 / amendment OQ #2)."""
        ember, ember_quality = await _make_agent(
            agent_id="ember-owl",
            display_name="Ember Owl",
            bid_text="speak: yes\nscore: 0.95",
        )

        with use_alias_map(_FAST_ALIAS_MAP):
            await _deliver(ember, _payload(_INVITE))

        assert ember_quality.call_count >= 1, (
            "a decisive contribution must clear the bid even when someone "
            "else was invited by name — NL addressing is not a hard filter"
        )
