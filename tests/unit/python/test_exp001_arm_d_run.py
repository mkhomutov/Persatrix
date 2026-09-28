"""EXP-001 harness — arm D's meetings on their series' deployment (PR 5c).

The briefing writes the series' deployment; every later meeting, and every
try, starts its processes again on it, with the meeting's own clock and
call-log settings. Memory carries over and the persona state does not, a
restart that would change memory stops the run before the operator speaks
(check 2), and a try held again starts from the stores its meeting began
with. The processes and the orchestrator are stand-ins.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from agents.call_log import CALL_LOG_ENV, CALL_TAGS_ENV
from agents.clock import CLOCK_START_ENV
from evaluators.exp001.arm_d import (
    STORES_BEFORE,
    arm_d_hold,
    memory_rows,
    run_meeting,
    series_channels,
)
from evaluators.exp001.attempts import RETRIES, HarnessFault, run_series
from evaluators.exp001.deployed_meeting import RECORD
from evaluators.exp001.deployment import REPO, DeploymentError, Layout, StartError
from evaluators.exp001.materials import Meeting, load_series
from evaluators.exp001.orchestrator import Message
from evaluators.exp001.panel import load_panel
from evaluators.exp001.processes import Process

_EXP = REPO / "evaluators" / "experiments" / "EXP-001"
PANEL = load_panel(_EXP / "panel.yaml")
SERIES = load_series(_EXP / "practice.yaml")
BRIEFING, PLAN = SERIES.meetings[:2]
_T0 = dt.datetime(2026, 10, 1, 9, 0, tzinfo=dt.UTC)
_CATCH_UP = "channels: catch-up complete agent={} channels=4 events={} elapsed_ms=9"


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(line + "\n")


def _log_line(message: str, **fields: Any) -> str:
    return json.dumps({"timestamp": _T0.isoformat(), "message": message, **fields})


class _Handle:
    """A process that stops *polls* polls after it is asked to, as an adviser
    writing memory does, or never, when it *hangs*, until it is killed."""

    def __init__(
        self, *, polls: int = 0, hangs: bool = False, on_stop: Callable[[], None] | None = None,
    ) -> None:
        self.returncode: int | None = None
        self.polls = polls
        self.hangs = hangs
        self.on_stop = on_stop
        self.stopping = False

    def poll(self) -> int | None:
        if self.stopping and self.returncode is None and not self.hangs:
            if self.polls <= 0:
                self.returncode = 0
            self.polls -= 1
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.stopping = True
        if self.on_stop is not None:
            self.on_stop()

    def kill(self) -> None:
        self.returncode = -9


class _World:
    """Stand-in processes for one series' deployments, and an orchestrator whose
    discussions close at once. An adviser's store is shaped like the runtime's;
    each meeting leaves a row in it and a line in the orchestrator's store, and
    each start logs the end of the adviser's catch-up pass."""

    def __init__(
        self, *, replayed: int = 0, logs_catch_up: bool = True, restart_writes: bool = False,
        failing: Sequence[tuple[str, int, int]] = (), exits: str | None = None,
        hangs: str | None = None, fails_at_stop: str | None = None, stop_polls: int = 0,
    ) -> None:
        self.replayed = replayed
        self.logs_catch_up = logs_catch_up
        self.restart_writes = restart_writes  # an adviser that adds to memory as it restarts
        self.failing = set(failing)  # (meeting, attempt, try): a provider error in the call log
        self.exits = exits  # an adviser that exits once registered, before its pass ends
        self.hangs = hangs  # an adviser that does not stop until it is killed
        self.fails_at_stop = fails_at_stop  # a meeting whose first try's stop logs a failed summary
        self.stop_polls = stop_polls
        self.handles: dict[str, _Handle] = {}
        self.processes: dict[str, Process] = {}
        self.found: list[tuple[str, str, str, Any, Any]] = []  # what each adviser found at a start
        self.posts: list[tuple[str, str]] = []
        self.seconds = 0.0
        self._messages: list[Message] = []

    def now(self) -> dt.datetime:
        return _T0 + dt.timedelta(seconds=self.seconds)

    async def sleep(self, seconds: float) -> None:
        self.seconds += seconds
        await asyncio.sleep(0)

    def _layout(self, process: Process) -> Layout:
        config = Path(process.argv[process.argv.index("--config") + 1])
        return Layout(config.parent if process.name == "orchestrator" else config.parent.parent)

    def spawn(self, process: Process, environ: Mapping[str, str]) -> _Handle:
        self.processes[process.name] = process
        layout = self._layout(process)
        if process.name == "orchestrator":
            _append(process.log, _log_line("security.rate_limit.disabled scope=startup"))
            _append(process.log, _log_line("wallet lease enforcement initialized"))
            _append(process.log, _log_line("gRPC server listening"))
            self._messages = []
            self.handles[process.name] = _Handle()
            return self.handles[process.name]
        db = layout.memory_db(process.name)
        tags = json.loads(process.env[CALL_TAGS_ENV])
        state = _state(db) if db.exists() else None
        self.found.append((tags["meeting"], tags["try"], process.name, state, memory_rows(db)))
        existed = db.exists()
        _open(db, process.name)
        if self.restart_writes and existed and process.name == "ripple-kite":
            _remember(db, "replayed at the restart")
        if self.logs_catch_up and process.name != self.exits:
            _append(process.log, _log_line(_CATCH_UP.format(process.name, self.replayed)))
        on_stop: Callable[[], None] | None = None
        if (tags["meeting"], tags["try"]) == (self.fails_at_stop, "1") and (
            process.name == PANEL.chair.id
        ):
            def fail() -> None:  # the chair writes what it held open, and a summary fails
                _append(Path(process.env[CALL_LOG_ENV]), _failed(tags, "summary", self.now()))

            on_stop = fail
        self.handles[process.name] = _Handle(
            polls=self.stop_polls, hangs=process.name == self.hangs, on_stop=on_stop,
        )
        return self.handles[process.name]

    async def healthy(self) -> bool:
        return True

    async def agents(self) -> set[str]:
        if self.exits in self.handles:
            self.handles[self.exits].returncode = 1  # once it has registered
        return {name for name in self.processes if name != "orchestrator"}

    async def post(
        self, channel: str, sender: str, content: str, *, mentions: Sequence[str] = (),
    ) -> Message:
        self.posts.append((channel, content))
        message = Message(id=f"m-{len(self._messages)}", sender=sender, content=content,
                          at=self.now(), mentions=tuple(mentions))
        self._messages.append(message)
        if len(self._messages) > 1:  # the memo request: the chair answers
            self._messages.append(Message(id="memo", sender=PANEL.chair.id, content="Memo.",
                                          at=self.now()))
            return message
        orchestrator = self.processes["orchestrator"]
        layout = self._layout(orchestrator)
        adviser = self.processes[PANEL.chair.id]
        tags = json.loads(adviser.env[CALL_TAGS_ENV])
        where = f"{tags['meeting']}/{tags['attempt']}/{tags['try']}"
        _append(layout.data / "channels.db", f"{channel}: {where}")
        for a in PANEL.advisers:
            _remember(layout.memory_db(a.id), where)
        if (tags["meeting"], int(tags["attempt"]), int(tags["try"])) in self.failing:
            _append(Path(adviser.env[CALL_LOG_ENV]), _failed(tags, "turn", _T0))
        _append(orchestrator.log, _log_line(
            "channels: interaction closed by RFC 0052 bounded close", channel_id=channel,
            interaction_id="i-1", trigger="structural",
        ))
        return message

    async def messages(self, channel: str) -> list[Message]:
        return list(self._messages)

    async def activity(self, channel: str) -> set[str]:
        return set()

    async def disarm(self, channel: str) -> None:
        pass

    async def set_respond(self, channel: str, member: str, respond: str) -> None:
        pass


