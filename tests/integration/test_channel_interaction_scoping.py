"""
RFC 0020 PR 5 — channel-aware interaction scoping (Phase 3, joint with
RFC 0011 PR 5).

Pins the per-channel scoping rules from RFC 0020 §G:

* **DM** (``channel_type="dm"``): scope = ``scope_for_dm(local, peer)`` —
  symmetric in the two participant ids so A→B and B→A accumulate into
  the same interaction.
* **Thread** (``event.thread_id`` set): scope = ``scope_for_thread(thread_id)``.
  Takes precedence over ``channel_type`` so a thread reply in a group
  channel still rolls under the thread, not the parent channel.
* **Group** (``channel_type="group"``): scope = ``scope_for_group(channel_id)``
  — rolling per-channel-per-agent (the tracker is per-agent, so the
  channel-scoped key is enough to keep one open scope per channel).

Also pins the multi-agent group-channel acceptance contract: a 15-message
exchange across six agents in ``#planning`` produces **one episode per
agent** on close (not 15 per-message episodes, not one shared episode).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.memory.interactions import (
    scope_for_dm,
    scope_for_group,
    scope_for_thread,
)
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import AgentEvent, EventType
from agents.tools.registry import clear_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


_TEST_IDLE_TIMEOUT_SEC: float = 5.0


def _persona_config(agent_id: str) -> dict:
    return {
        "id": agent_id,
        "model": "test-model",
        "role": "Channel-scoping test persona",
        "type": "persona",
        "max_llm_calls": 5,
        "max_tokens": 1024,
        "tools": [],
        "persona": {
            "name": agent_id,
            "background": "Used by RFC 0020 PR 5 channel-scoping tests.",
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
            "interaction_idle_timeout_sec": _TEST_IDLE_TIMEOUT_SEC,
        },
        "relationships": [],
    }


def _do_nothing_client() -> LLMClient:
    """Mock LLM client whose every reply parses to a single DO_NOTHING.

    Channel-scoping behaviour does not depend on action shape — only on
    interaction-tracker state and the persisted episode column values.
    """
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


def _channel_payload(content: str, channel_type: str) -> dict:
    """Build a CHANNEL_MESSAGE payload that passes the response gate.

    The gate (``agents/response_gate.py``) requires a recognised
    ``respond_policy`` (``always`` / ``when_mentioned`` / DM override) to
    reach the memory-ingestion path. Tests that exercise scope routing
    use ``always`` to keep the gate orthogonal to the routing surface
    they cover.
    """
    return {
        "content": content,
        "channel_type": channel_type,
        "respond_policy": "always",
        "mentions": [],
        "thread_parent_sender_id": "",
    }


async def _all_episodes(agent: _LLMPersonaAgent) -> list[dict]:
    db = agent._episodic_memory._ensure_db()
    async with db.execute(
        """
        SELECT summary, interaction_id, started_at, closed_at,
               turn_count, scope
        FROM episodes
        WHERE agent_id = ?
        ORDER BY created_at
        """,
        (agent.agent_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        {
            "summary": r[0],
            "interaction_id": r[1],
            "started_at": r[2],
            "closed_at": r[3],
            "turn_count": r[4],
            "scope": r[5],
        }
        for r in rows
    ]


# ─── Per-channel scope discrimination ─────────────────────────


@pytest.mark.asyncio
class TestChannelScopeDiscrimination:
    """RFC 0020 §G — channel_type drives the scope-builder selection."""

    async def test_group_channel_uses_group_scope(self):
        """``channel_type="group"`` → ``scope_for_group(channel_id)``."""
        agent = await _make_agent("ember-owl")
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload=_channel_payload("hello team", "group"),
            channel_id="group:planning",
            sender_id="iron-fox",
        ))
        # The group channel scope must use the group prefix — not thread.
        # Pre-PR-5 the runtime mis-routed every channel_id through
        # scope_for_thread, which would yield ``thread:group:planning``.
        expected = scope_for_group("group:planning")
        assert agent._interaction_tracker.open_scopes() == [expected]
        assert expected.startswith("group:")

    async def test_dm_channel_uses_dm_scope(self):
        """``channel_type="dm"`` → ``scope_for_dm(local, peer)`` symmetric."""
        agent = await _make_agent("ember-owl")
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload=_channel_payload("ping", "dm"),
            channel_id="dm:ember-owl:iron-fox",
            sender_id="iron-fox",
        ))
        expected = scope_for_dm("ember-owl", "iron-fox")
        assert agent._interaction_tracker.open_scopes() == [expected]
        # The DM scope is invariant to the channel-id participant order
        # (the tracker keys on the canonicalised pair, not the wire id).
        assert expected == scope_for_dm("iron-fox", "ember-owl")

    async def test_thread_id_takes_precedence_over_channel_type(self):
        """A thread reply in a group channel rolls under the thread scope.

        RFC 0020 §G — ``thread_id`` set on the event is the strongest
        discriminator; the parent channel's ``channel_type`` is ignored
        once the message is in a thread.
        """
        agent = await _make_agent("ember-owl")
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload=_channel_payload("in thread", "group"),
            channel_id="group:planning",
            sender_id="iron-fox",
            thread_id="t-42",
        ))
        expected = scope_for_thread("t-42")
        assert agent._interaction_tracker.open_scopes() == [expected]

    async def test_thread_channel_type_uses_thread_id_when_present(self):
        """``channel_type="thread"`` with explicit ``thread_id`` → thread scope."""
        agent = await _make_agent("ember-owl")
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload=_channel_payload("in thread", "thread"),
            channel_id="thread:t-42",
            sender_id="iron-fox",
            thread_id="t-42",
        ))
        expected = scope_for_thread("t-42")
        assert agent._interaction_tracker.open_scopes() == [expected]

    async def test_legacy_dm_without_channel_type_uses_dm_scope(self):
        """Back-compat: PR 3 chat path (no ``channel_type``, only ``sender_id``)."""
        agent = await _make_agent("ember-owl")
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hi"},
            sender_id="iron-fox",
        ))
        expected = scope_for_dm("ember-owl", "iron-fox")
        assert agent._interaction_tracker.open_scopes() == [expected]

    async def test_unknown_channel_type_falls_back_to_id_prefix(self):
        """If ``channel_type`` is missing, the channel-id prefix discriminates."""
        agent = await _make_agent("ember-owl")
        # No ``channel_type`` — but pass the response gate via a recognised
        # ``respond_policy`` so the routing change is the unit under test.
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hello", "respond_policy": "always"},
            channel_id="group:planning",
            sender_id="iron-fox",
        ))
        # ``group:`` prefix → group scope, not thread scope.
        expected = scope_for_group("group:planning")
        assert agent._interaction_tracker.open_scopes() == [expected]

    async def test_dm_id_prefix_disambiguates_without_channel_type(self):
        agent = await _make_agent("ember-owl")
        # DM channel-id prefix triggers the gate's DM override, so the
        # event reaches memory ingestion regardless of ``respond_policy``.
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hi"},  # no channel_type
            channel_id="dm:ember-owl:iron-fox",
            sender_id="iron-fox",
        ))
        expected = scope_for_dm("ember-owl", "iron-fox")
        assert agent._interaction_tracker.open_scopes() == [expected]


# ─── Multi-agent group-channel acceptance ──────────────────────


@pytest.mark.asyncio
async def test_six_agents_15_messages_one_episode_per_agent():
    """RFC 0020 PR 5 acceptance: ``#planning`` 15-message exchange.

    Six agents each receive every CHANNEL_MESSAGE in the group channel
    (orchestrator-side fanout filters self-messages — receivers see only
    cross-agent traffic). On structural close (chat_end metadata),
    each agent persists exactly one closed-interaction episode for the
    group scope — not one episode per inbound message.
    """
    agent_ids = [
        f"agent-{i}" for i in range(6)
    ]
    agents = {aid: await _make_agent(aid) for aid in agent_ids}
    channel_id = "group:planning"
    expected_scope = scope_for_group(channel_id)

    # 15 messages, round-robin across six senders. Each agent receives
    # every message *except* their own (orchestrator-side self-filter).
    for turn_idx in range(15):
        sender = agent_ids[turn_idx % len(agent_ids)]
        for receiver_id, agent in agents.items():
            if receiver_id == sender:
                continue
            await agent.on_event(AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload=_channel_payload(
                    f"turn {turn_idx} from {sender}", "group",
                ),
                channel_id=channel_id,
                sender_id=sender,
            ))

    # Every agent has exactly one open scope, the group scope.
    for receiver_id, agent in agents.items():
        assert agent._interaction_tracker.open_scopes() == [expected_scope], (
            f"{receiver_id}: unexpected open scopes "
            f"{agent._interaction_tracker.open_scopes()}"
        )
        # Each agent saw 15 - own_messages turns. Round-robin over 6 means
        # each agent sent ⌈15/6⌉ = 3 messages and received the other 12.
        # Senders for turn 0..14 = agent-(0..14 mod 6) = 3 each for the
        # first 3 agents and 2 each for the last 3 agents.
        own_sent = sum(
            1 for i in range(15) if agent_ids[i % len(agent_ids)] == receiver_id
        )
        interaction = agent._interaction_tracker.get(expected_scope)
        assert interaction is not None
        assert interaction.turn_count == 15 - own_sent

    # Structural close — every agent receives a final ``chat_end`` flag.
    closer = agent_ids[0]
    for receiver_id, agent in agents.items():
        if receiver_id == closer:
            continue
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload=_channel_payload("wrapping up", "group"),
            channel_id=channel_id,
            sender_id=closer,
            metadata={"chat_end": True},
        ))
        await agent.drain_pending_summaries()

    # Each agent (except the closer who never received the final message)
    # has exactly one closed-interaction episode for the group scope.
    for receiver_id, agent in agents.items():
        if receiver_id == closer:
            # The closer's own send was filtered upstream; their tracker
            # still has the open scope. Skip the close assertion for them.
            continue
        episodes = await _all_episodes(agent)
        assert len(episodes) == 1, (
            f"{receiver_id}: expected one episode, got {len(episodes)}"
        )
        ep = episodes[0]
        assert ep["scope"] == expected_scope
        assert ep["interaction_id"] is not None
        assert ep["closed_at"] is not None
        # turn_count = received turns + 1 (chat_end terminator).
        own_sent = sum(
            1 for i in range(15) if agent_ids[i % len(agent_ids)] == receiver_id
        )
        assert ep["turn_count"] == 15 - own_sent + 1


# ─── DM-vs-group isolation ────────────────────────────────────


@pytest.mark.asyncio
async def test_dm_and_group_scopes_are_isolated():
    """Same agent participating in a DM and a group has two open scopes."""
    agent = await _make_agent("ember-owl")
    # Group message.
    await agent.on_event(AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload=_channel_payload("hi team", "group"),
        channel_id="group:planning",
        sender_id="iron-fox",
    ))
    # DM with the same peer.
    await agent.on_event(AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload=_channel_payload("side note", "dm"),
        channel_id="dm:ember-owl:iron-fox",
        sender_id="iron-fox",
    ))
    open_scopes = set(agent._interaction_tracker.open_scopes())
    assert open_scopes == {
        scope_for_group("group:planning"),
        scope_for_dm("ember-owl", "iron-fox"),
    }


@pytest.mark.asyncio
async def test_thread_reply_opens_separate_scope_from_parent_channel():
    """A thread reply does not extend the parent channel's interaction."""
    agent = await _make_agent("ember-owl")
    # Top-level group message.
    await agent.on_event(AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload=_channel_payload("top-level", "group"),
        channel_id="group:planning",
        sender_id="iron-fox",
    ))
    # Reply in a thread inside the same group.
    await agent.on_event(AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload=_channel_payload("in thread", "group"),
        channel_id="group:planning",
        sender_id="iron-fox",
        thread_id="t-77",
    ))
    open_scopes = set(agent._interaction_tracker.open_scopes())
    assert open_scopes == {
        scope_for_group("group:planning"),
        scope_for_thread("t-77"),
    }
