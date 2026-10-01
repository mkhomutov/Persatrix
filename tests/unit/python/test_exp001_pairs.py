"""EXP-001 harness — one arm's series held in a directory of its own (PR 6b).

The run holds each arm's series, a pair, in a directory of its own. Every
try is kept as it ends, arm A's replies included, which no other file
keeps; a pair held to the end is marked finished and read back rather than
held again; and a pair stopped partway, by Ctrl-C or a crash, is set aside
and held again from its briefing. The holds themselves are in
``test_exp001_attempts_holds.py`` and the arms' own tests.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agents.llm_client import LLMClient
from agents.llm_offline import MockProvider
from agents.llm_types import StopReason
from evaluators.exp001 import arm_d, arm_d_prime, attempts, deployed_meeting
from evaluators.exp001.attempts import HarnessFault, Held, Hold, SeriesRun
from evaluators.exp001.costs import CallPurpose
from evaluators.exp001.deployment import ARMS_ALIAS, Alias
from evaluators.exp001.materials import Meeting
from evaluators.exp001.pairs import (
    PAIR,
    TRIES,
    Kept,
    arm_hold,
    hold_pair,
    pair_calls,
    read_pair,
)

from ._exp001_run_test_helpers import IDS, PANEL, SERIES, T0, arm_a_reply, channel_meeting, no_wait

BRIEFING, PLAN, CONTROL, RECALL = SERIES.meetings
_BINARY = Path("/repo/bin/persatrix-server")


class _Script:
    """A hold that answers each try as scripted by (meeting, attempt, try): a
    Held, or an exception raised; by default arm C's meeting, as held."""

    def __init__(self, acts: dict[tuple[str, int, int], Held[Any] | BaseException] | None = None,
                 *, default: Callable[[Meeting, int, int], Held[Any]] | None = None) -> None:
        self.acts = acts or {}
        self.default = default or (lambda m, a, t: Held(channel_meeting(m, a, t)))
        self.held: list[tuple[str, int, int]] = []
        self.directories: list[Path] = []

    def make(self, directory: Path) -> Hold[Any]:
        self.directories.append(directory)

        async def hold(meeting: Meeting, attempt: int, meeting_try: int) -> Held[Any]:
            self.held.append((meeting.id, attempt, meeting_try))
            act = self.acts.get((meeting.id, attempt, meeting_try))
            if isinstance(act, BaseException):
                raise act
            return act if act is not None else self.default(meeting, attempt, meeting_try)

        return hold


async def _hold(tmp_path: Path, script: _Script, arm: str = "C") -> SeriesRun[Kept]:
    return await hold_pair(
        arm, SERIES, tmp_path / "pairs" / SERIES.id / arm, script.make,
        interrupted=tmp_path / "interrupted", sleep=no_wait,
    )


def _lines(directory: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (directory / TRIES).read_text().splitlines()]