def _failed(tags: Mapping[str, str], purpose: str, at: dt.datetime) -> str:
    """A call-log line for a call the provider refused with a rate limit."""
    return json.dumps({
        "tags": tags, "agent_id": "ripple-kite", "purpose": purpose,
        "provider": "anthropic", "model": "claude-sonnet-4-6", "model_alias": None,
        "started_at": at.isoformat(), "input_tokens": 0, "output_tokens": 0,
        "cache_write_tokens": 0, "cache_read_tokens": 0, "error": "RateLimitError",
    })


def _open(db: Path, adviser: str) -> None:
    """What the runtime does to a store as it starts: its tables, and its saved state."""
    db.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(db)) as store, store:
        store.execute(
            "CREATE TABLE IF NOT EXISTS agent_state (agent_id TEXT PRIMARY KEY, "
            "interaction_count INTEGER, persona_state_json TEXT, updated_at REAL)",
        )
        store.execute("CREATE TABLE IF NOT EXISTS episodes (summary TEXT)")
        store.execute(
            "INSERT INTO agent_state VALUES (?, 0, NULL, 0) ON CONFLICT(agent_id) DO UPDATE "
            "SET updated_at = updated_at + 1", (adviser,),
        )


def _remember(db: Path, what: str) -> None:
    """A meeting's mark on an adviser's store: memory, a tired persona, a count."""
    with contextlib.closing(sqlite3.connect(db)) as store, store:
        store.execute("INSERT INTO episodes VALUES (?)", (what,))
        store.execute(
            "UPDATE agent_state SET persona_state_json = '{\"energy\": 0.4}', "
            "interaction_count = interaction_count + 1",
        )


