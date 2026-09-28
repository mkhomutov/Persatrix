"""ISSUE-0172 — an agent that stops writes its open conversations to memory.

A record closes on a close notification, when a message arrives under a new
interaction id, or once its idle window has passed, and the idle check runs
only when the agent's next event arrives. So a conversation the room ended
by its idle window, which tells no one, stays open until that event. An
agent that stops first used to drop it, and with it everything the
conversation should have left in memory. Now a stopping agent
(``close_memory(write_open=True)``, which ``AgentServer.stop`` passes)
closes every live open record, and waits for its summary, before the stores
close: by the idle rule if its window has already run out, and with the
shutdown reason otherwise. A record the catch-up replay opened is left
alone: it derives only when its pass ends, and the next boot reads its
window again.
"""

from __future__ import annotations

import contextlib
import copy
import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from agents.llm_client import LLMResponse
from agents.memory.interaction_janitor import SUMMARY_PENDING_TEXT
from agents.memory.interactions import Interaction
from agents.persona import create_persona_agent

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

_ROOM = "group:advice-1"
_LEASE = "The lease on our Quay Lane shop ends next June."
_REFIT = "Then the refit cannot pay back in time."
_SUMMARY = "The operator said the Quay Lane lease ends next June and will not be renewed."


def _agent(db: Path, *replies: str) -> Any:
    config = copy.deepcopy(_PERSONA_CONFIG)
    config["memory"]["db_path"] = str(db)
    client = _make_client([LLMResponse(text=reply) for reply in replies] or None)
    return create_persona_agent(agent_id="ember-owl", config=config, llm_client=client)


def _hear(
    agent: Any, speaker: str, text: str, *, replayed: bool = False, ago: float = 0.0,
) -> None:
    """A channel message the agent heard *ago* seconds back, still in its open record."""
    agent._interaction_tracker.add_turn(
        _ROOM, {"text": text, "sender": speaker}, now=time.time() - ago,
        source_channel_id=_ROOM, classification="internal", replayed=replayed,
        speaker_id=speaker,
    )


def _episodes(db: Path) -> list[tuple[str, dict[str, Any], str | None, float]]:
    with contextlib.closing(sqlite3.connect(db)) as store:
        rows = store.execute(
            "SELECT summary, context_json, speaker_id, closed_at FROM episodes",
        ).fetchall()
    return [
        (summary, json.loads(context or "{}"), speaker, at)
        for summary, context, speaker, at in rows
    ]


def _prompts(agent: Any) -> list[str]:
    """What the summariser was asked, one prompt per model call."""
    calls = agent._llm_client._provider.create_message.call_args_list
    return [call.kwargs["messages"][0]["content"] for call in calls]


class TestAStoppingAgentKeepsItsConversations:
    async def test_an_open_conversation_is_written_to_memory(self, tmp_path: Path) -> None:
        db = tmp_path / "memory.db"
        agent = _agent(db, _SUMMARY)
        await agent.initialize_memory()
        _hear(agent, "operator", _LEASE)
        await agent.close_memory(write_open=True)
        [(summary, context, speaker, _)] = _episodes(db)
        assert context["close_reason"] == "shutdown"
        assert speaker == "operator"
        assert summary != SUMMARY_PENDING_TEXT, "the summary is awaited before the stores close"

    async def test_the_summariser_hears_the_conversation_not_the_stop(
        self, tmp_path: Path,
    ) -> None:
        db = tmp_path / "memory.db"
        agent = _agent(db, _SUMMARY)
        await agent.initialize_memory()
        _hear(agent, "operator", _LEASE)
        await agent.close_memory(write_open=True)
        [prompt] = _prompts(agent)
        assert _LEASE in prompt
        assert "Close reason" not in prompt

    async def test_a_conversation_past_its_idle_window_closes_by_the_idle_rule(
        self, tmp_path: Path,
    ) -> None:
        db = tmp_path / "memory.db"
        agent = _agent(db, _SUMMARY, _SUMMARY)
        await agent.initialize_memory()
        _hear(agent, "operator", _LEASE, ago=7200)
        _hear(agent, "velvet-pika", _REFIT)
        await agent.close_memory(write_open=True)
        reasons = {speaker: context["close_reason"] for _, context, speaker, _ in _episodes(db)}
        assert reasons == {"operator": "idle_gap", "velvet-pika": "shutdown"}

    async def test_every_speakers_record_closes_at_one_instant(self, tmp_path: Path) -> None:
        db = tmp_path / "memory.db"
        agent = _agent(db, _SUMMARY, _SUMMARY)
        await agent.initialize_memory()
        _hear(agent, "operator", _LEASE, ago=7200)
        _hear(agent, "velvet-pika", _REFIT)
        await agent.close_memory(write_open=True)
        episodes = _episodes(db)
        assert sorted(speaker or "" for _, _, speaker, _ in episodes) == ["operator", "velvet-pika"]
        assert len({at for _, _, _, at in episodes}) == 1

    async def test_a_record_the_replay_opened_is_left_to_the_next_boot(
        self, tmp_path: Path,
    ) -> None:
        db = tmp_path / "memory.db"
        agent = _agent(db)
        await agent.initialize_memory()
        # Past its idle window too, so the stop's idle pass must pass it over as well.
        _hear(agent, "operator", "An old message the catch-up replayed.", replayed=True, ago=7200)
        await agent.close_memory(write_open=True)
        assert _episodes(db) == []
        # Not closed either: the replay pass, not the stop, decides it.
        assert [r.speaker_id for r in agent._interaction_tracker.open_records()] == ["operator"]

    async def test_an_agent_with_nothing_open_writes_nothing(self, tmp_path: Path) -> None:
        db = tmp_path / "memory.db"
        agent = _agent(db)
        await agent.initialize_memory()
        await agent.close_memory(write_open=True)
        assert _episodes(db) == []
        agent._llm_client._provider.create_message.assert_not_called()

    async def test_closing_the_stores_alone_writes_nothing(self, tmp_path: Path) -> None:
        db = tmp_path / "memory.db"
        agent = _agent(db)
        await agent.initialize_memory()
        _hear(agent, "operator", _LEASE)
        await agent.close_memory()
        assert _episodes(db) == []
        agent._llm_client._provider.create_message.assert_not_called()

    async def test_a_conversation_that_fails_to_write_does_not_stop_the_others(
        self, tmp_path: Path,
    ) -> None:
        db = tmp_path / "memory.db"
        agent = _agent(db, _SUMMARY)
        await agent.initialize_memory()
        _hear(agent, "operator", _LEASE)
        _hear(agent, "velvet-pika", _REFIT)
        persist = agent._persist_closed_interaction

        async def fail_the_operators(interaction: Interaction) -> None:
            if interaction.speaker_id == "operator":
                raise RuntimeError("disk full")
            await persist(interaction)

        agent._persist_closed_interaction = fail_the_operators
        await agent.close_memory(write_open=True)
        assert [speaker for _, _, speaker, _ in _episodes(db)] == ["velvet-pika"]

    async def test_a_failure_to_close_them_still_closes_the_stores(
        self, tmp_path: Path,
    ) -> None:
        agent = _agent(tmp_path / "memory.db")
        await agent.initialize_memory()
        agent.close_open_interactions = AsyncMock(side_effect=RuntimeError("boom"))
        await agent.close_memory(write_open=True)
        assert agent._episodic_memory._db is None