class TestKeepingEachTry:
    async def test_each_try_is_kept_as_it_ends(self, tmp_path: Path) -> None:
        script = _Script({(PLAN.id, 1, 1): Held(None, errors=("RateLimitError",))})
        await _hold(tmp_path, script)
        kept = _lines(tmp_path / "pairs" / SERIES.id / "C")
        assert [(k["meeting"], k["attempt"], k["try"]) for k in kept] == [
            (IDS[0], 1, 1), (IDS[1], 1, 1), (IDS[1], 1, 2), (IDS[2], 1, 1), (IDS[3], 1, 1),
        ]
        assert (kept[1]["errors"], kept[1]["cut_short"], kept[1]["record"]) == (
            ["RateLimitError"], True, None,
        )
        assert (kept[2]["errors"], kept[2]["cut_short"]) == ([], False)
        assert {(k["arm"], k["series"]) for k in kept} == {("C", SERIES.id)}

    async def test_the_pair_is_marked_finished_with_the_attempt_that_finished(
        self, tmp_path: Path,
    ) -> None:
        await _hold(tmp_path, _Script())
        marked = json.loads((tmp_path / "pairs" / SERIES.id / "C" / PAIR).read_text())
        assert marked == {"arm": "C", "series": SERIES.id, "finished_attempt": 1}

    async def test_the_answer_is_the_memo_or_the_recall_checks_answers(
        self, tmp_path: Path,
    ) -> None:
        script = _Script(
            default=lambda m, a, t: Held(channel_meeting(m, a, t, memo=f"Answer {m.id}")),
        )
        run = await _hold(tmp_path, script)
        answers = {m: t.held.result.answer for m, t in run.finished().items() if t.held.result}
        assert answers == {
            BRIEFING.id: None, PLAN.id: f"Answer {PLAN.id}", CONTROL.id: f"Answer {CONTROL.id}",
            RECALL.id: f"Answer {RECALL.id}",
        }

    async def test_a_missing_memo_is_kept_as_no_answer(self, tmp_path: Path) -> None:
        script = _Script({(PLAN.id, 1, 1): Held(channel_meeting(PLAN, 1, 1, memo=None,
                                                          failures=("missing_memo",)))})
        run = await _hold(tmp_path, script)
        kept = run.finished()[PLAN.id].held.result
        assert kept is not None and kept.answer is None
        assert kept.record is not None and kept.record["failures"] == ["missing_memo"]

    @pytest.mark.parametrize(("text", "stop", "answer"), [
        ("The memo.", StopReason.END_TURN, "The memo."),
        ("The memo, cut", StopReason.MAX_TOKENS, None),
        ("  \n", StopReason.END_TURN, None),
    ])
    async def test_arm_as_reply_is_its_answer_unless_it_counts_as_missing(
        self, tmp_path: Path, text: str, stop: StopReason, answer: str | None,
    ) -> None:
        script = _Script(default=lambda m, a, t: Held(arm_a_reply(m, text, stop)))
        run = await _hold(tmp_path, script, arm="A")
        kept = run.finished()[PLAN.id].held.result
        assert kept is not None and kept.answer == answer

    async def test_a_briefing_asks_for_no_answer_in_any_arm(self, tmp_path: Path) -> None:
        """Arm A replies to its briefing too; the reply is kept, but it answers nothing."""
        script = _Script(default=lambda m, a, t: Held(arm_a_reply(m, "Noted.")))
        run = await _hold(tmp_path, script, arm="A")
        kept = run.finished()[BRIEFING.id].held.result
        assert kept is not None and (kept.answer, kept.record["text"]) == (None, "Noted.")

    async def test_arm_as_replies_are_kept_whole(self, tmp_path: Path) -> None:
        """Arm A's reply is kept nowhere else: its text, stop reason and times."""
        script = _Script(default=lambda m, a, t: Held(arm_a_reply(m, f"Reply to {m.id}")))
        await _hold(tmp_path, script, arm="A")
        record = _lines(tmp_path / "pairs" / SERIES.id / "A")[1]["record"]
        assert record == {
            "series": SERIES.id, "meeting": PLAN.id, "text": f"Reply to {PLAN.id}",
            "stop_reason": "end_turn", "asked_at": T0.isoformat(),
            "answered_at": (T0 + dt.timedelta(seconds=20)).isoformat(),
        }

    async def test_a_try_that_did_not_start_is_kept_with_why(self, tmp_path: Path) -> None:
        refused: Held[Any] = Held(
            None, start_failed=True, start_error=RuntimeError("no registration"),
        )
        await _hold(tmp_path, _Script({(BRIEFING.id, 1, 1): refused}))
        first = _lines(tmp_path / "pairs" / SERIES.id / "C")[0]
        assert (first["start_failed"], first["start_error"], first["cut_short"]) == (
            True, "RuntimeError: no registration", True,
        )

    async def test_a_dropped_series_is_kept_as_dropped(self, tmp_path: Path) -> None:
        script = _Script(default=lambda m, a, t: (
            Held(None, errors=("RateLimitError",)) if m == PLAN else Held(channel_meeting(m, a, t))
        ))
        run = await _hold(tmp_path, script)
        assert run.dropped and run.finished() == {}
        marked = json.loads((tmp_path / "pairs" / SERIES.id / "C" / PAIR).read_text())
        assert marked["finished_attempt"] is None

    async def test_the_pair_read_back_is_the_pair_held(self, tmp_path: Path) -> None:
        script = _Script({(PLAN.id, 1, 1): Held(None, errors=("RateLimitError",))})
        run = await _hold(tmp_path, script)
        assert read_pair(tmp_path / "pairs" / SERIES.id / "C") == run
        assert [(t.meeting, t.attempt, t.meeting_try) for t in run.tries] == [
            (IDS[0], 1, 1), (IDS[1], 1, 1), (IDS[1], 1, 2), (IDS[2], 1, 1), (IDS[3], 1, 1),
        ]
        assert [(t.held.errors, t.held.cut_short) for t in run.tries] == [
            ((), False), (("RateLimitError",), True), ((), False), ((), False), ((), False),
        ]

    async def test_a_harness_fault_leaves_the_tries_held_before_it(self, tmp_path: Path) -> None:
        script = _Script({(PLAN.id, 1, 1): HarnessFault("C, practice: the call log is unreadable")})
        with pytest.raises(HarnessFault):
            await _hold(tmp_path, script)
        directory = tmp_path / "pairs" / SERIES.id / "C"
        assert [k["meeting"] for k in _lines(directory)] == [BRIEFING.id]
        assert not (directory / PAIR).exists()
        assert read_pair(directory) is None