def _state(db: Path) -> tuple[int, str | None]:
    with contextlib.closing(sqlite3.connect(db)) as store:
        row = store.execute(
            "SELECT interaction_count, persona_state_json FROM agent_state",
        ).fetchone()
    return row[0], row[1]


async def _run(world: _World, root: Path, meeting: Meeting, meeting_try: int = 1) -> Any:
    here = root / "attempt-1"
    return await run_meeting(
        PANEL, SERIES, meeting, attempt=1, meeting_try=meeting_try,
        directory=here / meeting.id / f"try-{meeting_try}", deployment=here / "deployment",
        before=here / meeting.id / STORES_BEFORE, binary=Path("/repo/bin/persatrix-server"),
        python=Path("/venv/bin/python"), repo=Path("/repo"), spawn=world.spawn, room=world,
        now=world.now, sleep=world.sleep,
    )


def _found(world: _World, meeting: Meeting, meeting_try: str = "1") -> dict[str, Any]:
    return {a: rows for m, t, a, _, rows in world.found if (m, t) == (meeting.id, meeting_try)}


class TestRunMeeting:
    async def test_the_briefing_writes_the_series_deployment(self, tmp_path: Path) -> None:
        world = _World()
        await _run(world, tmp_path, BRIEFING)
        config = tmp_path / "attempt-1" / "deployment" / "config"
        assert yaml.safe_load((config / "channels.yaml").read_text())["channels"] == (
            series_channels(PANEL, SERIES)
        )
        assert world.posts[0] == ("group:advice-1", BRIEFING.message)
        logs = tmp_path / "attempt-1" / BRIEFING.id / "try-1" / "logs"
        assert sorted(p.name for p in logs.iterdir()) == sorted(
            f"{name}.log" for name in world.processes
        )

    async def test_a_later_meeting_runs_on_the_same_deployment_with_its_own_settings(
        self, tmp_path: Path,
    ) -> None:
        world = _World()
        await _run(world, tmp_path, BRIEFING)
        written = (tmp_path / "attempt-1" / "deployment" / "config" / "agents.yaml").stat()
        await _run(world, tmp_path, PLAN)
        env = world.processes["velvet-pika"].env
        assert env[CLOCK_START_ENV] == f"{PLAN.story_date.isoformat()}T10:00:00+00:00"
        assert env[CALL_LOG_ENV] == str(tmp_path / "attempt-1" / PLAN.id / "try-1" / "calls.jsonl")
        assert json.loads(env[CALL_TAGS_ENV])["meeting"] == PLAN.id
        assert world.posts[1] == ("group:advice-2", PLAN.message)
        again = (tmp_path / "attempt-1" / "deployment" / "config" / "agents.yaml").stat()
        assert again.st_mtime_ns == written.st_mtime_ns

    async def test_memory_carries_to_the_next_meeting(self, tmp_path: Path) -> None:
        world = _World()
        await _run(world, tmp_path, BRIEFING)
        await _run(world, tmp_path, PLAN)
        for rows in _found(world, PLAN).values():
            assert rows["episodes"] == (repr((f"{BRIEFING.id}/1/1",)),)

    async def test_each_start_puts_the_persona_state_back_and_keeps_the_counter(
        self, tmp_path: Path,
    ) -> None:
        world = _World()
        await _run(world, tmp_path, BRIEFING)
        await _run(world, tmp_path, PLAN)
        states = {a: state for m, _, a, state, _ in world.found if m == PLAN.id}
        assert states == {a.id: (1, None) for a in PANEL.advisers}

    async def test_a_restart_that_changes_memory_stops_the_meeting_before_the_operator_speaks(
        self, tmp_path: Path,
    ) -> None:
        world = _World()
        await _run(world, tmp_path, BRIEFING)
        world.restart_writes = True
        with pytest.raises(DeploymentError, match=r"changed the memory stores \(check 2\): "
                                                  r"ripple-kite: episodes"):
            await _run(world, tmp_path, PLAN)
        assert len(world.posts) == 1  # the briefing's
        record = json.loads((tmp_path / "attempt-1" / PLAN.id / "try-1" / RECORD).read_text())
        assert record["error"].startswith("DeploymentError: a restart changed")

    async def test_a_restart_that_replayed_messages_stops_the_meeting(
        self, tmp_path: Path,
    ) -> None:
        world = _World()
        await _run(world, tmp_path, BRIEFING)
        world.replayed = 3
        with pytest.raises(DeploymentError, match=r"replayed messages into memory \(check 2\)"):
            await _run(world, tmp_path, PLAN)
        assert len(world.posts) == 1

    async def test_a_catch_up_pass_that_never_ends_stops_the_meeting(self, tmp_path: Path) -> None:
        world = _World(logs_catch_up=False)
        with pytest.raises(DeploymentError, match="no catch-up pass ended within 90 s"):
            await _run(world, tmp_path, BRIEFING)
        assert world.posts == []

    async def test_an_adviser_that_exits_before_its_pass_ends_is_a_start_that_failed(
        self, tmp_path: Path,
    ) -> None:
        """As a process that exits before the operator speaks is for B and C (PR 5b)."""
        world = _World(exits="ripple-kite")
        with pytest.raises(StartError, match=r"ripple-kite exited \(1\)"):
            await _run(world, tmp_path, BRIEFING)
        assert world.posts == []
        assert world.seconds < 1  # no wait for a pass that cannot end

    async def test_an_adviser_killed_at_the_stop_fails_the_meeting(self, tmp_path: Path) -> None:
        """It may not have written what it still held open, which D's next meeting reads."""
        world = _World(hangs="velvet-pika")
        with pytest.raises(DeploymentError, match="killed at the stop: velvet-pika"):
            await _run(world, tmp_path, BRIEFING)
        record = tmp_path / "attempt-1" / BRIEFING.id / "try-1" / RECORD
        assert json.loads(record.read_text())["failures"] == []

    async def test_a_try_held_again_starts_from_the_stores_its_meeting_began_with(
        self, tmp_path: Path,
    ) -> None:
        world = _World()
        await _run(world, tmp_path, BRIEFING)
        await _run(world, tmp_path, PLAN, meeting_try=1)
        await _run(world, tmp_path, PLAN, meeting_try=2)
        assert _found(world, PLAN, "2") == _found(world, PLAN, "1")
        channels = (tmp_path / "attempt-1" / "deployment" / "data" / "channels.db").read_text()
        assert channels.splitlines() == [
            f"group:advice-1: {BRIEFING.id}/1/1", f"group:advice-2: {PLAN.id}/1/2",
        ]

    async def test_a_later_meeting_needs_the_series_deployment(self, tmp_path: Path) -> None:
        world = _World()
        with pytest.raises(DeploymentError, match="the briefing writes it"):
            await _run(world, tmp_path, PLAN)
        assert world.processes == {}

    async def test_a_try_directory_that_holds_anything_is_refused(self, tmp_path: Path) -> None:
        """An earlier run's call log would be appended to, and counted again."""
        directory = tmp_path / "attempt-1" / BRIEFING.id / "try-1"
        directory.mkdir(parents=True)
        (directory / "calls.jsonl").write_text("{}\n")
        with pytest.raises(DeploymentError, match="is not empty"):
            await _run(_World(), tmp_path, BRIEFING)


