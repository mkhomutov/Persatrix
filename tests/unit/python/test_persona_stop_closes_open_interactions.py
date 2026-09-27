"""ISSUE-0172 — an agent that stops writes its open conversations to memory.

A record closes on a close notification, when a message arrives under a new
interaction id, or once its idle window has passed, and the idle check runs
only when the agent's next event arrives. So a conversation the room ended
by its idle window, which tells no one, stays open until that event. An
agent that stops first used to drop it, and with it everything the
conversation should have left in memory. Now ``close_memory`` closes every
live open record with the shutdown reason, and waits for its summary, before
the stores close. A record the catch-up replay opened is left alone: it
derives only when its pass ends, and the next boot reads its window again.
"""

from __future__ import annotations

import contextlib
import copy
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from agents.llm_client import LLMResponse
from agents.memory.interaction_janitor import SUMMARY_PENDING_TEXT
from agents.persona import create_persona_agent

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

_ROOM = "group:advice-1"
_SUMMARY = "The operator said the Quay Lane lease ends next June and will not be renewed."


def _agent(db: Path, *replies: str) -> Any:
    config = copy.deepcopy(_PERSONA_CONFIG)
    config["memory"]["db_path"] = str(db)
    client = _make_client([LLMResponse(text=reply) for reply in replies] or None)
    return create_persona_agent(agent_id="ember-owl", config=config, llm_client=client)


def _hear(agent: Any, speaker: str, text: str, *, replayed: bool = False) -> None:
    """A channel message the agent heard, still in its open record."""
    agent._interaction_tracker.add_turn(
        _ROOM, {"text": text, "sender": speaker}, now=time.time(),
        source_channel_id=_ROOM, classification="internal", replayed=replayed,
        speaker_id=speaker,
    )


def _episodes(db: Path) -> list[tuple[str, dict[str, Any], str | None]]:
    with contextlib.closing(sqlite3.connect(db)) as store:
        rows = store.execute("SELECT summary, context_json, speaker_id FROM episodes").fetchall()
    return [(summary, json.loads(context or "{}"), speaker) for summary, context, speaker in rows]


class TestAStoppingAgentKeepsItsConversations:
    async def test_an_open_conversation_is_written_to_memory(self, tmp_path: Path) -> None:
        db = tmp_path / "memory.db"
        agent = _agent(db, _SUMMARY)
        await agent.initialize_memory()
        _hear(agent, "operator", "The lease on our Quay Lane shop ends next June.")
        await agent.close_memory()
        [(summary, context, speaker)] = _episodes(db)
        assert context["close_reason"] == "shutdown"
        assert speaker == "operator"
        assert summary != SUMMARY_PENDING_TEXT, "the summary is awaited before the stores close"
        assert "Quay Lane" in summary

    async def test_every_speakers_record_closes_at_one_instant(self, tmp_path: Path) -> None:
        db = tmp_path / "memory.db"
        agent = _agent(db, _SUMMARY, _SUMMARY)
        await agent.initialize_memory()
        _hear(agent, "operator", "The lease on our Quay Lane shop ends next June.")
        _hear(agent, "velvet-pika", "Then the refit cannot pay back in time.")
        await agent.close_memory()
        episodes = _episodes(db)
        assert sorted(speaker or "" for _, _, speaker in episodes) == ["operator", "velvet-pika"]
        assert {context["close_reason"] for _, context, _ in episodes} == {"shutdown"}
        with contextlib.closing(sqlite3.connect(db)) as store:
            instants = {at for (at,) in store.execute("SELECT closed_at FROM episodes")}
        assert len(instants) == 1

    async def test_a_record_the_replay_opened_is_left_to_the_next_boot(
        self, tmp_path: Path,
    ) -> None:
        db = tmp_path / "memory.db"
        agent = _agent(db)
        await agent.initialize_memory()
        _hear(agent, "operator", "An old message the catch-up replayed.", replayed=True)
        await agent.close_memory()
        assert _episodes(db) == []
        # Not closed either: the replay pass, not the stop, decides it.
        assert [r.speaker_id for r in agent._interaction_tracker.open_records()] == ["operator"]

    async def test_an_agent_with_nothing_open_writes_nothing(self, tmp_path: Path) -> None:
        db = tmp_path / "memory.db"
        agent = _agent(db)
        await agent.initialize_memory()
        await agent.close_memory()
        assert _episodes(db) == []
        agent._llm_client._provider.create_message.assert_not_called()

    async def test_a_caller_whose_run_is_over_can_leave_them_unwritten(
        self, tmp_path: Path,
    ) -> None:
        """The golden-trace driver's run ends at its snapshot, and no
        recording holds the model call a summary makes."""
        db = tmp_path / "memory.db"
        agent = _agent(db)
        await agent.initialize_memory()
        _hear(agent, "operator", "The lease on our Quay Lane shop ends next June.")
        await agent.close_memory(write_open=False)
        assert _episodes(db) == []
        agent._llm_client._provider.create_message.assert_not_called()
