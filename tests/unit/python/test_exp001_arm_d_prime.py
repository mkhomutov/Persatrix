"""EXP-001 harness — arm D′: the earlier meetings' transcripts in a cached prefix (PR 5d).

Arm D′ meets as arm C does, on a new deployment with empty stores for every
meeting, and with no memory. What carries from one meeting to the next is
text: every adviser's turn carries the full transcripts of the series'
earlier meetings, oldest first, marked for the provider's cache
(pre-registration §2). Check 3 asks that the first call of a meeting that
carries the prefix writes it to the cache, that every later call of the
meeting reads it, and that no other arm sets a cache breakpoint. A try held
again waits until the cache entry of the try before it has gone, so its
first call writes the prefix again rather than reading what a discarded try
paid for.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from agents.prompt_prefix import PROMPT_PREFIX_ENV
from evaluators.exp001 import deployed_meeting
from evaluators.exp001.arm_d_prime import (
    ARM,
    RETRY_GAP,
    arm_d_prime_hold,
    check_cache,
    prefix_sha256,
    transcript_prefix,
)
from evaluators.exp001.attempts import RETRIES, run_series
from evaluators.exp001.channel_arm import ChannelMeeting
from evaluators.exp001.costs import CallPurpose, CallRecord
from evaluators.exp001.deployed_meeting import CALL_LOG, RECORD
from evaluators.exp001.deployment import StartError, channel_config
from evaluators.exp001.materials import Meeting, MeetingKind, load_series
from evaluators.exp001.orchestrator import Message, OrchestratorError
from evaluators.exp001.panel import load_panel
from evaluators.exp001.processes import Process

_EXP = Path(__file__).resolve().parents[3] / "evaluators" / "experiments" / "EXP-001"
PANEL = load_panel(_EXP / "panel.yaml")
SERIES = load_series(_EXP / "practice.yaml")
IDS = [m.id for m in SERIES.meetings]
_T0 = dt.datetime(2026, 10, 1, 9, 0, tzinfo=dt.UTC)


def _message(n: int, sender: str, content: str) -> Message:
    return Message(id=f"m-{n}", sender=sender, content=content, at=_T0)


class TestTranscriptPrefix:
    def test_the_first_meeting_carries_none(self) -> None:
        assert transcript_prefix([]) == ""

    def test_every_message_of_every_earlier_meeting_oldest_first(self) -> None:
        briefing = [
            _message(0, "operator", "Today is Monday 6 October 2036.\n\nWe run a ferry."),
            _message(1, "velvet-pika", "Noted: two boats."),
        ]
        plan = [
            _message(0, "operator", "Today is Monday 13 October 2036. A plan."),
            _message(1, "lunar-stoat", "Option B.\n\nIt keeps the lease."),
            _message(2, "operator", "Please write the memo."),
            _message(3, "lunar-stoat", "Recommendation\nOption B."),
        ]
        assert transcript_prefix([("advice-1", briefing), ("advice-2", plan)]) == (
            "Transcripts of your earlier meetings with the same members, oldest first. "
            "Each holds every message posted in that meeting's channel, in the order "
            "they were posted.\n"
            "\n"
            "Meeting in #advice-1\n"
            "\n"
            "[operator]: Today is Monday 6 October 2036.\n\nWe run a ferry.\n"
            "\n"
            "[velvet-pika]: Noted: two boats.\n"
            "\n"
            "Meeting in #advice-2\n"
            "\n"
            "[operator]: Today is Monday 13 October 2036. A plan.\n"
            "\n"
            "[lunar-stoat]: Option B.\n\nIt keeps the lease.\n"
            "\n"
            "[operator]: Please write the memo.\n"
            "\n"
            "[lunar-stoat]: Recommendation\nOption B.\n"
        )

    def test_its_digest_is_what_the_runtime_logs(self) -> None:
        text = "Meeting in #advice-1\r\n"
        assert prefix_sha256(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()


_Script = Exception | Sequence[str] | None  # raise; provider errors to log; or answer


class _Meetings:
    """Stands in for ``deployed_meeting.run_meeting``. Each try takes ten
    minutes on the shared clock, leaves a transcript of its own and, as
    scripted, logs failed turns or raises."""

    def __init__(self, scripts: dict[tuple[str, int, int], _Script] | None = None) -> None:
        self.scripts = scripts or {}
        self.seconds = 0.0
        self.calls: list[dict[str, Any]] = []

    def now(self) -> dt.datetime:
        return _T0 + dt.timedelta(seconds=self.seconds)

    async def sleep(self, seconds: float) -> None:
        self.seconds += seconds

    async def __call__(
        self, panel: Any, arm: str, series: Any, meeting: Meeting, *, attempt: int,
        meeting_try: int, directory: Path, ended: Any, prefix: str, binary: Path,
        **options: Any,
    ) -> ChannelMeeting:
        self.calls.append({
            "arm": arm, "meeting": meeting.id, "attempt": attempt, "try": meeting_try,
            "prefix": prefix, "began": self.now(), "options": options,
        })
        directory.mkdir(parents=True)
        script = self.scripts.get((meeting.id, attempt, meeting_try))
        if isinstance(script, Exception):
            if isinstance(script, OrchestratorError):  # the orchestrator exited
                (directory / RECORD).write_text(json.dumps({"exited": {"orchestrator": 2}}))
            raise script
        self.seconds += 600
        tags = {"arm": arm, "series": series.id, "meeting": meeting.id,
                "meeting_kind": meeting.kind.value, "attempt": str(attempt),
                "try": str(meeting_try)}
        with (directory / CALL_LOG).open("a") as log:
            for error in script or ():
                log.write(json.dumps({
                    "tags": tags, "agent_id": "ripple-kite", "purpose": "turn",
                    "provider": "anthropic", "model": "claude-sonnet-4-6", "model_alias": None,
                    "started_at": self.now().isoformat(), "input_tokens": 0,
                    "output_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0,
                    "error": error,
                }) + "\n")
        ended(self.now())
        return _held(meeting, attempt, meeting_try, directory)


def _transcript(meeting: Meeting, attempt: int, meeting_try: int) -> tuple[Message, ...]:
    return (
        _message(0, PANEL.operator, meeting.message),
        _message(1, "ripple-kite", f"{meeting.id}, attempt {attempt}, try {meeting_try}"),
    )


def _held(meeting: Meeting, attempt: int, meeting_try: int, directory: Path) -> ChannelMeeting:
    transcript = _transcript(meeting, attempt, meeting_try)
    return ChannelMeeting(
        arm=ARM, series=SERIES.id, meeting=meeting.id, attempt=attempt, meeting_try=meeting_try,
        channel=f"group:advice-{IDS.index(meeting.id) + 1}", opened=transcript[0],
        closed_at=_T0, trigger="end_votes", closed_by="vote", memo_turn=None, energy={},
        memo=None, transcript=transcript, failures=(),
    )


def _expected(*meetings: tuple[Meeting, int, int]) -> str:
    return transcript_prefix([
        (f"advice-{IDS.index(m.id) + 1}", _transcript(m, attempt, meeting_try))
        for m, attempt, meeting_try in meetings
    ])


async def _hold_series(tmp_path: Path, meetings: _Meetings) -> Any:
    hold = arm_d_prime_hold(
        PANEL, SERIES, tmp_path, binary=Path("/repo/bin/persatrix-server"),
        now=meetings.now, sleep=meetings.sleep, run=meetings,
    )
    return await run_series(ARM, SERIES, hold, sleep=meetings.sleep)


def _prefixes(meetings: _Meetings) -> dict[tuple[str, int, int], str]:
    return {(c["meeting"], c["attempt"], c["try"]): c["prefix"] for c in meetings.calls}


class TestTheHold:
    async def test_each_meeting_carries_the_meetings_before_it(self, tmp_path: Path) -> None:
        meetings = _Meetings()
        run = await _hold_series(tmp_path, meetings)
        prefixes = _prefixes(meetings)
        assert prefixes[(IDS[0], 1, 1)] == ""
        for n, meeting in enumerate(SERIES.meetings[1:], start=1):
            earlier = [(m, 1, 1) for m in SERIES.meetings[:n]]
            assert prefixes[(meeting.id, 1, 1)] == _expected(*earlier)
        assert run.finished_attempt == 1
        assert {c["arm"] for c in meetings.calls} == {ARM}

    async def test_the_hold_passes_its_binary_and_options_through(self, tmp_path: Path) -> None:
        meetings = _Meetings()
        hold = arm_d_prime_hold(
            PANEL, SERIES, tmp_path, binary=Path("/repo/bin/persatrix-server"),
            now=meetings.now, sleep=meetings.sleep, run=meetings, python=Path("/venv/py"),
        )
        await hold(SERIES.meetings[0], 1, 1)
        options = meetings.calls[0]["options"]
        assert (options["python"], options["now"], options["sleep"]) == (
            Path("/venv/py"), meetings.now, meetings.sleep,
        )

    async def test_a_try_cut_short_is_never_carried(self, tmp_path: Path) -> None:
        meetings = _Meetings({(IDS[1], 1, 1): ["RateLimitError"]})
        await _hold_series(tmp_path, meetings)
        assert _prefixes(meetings)[(IDS[2], 1, 1)] == _expected(
            (SERIES.meetings[0], 1, 1), (SERIES.meetings[1], 1, 2),
        )

    async def test_a_try_held_again_waits_until_the_cache_entry_has_gone(
        self, tmp_path: Path,
    ) -> None:
        """A cache entry lives five minutes after the last call that read or
        wrote it began; the harness's own wait before a second try is one
        minute, so D′ waits longer."""
        meetings = _Meetings({(IDS[1], 1, 1): ["RateLimitError"]})
        await _hold_series(tmp_path, meetings)
        first, second = (c for c in meetings.calls if c["meeting"] == IDS[1])
        stopped = first["began"] + dt.timedelta(seconds=600)
        assert second["began"] == stopped + RETRY_GAP
        assert RETRY_GAP > dt.timedelta(minutes=5)

    async def test_a_start_that_failed_adds_no_wait(self, tmp_path: Path) -> None:
        """No call was made, so no cache entry can be left behind."""
        meetings = _Meetings({(IDS[1], 1, 1): StartError("ripple-kite exited (1)")})
        await _hold_series(tmp_path, meetings)
        first, second = (c for c in meetings.calls if c["meeting"] == IDS[1])
        assert second["began"] - first["began"] == dt.timedelta(minutes=1)

    async def test_a_series_started_again_carries_only_its_own_attempt(
        self, tmp_path: Path,
    ) -> None:
        meetings = _Meetings({(IDS[1], 1, t): ["RateLimitError"] for t in range(1, RETRIES + 2)})
        run = await _hold_series(tmp_path, meetings)
        assert run.finished_attempt == 2
        assert _prefixes(meetings)[(IDS[1], 2, 1)] == _expected((SERIES.meetings[0], 2, 1))

    async def test_a_meeting_that_left_no_transcript_is_left_out(self, tmp_path: Path) -> None:
        """An orchestrator that exits mid-meeting is recorded, not retried,
        and the harness could read none of the meeting's messages."""
        meetings = _Meetings({(IDS[1], 1, 1): OrchestratorError("GET …/messages: refused")})
        await _hold_series(tmp_path, meetings)
        assert _prefixes(meetings)[(IDS[2], 1, 1)] == _expected((SERIES.meetings[0], 1, 1))

    async def test_a_discarded_try_never_stands_in_for_the_one_that_finished(
        self, tmp_path: Path,
    ) -> None:
        meetings = _Meetings({
            (IDS[1], 1, 1): ["RateLimitError"],
            (IDS[1], 1, 2): OrchestratorError("GET …/messages: refused"),
        })
        await _hold_series(tmp_path, meetings)
        assert _prefixes(meetings)[(IDS[2], 1, 1)] == _expected((SERIES.meetings[0], 1, 1))


