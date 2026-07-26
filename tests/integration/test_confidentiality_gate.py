"""RFC 0037 Phase-1 integration — the §D gate end to end (PR 4).

The RFC's Test Strategy integration case: a persona learns something in a
``restricted`` channel, then acts in a ``public`` (and an ``internal``)
channel — the verbatim memory is absent from the lower turns' assembled
working memory, present again when the persona acts back at (or above)
the protected level, and the autonomous tick sees only the ``public``
floor.  The §B guard's end-to-end seam (a cross-channel
``SEND_CHANNEL_MESSAGE`` surviving parse → replaced before dispatch) is
also pinned here through the real ``_on_event_inner``.

Entries are pre-seeded with the stamps the RFC 0020 close path writes
(``protection_level="restricted"`` from a restricted interaction — the
wire→capture→stamp seam itself is covered by
``test_interaction_classification_capture.py``), keeping this file
focused on the read-side boundary.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import ActionType, AgentEvent, EventType
from agents.tools.registry import clear_registry

_SECRET_TOKEN = "REDWOLF-2291"


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _persona_config(agent_id: str) -> dict:
    return {
        "id": agent_id,
        "model": "test-model",
        "role": "RFC 0037 confidentiality-gate test persona",
        "type": "persona",
        "max_llm_calls": 5,
        "max_tokens": 1024,
        "tools": [],
        "persona": {
            "name": agent_id,
            "background": "Used by the RFC 0037 §D gate integration tests.",
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


def _client(reply_json: str | None = None) -> LLMClient:
    text = reply_json or (
        '```json\n[{"action_type": "do_nothing", "payload": {}}]\n```'
    )
    mock_provider = AsyncMock()
    mock_provider.create_message = AsyncMock(
        return_value=LLMResponse(
            text=text, stop_reason=StopReason.END_TURN, usage=Usage(10, 5),
        ),
    )
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: msgs,
    )
    return LLMClient(mock_provider)


async def _make_agent(reply_json: str | None = None) -> _LLMPersonaAgent:
    agent = create_persona_agent(
        agent_id="gate-agent",
        config=_persona_config("gate-agent"),
        llm_client=_client(reply_json),
    )
    await agent.initialize_memory()
    return agent


async def _seed_restricted_memory(agent: _LLMPersonaAgent) -> None:
    """One protected entry per gated tier, stamped as the close path
    stamps a ``restricted`` interaction's output."""
    await agent._episodic_memory.store_episode(
        summary=f"Leadership decided to sunset {_SECRET_TOKEN} next quarter",
        context={},
        importance=0.9,
        protection_level="restricted",
        source_channel_id="group:leadership",
    )
    await agent._episodic_memory.store_note(
        topic="sunset-plan",
        content=f"Sunset briefing notes for {_SECRET_TOKEN}",
        protection_level="restricted",
    )


def _event(classification: str | None, content: str) -> AgentEvent:
    metadata: dict = {}
    if classification is not None:
        metadata["channel_classification"] = classification
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        channel_id="group:town-square",
        sender_id="alice",
        payload={
            "content": content,
            # ``respond_policy=always`` keeps the response gate
            # orthogonal so `_on_event_inner` reaches the action parse.
            "respond_policy": "always",
            "mentions": [],
            "thread_parent_sender_id": "",
        },
        metadata=metadata,
    )


def _visible_memory(agent: _LLMPersonaAgent) -> str:
    return "\n".join(s.content for s in agent._working_memory._sections)


