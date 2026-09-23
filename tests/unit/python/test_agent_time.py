"""Agent time: the one clock a persona's memory and events read.

An agent process normally runs on real time. EXP-001 needs each adviser to
live on the meeting's story date instead (pre-registration §2: "Its clock
reads 10:00 on the meeting's story date"), and arm D's memories must age in
story time from one meeting to the next. Setting ``PERSATRIX_CLOCK_START``
shifts the whole agent's time: it starts at that instant and moves with the
real clock. Everything the persona remembers or renders reads that time;
timestamps from the orchestrator are moved by the same offset on the way in.
"""

from __future__ import annotations

import ast
import time
from datetime import datetime
from pathlib import Path

import pytest

from agents import clock as clock_mod
from agents.clock import (
    CLOCK_START_ENV,
    agent_clock_offset,
    agent_now,
    reset_agent_clock,
    resolve_persona_clock,
    to_agent_time,
)
from agents.persona_types import AgentEvent, EventType

_STORY_START = "2036-10-06T10:00:00+00:00"
_STORY_EPOCH = datetime.fromisoformat(_STORY_START).timestamp()
_REAL = 1_790_000_000.0  # a fixed real instant in 2026

_REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _fresh_agent_clock(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(CLOCK_START_ENV, raising=False)
    reset_agent_clock()
    yield
    reset_agent_clock()


@pytest.fixture
def real_time(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Pin the real clock; the test moves it by editing ``now[0]``."""
    now = [_REAL]
    monkeypatch.setattr(clock_mod.time, "time", lambda: now[0])
    return now


class TestUnshifted:
    def test_agent_time_is_real_time(self) -> None:
        before = time.time()
        observed = agent_now()
        after = time.time()
        assert before <= observed <= after
        assert agent_clock_offset() == 0.0

    def test_wire_timestamps_pass_through(self) -> None:
        assert to_agent_time(_REAL) == _REAL

    def test_blank_setting_means_real_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CLOCK_START_ENV, "  ")
        assert agent_clock_offset() == 0.0


class TestShifted:
    def test_starts_at_the_set_instant_and_moves_with_real_time(
        self, monkeypatch: pytest.MonkeyPatch, real_time: list[float],
    ) -> None:
        monkeypatch.setenv(CLOCK_START_ENV, _STORY_START)
        assert agent_now() == _STORY_EPOCH
        real_time[0] += 90.0
        assert agent_now() == _STORY_EPOCH + 90.0

    def test_offset_is_fixed_at_first_read(
        self, monkeypatch: pytest.MonkeyPatch, real_time: list[float],
    ) -> None:
        monkeypatch.setenv(CLOCK_START_ENV, _STORY_START)
        offset = agent_clock_offset()
        real_time[0] += 3_600.0
        assert agent_clock_offset() == offset

    def test_wire_timestamps_move_by_the_same_offset(
        self, monkeypatch: pytest.MonkeyPatch, real_time: list[float],
    ) -> None:
        monkeypatch.setenv(CLOCK_START_ENV, _STORY_START)
        published = _REAL - 30.0  # the orchestrator stamped it 30s ago
        assert to_agent_time(published) == _STORY_EPOCH - 30.0

    @pytest.mark.parametrize("value", ["2036-10-06T10:00:00", "next monday", "2036-13-45"])
    def test_a_start_without_a_zone_or_unparseable_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, value: str,
    ) -> None:
        monkeypatch.setenv(CLOCK_START_ENV, value)
        with pytest.raises(ValueError, match=CLOCK_START_ENV):
            agent_now()

    def test_persona_clock_reads_agent_time(
        self, monkeypatch: pytest.MonkeyPatch, real_time: list[float],
    ) -> None:
        monkeypatch.setenv(CLOCK_START_ENV, _STORY_START)
        clock, tz = resolve_persona_clock({"persona": {"timezone": "UTC"}})
        assert tz == "UTC"
        assert clock.now() == _STORY_EPOCH
        assert clock.now_iso() == _STORY_START

    def test_a_new_event_is_stamped_in_agent_time(
        self, monkeypatch: pytest.MonkeyPatch, real_time: list[float],
    ) -> None:
        monkeypatch.setenv(CLOCK_START_ENV, _STORY_START)
        assert AgentEvent(event_type=EventType.TICK).timestamp == _STORY_EPOCH

    def test_a_replayed_channel_message_is_moved_into_agent_time(
        self, monkeypatch: pytest.MonkeyPatch, real_time: list[float],
    ) -> None:
        from agents.channel_replay_event import build_replay_event

        monkeypatch.setenv(CLOCK_START_ENV, _STORY_START)
        published = datetime.fromtimestamp(_REAL - 30.0).astimezone().isoformat()
        event = build_replay_event(
            {"id": "m1", "content": "hi", "sender_id": "op", "timestamp": published},
            channel_id="room", respond_policy="always", channel={"channel_type": "group"},
        )
        assert event.timestamp == pytest.approx(_STORY_EPOCH - 30.0)

    async def test_a_live_channel_message_is_moved_into_agent_time(
        self, monkeypatch: pytest.MonkeyPatch, real_time: list[float],
    ) -> None:
        from unittest.mock import MagicMock

        import grpc

        from ._receive_channel_message_helpers import (
            channel_event,
            enqueued_event,
            make_servicer,
        )

        monkeypatch.setenv(CLOCK_START_ENV, _STORY_START)
        servicer, dispatcher = make_servicer()
        published = datetime.fromtimestamp(_REAL - 30.0).astimezone().isoformat()
        await servicer.ReceiveChannelMessage(
            channel_event(timestamp=published), MagicMock(spec=grpc.aio.ServicerContext),
        )
        assert enqueued_event(dispatcher).timestamp == pytest.approx(_STORY_EPOCH - 30.0)


async def test_a_note_is_stamped_in_agent_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_time: list[float],
) -> None:
    from agents.memory.episodic import EpisodicMemory

    monkeypatch.setenv(CLOCK_START_ENV, _STORY_START)
    memory = EpisodicMemory(db_path=str(tmp_path / "m.db"), agent_id="ember-owl")
    await memory.initialize()
    try:
        await memory.store_note(topic="Plan", content="Open the second site")
        assert memory._db is not None
        async with memory._db.execute("SELECT created_at, updated_at FROM notes") as cur:
            row = await cur.fetchone()
    finally:
        await memory.close()
    assert row is not None
    assert tuple(row) == (_STORY_EPOCH, _STORY_EPOCH)


# The modules whose times must all be agent time: every memory store and the
# persona runtime that renders them. One real-clock read among them would mix
# real and story timestamps in the same table.
_AGENT_TIME_PACKAGES = ("agents/memory", "agents/persona_runtime", "agents/temporal")
_AGENT_TIME_MODULES = ("agents/persona_types.py", "agents/channel_replay_event.py")


def _real_clock_reads(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        target = node.value
        name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
        if (name, node.attr) in {("time", "time"), ("datetime", "now"), ("datetime", "utcnow")}:
            found.append(f"{path.relative_to(_REPO)}:{node.lineno} {name}.{node.attr}")
    return found


def test_memory_and_the_persona_runtime_never_read_the_real_clock() -> None:
    files = [p for pkg in _AGENT_TIME_PACKAGES for p in (_REPO / pkg).rglob("*.py")]
    files += [_REPO / m for m in _AGENT_TIME_MODULES]
    assert len(files) > 40  # the scan found the packages
    offenders = [hit for path in files for hit in _real_clock_reads(path)]
    assert offenders == [], "read agents.clock.agent_now() instead:\n" + "\n".join(offenders)


def test_a_bad_start_stops_the_agent_server_at_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refused at startup, not at the first memory write mid-turn."""
    from agents import server_cli
    from agents.model_aliases import use_alias_map

    priced = {"quality": {"provider": "mock", "model": "mock", "input_per_1m_tokens": 0.0,
                          "output_per_1m_tokens": 0.0}}
    monkeypatch.setenv(CLOCK_START_ENV, "2036-10-06T10:00:00")
    with use_alias_map(priced), pytest.raises(SystemExit, match=CLOCK_START_ENV):
        server_cli._validate_startup_config()