class TestResuming:
    async def test_a_pair_held_before_is_read_back_not_held_again(self, tmp_path: Path) -> None:
        first = await _hold(tmp_path, _Script())
        again = _Script()
        assert await _hold(tmp_path, again) == first
        assert (again.held, again.directories) == ([], [])

    async def test_a_pair_stopped_partway_is_set_aside_and_held_again_from_its_briefing(
        self, tmp_path: Path,
    ) -> None:
        with pytest.raises(asyncio.CancelledError):
            await _hold(tmp_path, _Script({(CONTROL.id, 1, 1): asyncio.CancelledError()}))
        again = _Script()
        run = await _hold(tmp_path, again)
        aside = tmp_path / "interrupted" / f"{SERIES.id}-C-1"
        assert [k["meeting"] for k in _lines(aside)] == [BRIEFING.id, PLAN.id]
        assert again.held == [(m, 1, 1) for m in IDS]
        assert again.directories == [tmp_path / "pairs" / SERIES.id / "C"]
        assert run.finished_attempt == 1

    async def test_an_empty_directory_is_held_in_as_it_is(self, tmp_path: Path) -> None:
        """Nothing was held there, so nothing is set aside."""
        (tmp_path / "pairs" / SERIES.id / "C").mkdir(parents=True)
        await _hold(tmp_path, _Script())
        assert not (tmp_path / "interrupted").exists()

    async def test_each_stop_is_set_aside_apart(self, tmp_path: Path) -> None:
        for _ in range(2):
            with pytest.raises(KeyboardInterrupt):
                await _hold(tmp_path, _Script({(PLAN.id, 1, 1): KeyboardInterrupt()}))
        await _hold(tmp_path, _Script())
        assert sorted(p.name for p in (tmp_path / "interrupted").iterdir()) == [
            f"{SERIES.id}-C-1", f"{SERIES.id}-C-2",
        ]