class _Handle:
    def __init__(self) -> None:
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


class _World:
    """Stand-in processes, and an orchestrator whose discussion closes at once
    and whose chair answers the memo request."""

    def __init__(self) -> None:
        self.processes: dict[str, Process] = {}
        self.messages_: list[Message] = []

    def now(self) -> dt.datetime:
        return _T0

    async def sleep(self, seconds: float) -> None:
        pass

    def spawn(self, process: Process, environ: Any) -> _Handle:
        self.processes[process.name] = process
        if process.name == "orchestrator":
            process.log.parent.mkdir(parents=True, exist_ok=True)
            process.log.write_text("".join(json.dumps({"timestamp": _T0.isoformat(),
                                                       "message": m}) + "\n" for m in (
                "security.rate_limit.disabled scope=startup",
                "wallet lease enforcement initialized",
                "gRPC server listening",
            )))
        return _Handle()

    async def healthy(self) -> bool:
        return True

    async def agents(self) -> set[str]:
        return {name for name in self.processes if name != "orchestrator"}

    async def post(
        self, channel: str, sender: str, content: str, *, mentions: Sequence[str] = (),
    ) -> Message:
        message = _message(len(self.messages_), sender, content)
        self.messages_.append(message)
        if len(self.messages_) == 1:
            with self.processes["orchestrator"].log.open("a") as log:
                log.write(json.dumps({
                    "timestamp": _T0.isoformat(),
                    "message": "channels: interaction closed by end-of-interaction votes",
                    "channel_id": channel, "interaction_id": "i-1", "trigger": "end_votes",
                }) + "\n")
        else:
            self.messages_.append(_message(len(self.messages_), PANEL.chair.id, "Memo."))
        return message

    async def messages(self, channel: str) -> list[Message]:
        return list(self.messages_)

    async def activity(self, channel: str) -> set[str]:
        return set()

    async def disarm(self, channel: str) -> None:
        pass

    async def set_respond(self, channel: str, member: str, respond: str) -> None:
        pass


