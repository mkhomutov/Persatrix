"""A persona that never ticks still rescues a stuck episode (ISSUE-0198).

When an interaction closes, its episode is first written with the
sentinel ``[summary pending]`` and the real summary is filled in by a
second step.  If that step never finishes (a crash, a stop that ran out
of time), a sweep marks the row ``[interaction summary unavailable]`` so
recall shows it and a later boot can derive a replayed span again.

That sweep used to run only at the end of a tick.  The stock personas
ship ``timers: []`` and a ``reactive`` persona has no scheduler, so for
them it never ran.  It now also runs when the persona's memory opens
(before the boot catch-up asks whether a span was already derived) and
when an event arrives, with the tick path's five-minute cooldown.

Each test seeds the store the way a stopped process leaves it, on a real
file, and then starts a persona on that file.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.memory.boundary_detectors import DEFAULT_CLOSING_GRACE_SEC
from agents.memory.episodic import EpisodicMemory
from agents.memory.interactions import (
    SUMMARY_PENDING_TEXT,
    SUMMARY_UNAVAILABLE_TEXT,
)
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_runtime.finalize_close import JANITOR_INTERVAL_SEC
from agents.persona_types import AgentEvent, EventType, turn_failure
from agents.tools.registry import clear_registry

AGENT = "ember-owl"
PEER = "iron-fox"
DM = f"dm:{AGENT}:{PEER}"

#: A replayed span's boot-stable id, the shape ``replay_identity`` mints.
DIGEST = "replay-" + "b" * 64


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _config(db_path: str, **overrides: Any) -> dict[str, Any]:
    """A persona that never ticks: ``reactive``, so it gets no scheduler."""
    config: dict[str, Any] = {
        "id": AGENT,
        "model": "test-model",
        "role": "a persona with no timers",
        "type": "persona",
        "max_llm_calls": 3,
        "max_tokens": 4096,
        "tools": [],
        "persona": {"name": "Ember Owl", "background": "ISSUE-0198 test persona."},
        "autonomy": {"level": "reactive", "timers": []},
        "memory": {"db_path": db_path, "working": {"max_tokens": 50000}},
        "relationships": [],
    }
    config.update(overrides)
    return config


def _silent_client(*, gate: asyncio.Event | None = None) -> LLMClient:
    """A model that answers "do nothing"; with ``gate`` it never answers."""
    provider = AsyncMock()

    async def _reply(**_kwargs: Any) -> LLMResponse:
        if gate is not None:
            await gate.wait()
        return LLMResponse(
            text='```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
            stop_reason=StopReason.END_TURN, usage=Usage(10, 5),
        )

    provider.create_message = AsyncMock(side_effect=_reply)
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: msgs,
    )
    return LLMClient(provider)


async def _leave_stuck_row(
    db_path: str, *, age_sec: float, interaction_id: str = DIGEST,
) -> None:
    """Write what a process that died mid-close leaves: a pending row."""
    store = EpisodicMemory(agent_id=AGENT, db_path=db_path)
    await store.initialize()
    try:
        await _insert_stuck_row(store, age_sec=age_sec, interaction_id=interaction_id)
    finally:
        await store.close()


async def _insert_stuck_row(
    store: EpisodicMemory, *, age_sec: float, interaction_id: str = DIGEST,
) -> None:
    from agents.clock import agent_now  # noqa: PLC0415

    closed_at = agent_now() - age_sec
    await store.store_episode(
        summary=SUMMARY_PENDING_TEXT, context={"scope": DM},
        interaction_id=interaction_id, scope=DM,
        started_at=closed_at - 60.0, closed_at=closed_at, turn_count=2,
    )


async def _summaries(agent: _LLMPersonaAgent) -> dict[str, str]:
    db = agent._episodic_memory._ensure_db()
    async with db.execute(
        "SELECT interaction_id, summary FROM episodes WHERE agent_id = ?",
        (agent.agent_id,),
    ) as cursor:
        return {row[0]: row[1] for row in await cursor.fetchall()}


async def _boot(db_path: str, **kwargs: Any) -> _LLMPersonaAgent:
    client = kwargs.pop("client", None) or _silent_client()
    agent = create_persona_agent(
        agent_id=AGENT, config=_config(db_path, **kwargs), llm_client=client,
    )
    await agent.initialize_memory()
    return agent


def _message(text: str = "hello") -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE, payload={"content": text},
        sender_id=PEER, channel_id=DM, message_id="m-1",
    )


OLD = DEFAULT_CLOSING_GRACE_SEC + 60.0
YOUNG = DEFAULT_CLOSING_GRACE_SEC / 2


@pytest.mark.asyncio
class TestTheSweepRunsWhenMemoryOpens:
    async def test_a_row_stuck_by_the_last_process_is_marked_at_boot(
        self, tmp_path: Path,
    ) -> None:
        path = str(tmp_path / "memory.db")
        await _leave_stuck_row(path, age_sec=OLD)

        agent = await _boot(path)
        try:
            assert await _summaries(agent) == {DIGEST: SUMMARY_UNAVAILABLE_TEXT}
        finally:
            await agent.close_memory()

    async def test_the_boot_catch_up_then_finds_the_span_not_derived(
        self, tmp_path: Path,
    ) -> None:
        """The order that matters: the sweep, then the guard's read.

        A pending row counts as derived; a marked one does not.  With the
        sweep at boot the catch-up that follows ``initialize_memory`` asks
        about the replayed span and is told to derive it again.
        """
        path = str(tmp_path / "memory.db")
        await _leave_stuck_row(path, age_sec=OLD)

        agent = await _boot(path)
        try:
            derived = await agent._episodic_memory.has_episode_for_interaction(DIGEST)
            assert derived is False
        finally:
            await agent.close_memory()

    async def test_a_row_younger_than_the_grace_period_is_left_at_boot(
        self, tmp_path: Path,
    ) -> None:
        """The boot sweep keeps the sweep's own rule: only rows past the grace.

        A restart within the grace period leaves the row pending, and the
        event path below marks it once the cooldown has passed.
        """
        path = str(tmp_path / "memory.db")
        await _leave_stuck_row(path, age_sec=YOUNG)

        agent = await _boot(path)
        try:
            assert await _summaries(agent) == {DIGEST: SUMMARY_PENDING_TEXT}
        finally:
            await agent.close_memory()

    async def test_another_agents_stuck_row_on_the_same_file_is_left(
        self, tmp_path: Path,
    ) -> None:
        path = str(tmp_path / "memory.db")
        other = EpisodicMemory(agent_id="quiet-heron", db_path=path)
        await other.initialize()
        await _insert_stuck_row(other, age_sec=OLD, interaction_id="theirs")

        agent = await _boot(path)
        try:
            db = other._ensure_db()
            async with db.execute(
                "SELECT summary FROM episodes WHERE agent_id = 'quiet-heron'",
            ) as cursor:
                rows = await cursor.fetchall()
            assert [row[0] for row in rows] == [SUMMARY_PENDING_TEXT]
        finally:
            await agent.close_memory()
            await other.close()

    async def test_a_sweep_that_fails_does_not_stop_the_boot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        path = str(tmp_path / "memory.db")
        agent = create_persona_agent(
            agent_id=AGENT, config=_config(path), llm_client=_silent_client(),
        )
        monkeypatch.setattr(
            agent, "cleanup_closing_interactions",
            AsyncMock(side_effect=RuntimeError("database is locked")),
        )
        with caplog.at_level(logging.WARNING):
            await agent.initialize_memory()
        try:
            assert agent.state is not None, "the boot went on to load the state"
            assert [
                r.getMessage() for r in caplog.records
                if r.name == "agents.persona_runtime.finalize_close"
            ] == [f"Janitor sweep failed for agent {AGENT}"]
        finally:
            await agent.close_memory()


@pytest.mark.asyncio
class TestTheSweepRunsOnAnInboundEvent:
    async def test_an_event_after_the_cooldown_marks_a_stuck_row(
        self, tmp_path: Path,
    ) -> None:
        """A row the boot sweep left (too young then) is marked later."""
        agent = await _boot(str(tmp_path / "memory.db"))
        try:
            await _insert_stuck_row(agent._episodic_memory, age_sec=OLD)
            _let_the_cooldown_pass(agent)

            await agent.on_event(_message())

            assert (await _summaries(agent))[DIGEST] == SUMMARY_UNAVAILABLE_TEXT
        finally:
            await agent.close_memory()

    async def test_an_event_inside_the_cooldown_does_not_sweep(
        self, tmp_path: Path,
    ) -> None:
        """One sweep per five minutes, counted from the boot sweep."""
        agent = await _boot(str(tmp_path / "memory.db"))
        try:
            await _insert_stuck_row(agent._episodic_memory, age_sec=OLD)

            await agent.on_event(_message())

            assert (await _summaries(agent))[DIGEST] == SUMMARY_PENDING_TEXT
        finally:
            await agent.close_memory()

    async def test_the_cooldown_is_shared_with_the_tick_path(
        self, tmp_path: Path,
    ) -> None:
        """An event's sweep restarts the cooldown the next event reads."""
        agent = await _boot(str(tmp_path / "memory.db"))
        try:
            _let_the_cooldown_pass(agent)
            await agent.on_event(_message("first"))
            await _insert_stuck_row(agent._episodic_memory, age_sec=OLD)

            await agent.on_event(_message("second"))
            assert (await _summaries(agent))[DIGEST] == SUMMARY_PENDING_TEXT

            _let_the_cooldown_pass(agent)
            await agent.on_tick()
            assert (await _summaries(agent))[DIGEST] == SUMMARY_UNAVAILABLE_TEXT
        finally:
            await agent.close_memory()

    async def test_an_event_that_times_out_still_sweeps(
        self, tmp_path: Path,
    ) -> None:
        """The sweep does not depend on the turn succeeding."""
        never = asyncio.Event()
        agent = await _boot(
            str(tmp_path / "memory.db"),
            client=_silent_client(gate=never), event_timeout=0.05,
        )
        try:
            await _insert_stuck_row(agent._episodic_memory, age_sec=OLD)
            _let_the_cooldown_pass(agent)

            await agent.on_event(_message())

            assert (await _summaries(agent))[DIGEST] == SUMMARY_UNAVAILABLE_TEXT
        finally:
            await agent.close_memory()

    async def test_a_sweep_that_fails_does_not_cost_the_event_its_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _silent_client()
        agent = await _boot(str(tmp_path / "memory.db"), client=client)
        try:
            _let_the_cooldown_pass(agent)
            monkeypatch.setattr(
                agent, "cleanup_closing_interactions",
                AsyncMock(side_effect=RuntimeError("database is locked")),
            )

            actions = await agent.on_event(_message())

            provider: Any = client._provider
            assert provider.create_message.await_count == 1, "the turn ran"
            assert turn_failure(actions) is None, "and did not fail"
        finally:
            await agent.close_memory()


def _let_the_cooldown_pass(agent: _LLMPersonaAgent) -> None:
    """Put the last sweep more than one cooldown into the past."""
    agent._last_janitor_monotonic = time.monotonic() - JANITOR_INTERVAL_SEC - 1.0