class TestLearnRestrictedActLower:
    async def test_public_turn_never_sees_the_verbatim_memory(self) -> None:
        agent = await _make_agent()
        try:
            await _seed_restricted_memory(agent)
            await agent._inject_memory_context(
                _event("public", "any news on the sunset plan?"),
                query="sunset " + _SECRET_TOKEN,
            )
            assert _SECRET_TOKEN not in _visible_memory(agent)
        finally:
            await agent.close_memory()

    async def test_internal_turn_never_sees_the_verbatim_memory(self) -> None:
        """The realistic leak (v0.3.12 review item 8): ``restricted``
        memory must not flow into every ordinary ``internal`` room."""
        agent = await _make_agent()
        try:
            await _seed_restricted_memory(agent)
            await agent._inject_memory_context(
                _event("internal", "any news on the sunset plan?"),
                query="sunset " + _SECRET_TOKEN,
            )
            assert _SECRET_TOKEN not in _visible_memory(agent)
        finally:
            await agent.close_memory()

    async def test_restricted_turn_sees_it_verbatim(self) -> None:
        agent = await _make_agent()
        try:
            await _seed_restricted_memory(agent)
            await agent._inject_memory_context(
                _event("restricted", "any news on the sunset plan?"),
                query="sunset " + _SECRET_TOKEN,
            )
            assert _SECRET_TOKEN in _visible_memory(agent)
        finally:
            await agent.close_memory()

    async def test_unclassified_turn_floors_to_public(self) -> None:
        """Version skew (rule (b)): an event with no §B stamp sees the
        least-confidential view, not the ``internal`` default."""
        agent = await _make_agent()
        try:
            await _seed_restricted_memory(agent)
            await agent._episodic_memory.store_note(
                topic="open-note",
                content="sunset press release drafted",
                protection_level="public",
            )
            # Single-term query so every seeded entry (protected and
            # public alike) is in the recall set — the assertion then
            # isolates the gate, not FTS5's implicit-AND term matching.
            await agent._inject_memory_context(
                _event(None, "any news on the sunset plan?"),
                query="sunset",
            )
            visible = _visible_memory(agent)
            assert _SECRET_TOKEN not in visible
            assert "press release" in visible
        finally:
            await agent.close_memory()


class TestTickFloor:
    async def test_tick_sees_only_public_memory(self) -> None:
        agent = await _make_agent()
        try:
            await _seed_restricted_memory(agent)
            result = await agent._inject_memory_context(
                AgentEvent(event_type=EventType.TICK),
                query="sunset " + _SECRET_TOKEN,
            )
            assert _SECRET_TOKEN not in _visible_memory(agent)
            # With nothing at the floor, the tick admits zero tokens —
            # the RFC 0017 §F empty-context short-circuit composes with
            # the §D floor to skip the LLM call entirely.
            assert result.memory_admitted_tokens == 0
            assert result.manifest == ()
        finally:
            await agent.close_memory()


class TestManifestEndToEnd:
    async def test_manifest_names_what_reached_the_prompt(self) -> None:
        agent = await _make_agent()
        try:
            await _seed_restricted_memory(agent)
            result = await agent._inject_memory_context(
                _event("restricted", "any news on the sunset plan?"),
                query="sunset " + _SECRET_TOKEN,
            )
            tiers = {entry.tier for entry in result.manifest}
            assert "episodic" in tiers and "notes" in tiers
            assert all(
                entry.protection_level == "restricted"
                for entry in result.manifest
            )
        finally:
            await agent.close_memory()


class TestSingleChannelTurnEndToEnd:
    async def test_cross_channel_publish_replaced_before_dispatch(
        self,
    ) -> None:
        """§B guard through the real ``_on_event_inner``: a parsed
        ``SEND_CHANNEL_MESSAGE`` targeting a foreign channel comes back
        as ``DO_NOTHING``; the same-channel form survives."""
        reply = (
            '```json\n[{"action_type": "send_channel_message", '
            '"payload": {"channel_id": "group:other", "content": "leak"}}]'
            "\n```"
        )
        agent = await _make_agent(reply)
        try:
            async with agent._lock:
                actions = await agent._on_event_inner(
                    _event("internal", "hello there"),
                )
            assert [a.action_type for a in actions] == [ActionType.DO_NOTHING]
        finally:
            await agent.close_memory()

    async def test_same_channel_publish_survives(self) -> None:
        reply = (
            '```json\n[{"action_type": "send_channel_message", '
            '"payload": {"channel_id": "group:town-square", '
            '"content": "hi all"}}]\n```'
        )
        agent = await _make_agent(reply)
        try:
            async with agent._lock:
                actions = await agent._on_event_inner(
                    _event("internal", "hello there"),
                )
            sends = [
                a for a in actions
                if a.action_type is ActionType.SEND_CHANNEL_MESSAGE
            ]
            assert len(sends) == 1
            assert sends[0].payload["channel_id"] == "group:town-square"
        finally:
            await agent.close_memory()