async def _deploy(world: _World, directory: Path, arm: str = ARM, prefix: str = "") -> Any:
    return await deployed_meeting.run_meeting(
        PANEL, arm, SERIES, SERIES.meetings[1], attempt=1, meeting_try=1, directory=directory,
        binary=Path("/repo/bin/persatrix-server"), python=Path("/venv/bin/python"),
        repo=Path("/repo"), spawn=world.spawn, room=world, now=world.now, sleep=world.sleep,
        prefix=prefix,
    )


class TestTheDeployment:
    async def test_every_adviser_carries_the_prefix_written_beside_the_try(
        self, tmp_path: Path,
    ) -> None:
        world = _World()
        text = "Transcripts…\r\nMeeting in #advice-1\n"
        await _deploy(world, tmp_path, prefix=text)
        written = tmp_path / deployed_meeting.PREFIX
        assert written.read_bytes() == text.encode("utf-8")
        for adviser in PANEL.advisers:
            assert world.processes[adviser.id].env[PROMPT_PREFIX_ENV] == str(written)
        assert PROMPT_PREFIX_ENV not in world.processes["orchestrator"].env

    async def test_a_meeting_with_nothing_before_it_gives_no_adviser_the_setting(
        self, tmp_path: Path,
    ) -> None:
        world = _World()
        await _deploy(world, tmp_path)
        assert not (tmp_path / deployed_meeting.PREFIX).exists()
        assert all(PROMPT_PREFIX_ENV not in p.env for p in world.processes.values())

    @pytest.mark.parametrize("arm", ["B", "C"])
    async def test_no_other_arm_is_given_a_prefix(self, tmp_path: Path, arm: str) -> None:
        with pytest.raises(ValueError, match="only arm D-prime"):
            await _deploy(_World(), tmp_path, arm=arm, prefix="Meeting in #advice-1\n")

    async def test_d_prime_meets_as_c_does_with_no_memory(self, tmp_path: Path) -> None:
        """Governance as shipped, memory off (pre-registration §2), and the
        shipped floor control, which lets one adviser speak at a time, so a
        meeting's first turn is under way before the next one starts."""
        await _deploy(_World(), tmp_path)
        config = tmp_path / "deployment" / "config"
        [channel] = yaml.safe_load((config / "channels.yaml").read_text())["channels"]
        assert channel == channel_config(
            PANEL, "C", name="advice-2", organisation=SERIES.organisation,
        )
        assert "floor_control" not in channel
        optimization = yaml.safe_load((config / "optimization.yaml").read_text())
        assert optimization["memory_budget"] == {"tokens": 0}