class TestThePairsCalls:
    def _log(self, path: Path, *lines: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    def _line(self, meeting: Meeting, meeting_try: int, agent: str, at: dt.datetime,
              **overrides: Any) -> dict[str, Any]:
        line = {
            "tags": {"arm": "C", "series": SERIES.id, "meeting": meeting.id,
                     "meeting_kind": meeting.kind.value, "attempt": "1",
                     "try": str(meeting_try)},
            "agent_id": agent, "purpose": "turn", "provider": "anthropic",
            "model": "claude-sonnet-4-6", "model_alias": "quality",
            "started_at": at.isoformat(), "input_tokens": 100, "output_tokens": 10,
            "cache_write_tokens": 0, "cache_read_tokens": 0, "cache_prefix_sha256": None,
            "error": None,
        }
        line.update(overrides)
        return line

    async def test_every_call_log_of_the_pair_is_read_with_its_memo_turns(
        self, tmp_path: Path,
    ) -> None:
        script = _Script({(PLAN.id, 1, 1): Held(None, errors=("RateLimitError",))})
        run = await _hold(tmp_path, script)
        directory = tmp_path / "pairs" / SERIES.id / "C"
        chair = PANEL.chair.id
        self._log(attempts.try_directory(directory, 1, PLAN, 1) / "calls.jsonl",
                  self._line(PLAN, 1, chair, T0, error="RateLimitError", input_tokens=0,
                             output_tokens=0))
        self._log(attempts.try_directory(directory, 1, PLAN, 2) / "calls.jsonl",
                  self._line(PLAN, 2, chair, T0 + dt.timedelta(minutes=1)),
                  self._line(PLAN, 2, chair, T0 + dt.timedelta(minutes=6)))
        calls = pair_calls(directory, run)
        assert [f.error for f in calls.failures] == ["RateLimitError"]
        assert [(r.meeting_try, r.purpose) for r in calls.records] == [
            (2, CallPurpose.REPLY), (2, CallPurpose.MEMO),
        ]

    async def test_arm_as_one_log_is_read(self, tmp_path: Path) -> None:
        script = _Script(default=lambda m, a, t: Held(arm_a_reply(m, "Noted.")))
        run = await _hold(tmp_path, script, arm="A")
        directory = tmp_path / "pairs" / SERIES.id / "A"
        line = self._line(PLAN, 1, "", T0)
        line["tags"]["arm"] = "A"
        self._log(directory / "calls.jsonl", line)
        assert [(r.arm, r.adviser) for r in pair_calls(directory, run).records] == [("A", None)]


class TestEachArmsHold:
    @pytest.fixture
    def built(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        """Each arm's hold, as the arm's own module builds it, recorded instead."""
        seen: dict[str, Any] = {}

        def recorder(name: str) -> Callable[..., str]:
            def build(*args: Any, **kwargs: Any) -> str:
                seen[name] = (args, kwargs)
                return name
            return build

        monkeypatch.setattr(attempts, "arm_a_hold", recorder("A"))
        monkeypatch.setattr(attempts, "channel_hold", recorder("channel"))
        monkeypatch.setattr(arm_d, "arm_d_hold", recorder("D"))
        monkeypatch.setattr(arm_d_prime, "arm_d_prime_hold", recorder("D-prime"))
        return seen

    def test_arm_a_logs_each_series_to_a_new_file_in_its_directory(
        self, built: dict[str, Any], tmp_path: Path,
    ) -> None:
        """Its call names the aliases' model: offline, the mock's, which the report never prices."""
        client = LLMClient(MockProvider())
        mock = Alias("mock", "offline", 0, 0)
        assert arm_hold("A", PANEL, SERIES, tmp_path, client=client, binary=_BINARY,
                        alias=mock) == "A"
        assert built["A"] == (
            (client, PANEL, SERIES), {"log_path": tmp_path / "calls.jsonl", "model": "offline"},
        )

    @pytest.mark.parametrize("arm", ["B", "C"])
    def test_arms_b_and_c_hold_each_try_on_a_new_deployment(
        self, built: dict[str, Any], tmp_path: Path, arm: str,
    ) -> None:
        mock = Alias("mock", "offline", 0, 0)
        arm_hold(arm, PANEL, SERIES, tmp_path, client=LLMClient(MockProvider()),
                 binary=_BINARY, alias=mock)
        args, kwargs = built["channel"]
        assert args == (PANEL, arm, SERIES, tmp_path)
        run = kwargs["run"]
        assert isinstance(run, functools.partial)
        assert (run.func, run.args, run.keywords) == (
            deployed_meeting.run_meeting, (), {"binary": _BINARY, "alias": mock},
        )

    @pytest.mark.parametrize("arm", ["D", "D-prime"])
    def test_arms_d_and_d_prime_are_held_by_their_own_holds(
        self, built: dict[str, Any], tmp_path: Path, arm: str,
    ) -> None:
        arm_hold(arm, PANEL, SERIES, tmp_path, client=LLMClient(MockProvider()), binary=_BINARY)
        assert built[arm] == ((PANEL, SERIES, tmp_path), {"binary": _BINARY, "alias": ARMS_ALIAS})

    def test_an_arm_the_pre_registration_does_not_name_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="unknown arm 'E'"):
            arm_hold("E", PANEL, SERIES, tmp_path, client=LLMClient(MockProvider()), binary=_BINARY)
