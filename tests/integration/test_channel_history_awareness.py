"""Integration test for the channel-history tier (RFC 0011 PR 5 follow-up).

Verifies the agent-B-reply contract from the deferred PR 5 checklist: a
distinct token persisted in an earlier same-scope episode surfaces in B's
``MemoryBudget`` admitted set when B receives the next ``CHANNEL_MESSAGE``
in that channel.

Pre-seeding a closed-interaction episode (rather than driving the
two-phase close path with a stub LLM) keeps the assertion focused on the
RFC 0011 §E recall surface — the close-path summarization machinery is
RFC 0020 §C territory and is already covered by
``tests/integration/test_channel_interaction_scoping.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import AgentEvent, EventType
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
        "role": "Channel-history awareness test persona",
        "type": "persona",
        "max_llm_calls": 5,
        "max_tokens": 1024,
        "tools": [],
        "persona": {
            "name": agent_id,
            "background": "Used by RFC 0011 PR 5 follow-up channel-history tests.",
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


def _do_nothing_client() -> LLMClient:
    mock_provider = AsyncMock()
    mock_provider.create_message = AsyncMock(
        return_value=LLMResponse(
            text='```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
            stop_reason=StopReason.END_TURN,
            usage=Usage(10, 5),
        ),
    )
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: msgs,
    )
    return LLMClient(mock_provider)


async def _make_agent(agent_id: str) -> _LLMPersonaAgent:
    cfg = _persona_config(agent_id)
    agent = create_persona_agent(
        agent_id=agent_id,
        config=cfg,
        llm_client=_do_nothing_client(),
    )
    await agent.initialize_memory()
    return agent


def _channel_payload(content: str, channel_type: str = "group") -> dict:
    """``respond_policy=always`` keeps the gate orthogonal — ingest fires."""
    return {
        "content": content,
        "channel_type": channel_type,
        "respond_policy": "always",
        "mentions": [],
        "thread_parent_sender_id": "",
    }


# ─── Tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_channel_history_tier_surfaces_earlier_token_for_same_scope() -> None:
    """Token in B's earlier closed episode surfaces on the next CHANNEL_MESSAGE.

    Pins the joint-delivery contract from
    [RFC 0011 PR 5 checklist](docs/rfcs/0011-pr-plan.md): "agent B's reply
    demonstrates channel-history awareness — verifiable by injecting a
    distinct token in A's earlier message and asserting it surfaces in
    B's ``MemoryBudget`` admitted set on the next turn."
    """
    agent_b = await _make_agent("agent-b")
    channel_id = "group:planning"
    distinctive_token = "REDFROG-7841"

    # Seed B's episodic memory with a closed-interaction episode in the
    # same channel scope, with the distinctive token in the summary.
    # The two-phase close path (RFC 0020 PR 4) is exercised by other
    # integration tests; here we focus on the recall surface.
    await agent_b._episodic_memory.store_episode(
        summary=(
            f"Planning channel earlier discussion: agent-a flagged "
            f"the {distinctive_token} milestone for Q3 rollout."
        ),
        context={},
        importance=0.9,
        scope="group:planning",
        interaction_id="seed-1",
        started_at=100.0,
        closed_at=110.0,
        turn_count=3,
    )

    # Fresh CHANNEL_MESSAGE arrives in the same channel.  The query
    # text does NOT contain the distinctive token — surfacing it
    # depends on the per-channel scope filter, not on lexical overlap
    # with the token itself.  Word overlap on "planning"/"milestone"/
    # "rollout" keeps the FTS5 BM25 score above the recall ``min_score``
    # floor so the seeded episode is in the recall set when the scope
    # filter runs.
    await agent_b.on_event(AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload=_channel_payload(
            "Status check on the planning milestone rollout for Q3.",
        ),
        channel_id=channel_id,
        sender_id="agent-a",
        # RFC 0037 §B/§D: mirror the dispatch path's classification stamp
        # so the internal-default seeded episode passes the §D gate (an
        # unclassified event floors the turn to ``public`` — rule (b)).
        metadata={"channel_classification": "internal"},
    ))

    sections = {s.name: s for s in agent_b._working_memory._sections}
    assert "channel_history" in sections, (
        f"channel_history section absent; saw {list(sections)}"
    )
    assert distinctive_token in sections["channel_history"].content, (
        f"earlier-turn token {distinctive_token!r} did not surface in "
        f"the channel_history section: {sections['channel_history'].content!r}"
    )


@pytest.mark.asyncio
async def test_channel_history_tier_skipped_when_no_same_scope_episodes() -> None:
    """A CHANNEL_MESSAGE for a channel with no matching episodes adds no section.

    Negative pin: the tier is gated *and* scoped — even though the tier
    fires for every CHANNEL_MESSAGE, no section is admitted when the
    scope filter excludes every persisted episode.
    """
    agent_b = await _make_agent("agent-b")
    # Seed an episode in a *different* channel scope.
    await agent_b._episodic_memory.store_episode(
        summary="Other channel discussion about lunch plans.",
        context={},
        importance=0.9,
        scope="group:other",
        interaction_id="seed-other",
        started_at=100.0,
        closed_at=110.0,
        turn_count=2,
    )

    await agent_b.on_event(AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload=_channel_payload("hello team"),
        channel_id="group:planning",
        sender_id="agent-a",
    ))

    section_names = {s.name for s in agent_b._working_memory._sections}
    assert "channel_history" not in section_names, (
        "channel_history must not be admitted when no episodes match the "
        "event's per-channel scope"
    )


@pytest.mark.asyncio
async def test_channel_history_tier_skipped_for_dm_with_no_dm_episodes() -> None:
    """DM event with no DM-scope episodes adds no channel_history section.

    Companion to the same-channel positive test — pins the negative for
    the DM scope key (``dm:a:b``).
    """
    agent_b = await _make_agent("agent-b")
    # Group-scope episode is in the same DB but does not match a DM scope.
    await agent_b._episodic_memory.store_episode(
        summary="Group channel discussion.",
        context={},
        importance=0.9,
        scope="group:planning",
        interaction_id="seed-group",
        started_at=100.0,
        closed_at=110.0,
        turn_count=2,
    )

    await agent_b.on_event(AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload=_channel_payload("hi", channel_type="dm"),
        channel_id="dm:agent-a:agent-b",
        sender_id="agent-a",
    ))

    section_names = {s.name for s in agent_b._working_memory._sections}
    assert "channel_history" not in section_names