def _record(
    *, arm: str = ARM, meeting: str = "plan-1", meeting_try: int = 1, adviser: str = "ripple-kite",
    purpose: CallPurpose = CallPurpose.REPLY, second: int = 0, prefix: str | None = "a" * 64,
    write: int = 0, read: int = 0,
) -> CallRecord:
    return CallRecord(
        arm=arm, series="series-1", meeting=meeting, meeting_kind=MeetingKind.PLAN, attempt=1,
        adviser=adviser, purpose=purpose, started_at=_T0 + dt.timedelta(seconds=second),
        model="claude-sonnet-4-6", input_tokens=100, output_tokens=20,
        cache_write_tokens=write, cache_read_tokens=read, meeting_try=meeting_try,
        cache_prefix=prefix,
    )


def _meeting_that_holds() -> list[CallRecord]:
    """A D′ meeting as check 3 wants it: bids carry nothing, the first turn
    writes the prefix, and every later turn, the memo's included, reads it."""
    return [
        _record(purpose=CallPurpose.BID, prefix=None, second=0),
        _record(adviser="lunar-stoat", second=5, write=12_000),
        _record(purpose=CallPurpose.BID, adviser="velvet-pika", prefix=None, second=40),
        _record(adviser="velvet-pika", second=45, read=12_000),
        _record(adviser="lunar-stoat", purpose=CallPurpose.MEMO, second=200, read=12_000),
    ]


