"""EXP-001 harness — one meeting of arm B or C, start to finish (PR 5a).

Each meeting of B and C runs on a deployment of its own, written into the
meeting's directory, so it starts with empty stores (check 1). Every adviser
of the meeting gets the same clock and call-log settings (checks 7 and 4).
The orchestrator's rate limiter must be off before the operator speaks, and
a lease the wallet refused fails the meeting (check 6); either way the
deployment is stopped.
"""

from __future__ import annotations

import datetime as dt
import json
import signal
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from agents.call_log import CALL_LOG_ENV, CALL_TAGS_ENV
from agents.clock import CLOCK_ANCHOR_ENV, CLOCK_START_ENV
from evaluators.exp001.channel_arm import run_meeting
from evaluators.exp001.deployment import DeploymentError, channel_config
from evaluators.exp001.materials import MeetingKind, load_series
from evaluators.exp001.orchestrator import Message
from evaluators.exp001.panel import load_panel
from evaluators.exp001.processes import Process

_EXP = Path(__file__).resolve().parents[3] / "evaluators" / "experiments" / "EXP-001"
PANEL = load_panel(_EXP / "panel.yaml")
SERIES = load_series(_EXP / "series-1.yaml")
PLAN = next(m for m in SERIES.meetings if m.kind is MeetingKind.PLAN)
_T0 = dt.datetime(2026, 10, 1, 9, 0, tzinfo=dt.UTC)


def _log_line(message: str, **fields: Any) -> str:
    return json.dumps({"timestamp": _T0.isoformat(), "message": message, **fields}) + "\n"


class _Handle:
    def __init__(self, name: str, world: _World) -> None:
        self.name = name
        self.world = world
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.world.signalled.append((self.name, sig))
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -signal.SIGKILL


class _World:
    """Fake processes, and an orchestrator whose discussion closes at once."""

    def __init__(self, *, rate_limit_off: bool = True, refuse_lease: bool = False) -> None:
        self.processes: dict[str, Process] = {}
        self.signalled: list[tuple[str, int]] = []
        self.posts: list[tuple[str, str, str, tuple[str, ...]]] = []
        self.rate_limit_off = rate_limit_off
        self.refuse_lease = refuse_lease
        self.seconds = 0.0
        self._messages: list[Message] = []

    def now(self) -> dt.datetime:
        return _T0 + dt.timedelta(seconds=self.seconds)

    async def sleep(self, seconds: float) -> None:
        self.seconds += seconds

    def spawn(self, process: Process, environ: Mapping[str, str]) -> _Handle:
        self.processes[process.name] = process
        if process.name == "orchestrator" and self.rate_limit_off:
            self._log(_log_line("security.rate_limit.disabled scope=startup"))
        return _Handle(process.name, self)

    def _log(self, line: str) -> None:
        log = self.processes["orchestrator"].log
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as f:
            f.write(line)

    async def healthy(self) -> bool:
        return True

    async def agents(self) -> set[str]:
        return {name for name in self.processes if name != "orchestrator"}

    async def post(
        self, channel: str, sender: str, content: str, *, mentions: Sequence[str] = (),
    ) -> Message:
        self.posts.append((channel, sender, content, tuple(mentions)))
        message = Message(id=f"m-{len(self._messages)}", sender=sender, content=content,
                          at=self.now(), mentions=tuple(mentions))
        self._messages.append(message)
        if len(self.posts) == 1:  # the discussion closes at once
            self._log(_log_line(
                "channels: interaction closed by RFC 0052 bounded close", channel_id=channel,
                interaction_id="i-1", trigger="structural",
            ))
            if self.refuse_lease:
                self._log(_log_line(
                    "wallet: lease denied — budget exceeded", agent_id="velvet-pika",
                ))
        else:
            self._messages.append(Message(
                id="memo", sender="lunar-stoat", content="## Recommendation\nOption B.",
                at=self.now(),
            ))
        return message

    async def messages(self, channel: str) -> list[Message]:
        return list(self._messages)

    async def disarm(self, channel: str) -> None:
        pass

    async def set_respond(self, channel: str, member: str, respond: str) -> None:
        pass


async def _run(world: _World, directory: Path, arm: str = "C") -> Any:
    return await run_meeting(
        PANEL, arm, SERIES, PLAN, attempt=2, directory=directory,
        binary=Path("/repo/bin/persatrix-server"), python=Path("/venv/bin/python"),
        repo=Path("/repo"), spawn=world.spawn, room=world, now=world.now, sleep=world.sleep,
    )


class TestRunMeeting:
    async def test_the_meeting_runs_on_a_deployment_of_its_own(self, tmp_path: Path) -> None:
        world = _World()
        result = await _run(world, tmp_path)
        channels = yaml.safe_load((tmp_path / "deployment/config/channels.yaml").read_text())
        assert channels["channels"] == [
            channel_config(PANEL, "C", name="advice-2", organisation=SERIES.organisation),
        ]
        assert world.posts[0] == ("group:advice-2", "operator", PLAN.message, ())
        assert result.memo is not None and result.memo.content == "## Recommendation\nOption B."
        assert sorted(world.signalled) == sorted(
            (name, signal.SIGTERM) for name in world.processes
        )

    async def test_every_adviser_gets_the_meetings_clock_and_call_log(
        self, tmp_path: Path,
    ) -> None:
        world = _World()
        await _run(world, tmp_path)
        envs = [world.processes[a.id].env for a in PANEL.advisers]
        assert all(env == envs[0] for env in envs)
        env = envs[0]
        assert (env[CLOCK_START_ENV], env[CLOCK_ANCHOR_ENV]) == (
            f"{PLAN.story_date.isoformat()}T10:00:00+00:00", _T0.isoformat(),
        )
        assert env[CALL_LOG_ENV] == str(tmp_path / "calls.jsonl")
        assert json.loads(env[CALL_TAGS_ENV]) == {
            "arm": "C", "series": "series-1", "meeting": PLAN.id, "meeting_kind": "plan",
            "attempt": "2",
        }

    async def test_the_meeting_is_recorded_beside_its_deployment(self, tmp_path: Path) -> None:
        await _run(_World(), tmp_path)
        record = json.loads((tmp_path / "meeting.json").read_text())
        assert (record["arm"], record["meeting"], record["trigger"]) == ("C", PLAN.id, "structural")
        assert record["memo"]["content"] == "## Recommendation\nOption B."
        senders = [m["sender"] for m in record["transcript"]]
        assert senders == ["operator", "operator", "lunar-stoat"]

    async def test_a_rate_limiter_left_on_stops_the_meeting_before_the_operator_speaks(
        self, tmp_path: Path,
    ) -> None:
        world = _World(rate_limit_off=False)
        with pytest.raises(DeploymentError, match="rate limiter is on"):
            await _run(world, tmp_path)
        assert world.posts == []
        assert {name for name, _ in world.signalled} == set(world.processes)

    async def test_a_lease_the_wallet_refused_fails_the_meeting_once_it_is_stopped(
        self, tmp_path: Path,
    ) -> None:
        world = _World(refuse_lease=True)
        with pytest.raises(DeploymentError, match="check 6.*velvet-pika: wallet: lease denied"):
            await _run(world, tmp_path)
        assert {name for name, _ in world.signalled} == set(world.processes)
        assert (tmp_path / "meeting.json").exists()

    async def test_arms_d_and_d_prime_are_not_held_this_way(self, tmp_path: Path) -> None:
        for arm in ("A", "D", "D-prime"):
            with pytest.raises(ValueError, match=f"arm {arm}"):
                await _run(_World(), tmp_path, arm=arm)