async def _no_wait(seconds: float) -> None:
    """Stands in for asyncio.sleep between tries."""


def _hold(world: _World, root: Path, **options: Any) -> Any:
    return arm_d_hold(
        PANEL, SERIES, root, binary=Path("/repo/bin/persatrix-server"),
        python=Path("/venv/bin/python"), repo=Path("/repo"), spawn=world.spawn, room=world,
        now=world.now, sleep=world.sleep, **options,
    )


class TestHold:
    async def test_every_try_of_an_attempt_is_held_on_its_one_deployment(
        self, tmp_path: Path,
    ) -> None:
        world = _World(failing=[(PLAN.id, 1, 1)])
        run = await run_series("D", SERIES, _hold(world, tmp_path), sleep=_no_wait)
        assert run.finished_tries()[PLAN.id] == 2
        here = tmp_path / "attempt-1"
        assert sorted(p.name for p in here.iterdir()) == sorted(
            ["deployment", *(m.id for m in SERIES.meetings)],
        )
        assert sorted(p.name for p in (here / PLAN.id).iterdir()) == [
            STORES_BEFORE, "try-1", "try-2",
        ]
        assert _found(world, PLAN, "2") == _found(world, PLAN, "1")

    async def test_a_series_started_again_is_held_on_a_new_deployment(
        self, tmp_path: Path,
    ) -> None:
        world = _World(failing=[(PLAN.id, 1, n) for n in range(1, RETRIES + 2)])
        run = await run_series("D", SERIES, _hold(world, tmp_path), sleep=_no_wait)
        assert run.finished_attempt == 2
        config = world.processes["orchestrator"].argv.index("--config") + 1
        assert world.processes["orchestrator"].argv[config] == str(
            tmp_path / "attempt-2" / "deployment" / "config",
        )
        second = [rows for m, t, _, _, rows in world.found if m == BRIEFING.id][-1]
        assert second is None  # the new deployment's stores start empty

    async def test_a_restart_that_changes_memory_is_a_harness_fault(self, tmp_path: Path) -> None:
        world = _World(restart_writes=True)
        with pytest.raises(HarnessFault, match=f"D, practice, {PLAN.id}, attempt 1, try 1: "
                                               "DeploymentError: a restart changed"):
            await run_series("D", SERIES, _hold(world, tmp_path), sleep=_no_wait)

    async def test_a_summary_that_fails_at_the_stop_lets_the_stop_finish(
        self, tmp_path: Path,
    ) -> None:
        """The try is held again all the same, from the stores its meeting began
        with. Killing the advisers still writing would lose their calls from the
        call log, which counts the spend."""
        world = _World(fails_at_stop=PLAN.id, stop_polls=20)
        hold = _hold(world, tmp_path, watch_seconds=0)
        await hold(BRIEFING, 1, 1)
        held = await hold(PLAN, 1, 1)
        assert held.result is not None and held.cut_short
        assert [n for n, h in world.handles.items() if h.returncode != 0] == []

    async def test_a_summary_that_fails_once_the_series_last_meeting_is_over_changes_nothing(
        self, tmp_path: Path,
    ) -> None:
        """No later meeting reads what the last one writes to memory as it stops."""
        last = SERIES.meetings[-1]
        world = _World(fails_at_stop=last.id)
        run = await run_series("D", SERIES, _hold(world, tmp_path), sleep=_no_wait)
        assert run.finished_tries()[last.id] == 1