class TestCheckCache:
    def test_a_meeting_that_holds_check_3_has_no_findings(self) -> None:
        assert check_cache(_meeting_that_holds()) == []

    def test_the_calls_are_taken_in_the_order_they_began(self) -> None:
        assert check_cache(list(reversed(_meeting_that_holds()))) == []

    def test_a_first_call_that_wrote_nothing_is_a_finding(self) -> None:
        """A prefix under the model's minimum is cached silently not at all,
        so the call after it has nothing to read either."""
        first, after = check_cache([_record(second=0), _record(second=30)])
        assert first.startswith(f"{ARM}, series-1, plan-1, attempt 1, try 1: ")
        assert "the first call that carried the prefix wrote 0 tokens to the cache" in first
        assert "wrote 0 and read 0 tokens of the cache, 30 s after" in after

    def test_a_first_call_that_read_an_entry_already_there_is_a_finding(self) -> None:
        [finding] = check_cache([_record(write=12_000, read=12_000)])
        assert "read 12000" in finding

    def test_a_later_call_that_wrote_the_prefix_again_names_the_gap(self) -> None:
        """An entry lives five minutes: a discussion quiet for its whole
        600-second idle window outlasts it, and the memo turn writes again."""
        records = [
            _record(second=0, write=12_000),
            _record(adviser="lunar-stoat", purpose=CallPurpose.MEMO, second=700, write=12_000),
        ]
        [finding] = check_cache(records)
        assert "lunar-stoat's memo call" in finding
        assert "wrote 12000 and read 0" in finding
        assert "700 s after" in finding

    def test_a_later_call_that_read_and_wrote_is_a_finding(self) -> None:
        """One breakpoint, at the end of the prefix: a call that reads it has
        nothing else to write."""
        [finding] = check_cache([
            _record(second=0, write=12_000), _record(second=30, write=500, read=12_000),
        ])
        assert "wrote 500 and read 12000 tokens of the cache, 30 s after" in finding

    def test_calls_that_carried_different_prefixes_are_a_finding(self) -> None:
        records = [_record(write=12_000), _record(second=30, read=12_000, prefix="b" * 64)]
        assert any("2 different prefixes" in f for f in check_cache(records))

    def test_a_call_without_a_prefix_that_touched_the_cache_is_a_finding(self) -> None:
        [finding] = check_cache([_record(purpose=CallPurpose.BID, prefix=None, write=5)])
        assert "carried no prefix" in finding

    @pytest.mark.parametrize("arm", ["A", "B", "C", "D"])
    def test_no_other_arm_sets_a_cache_breakpoint(self, arm: str) -> None:
        assert check_cache([_record(arm=arm, prefix=None)]) == []
        assert check_cache([_record(arm=arm, prefix=None, read=40)]) != []
        [finding] = check_cache([_record(arm=arm)])
        assert finding.endswith(f"carried a prefix, which only arm {ARM}'s turns do")

    def test_each_try_is_judged_on_its_own(self) -> None:
        """A second try writes the prefix again, as the wait before it makes sure."""
        records = [
            _record(write=12_000), _record(second=30, read=12_000),
            _record(meeting_try=2, second=900, write=12_000),
        ]
        assert check_cache(records) == []

    def test_a_call_must_carry_the_prefix_the_harness_wrote(self) -> None:
        records = [_record(write=12_000), _record(second=30, read=12_000)]
        written = {(ARM, "series-1", "plan-1", 1, 1): "c" * 64}
        [finding] = check_cache(records, written=written)
        assert "not the prefix the harness wrote" in finding
        assert check_cache(records, written={(ARM, "series-1", "plan-1", 1, 1): "a" * 64}) == []

    @pytest.mark.parametrize("records", [[_record(prefix=None)], []])
    def test_a_try_given_a_prefix_whose_turns_carried_none_is_a_finding(
        self, records: list[CallRecord],
    ) -> None:
        """Whether its advisers made other calls or none at all."""
        written = {(ARM, "series-1", "plan-1", 1, 1): "a" * 64}
        [finding] = check_cache(records, written=written)
        assert finding == (
            f"{ARM}, series-1, plan-1, attempt 1, try 1: "
            "no call carried the prefix the harness wrote"
        )
