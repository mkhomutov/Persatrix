"""EXP-001 harness — the practice run, start to finish (PR 6b).

The practice series runs first, as often as needed, until the eight checks
pass (pre-registration §3). A practice run holds it for each arm named, one
arm at a time; draws the packets the two people score together and the
judge scores in a batch of its own; and writes the report. Started again in
the same directory it goes on where it stopped. A harness fault while the
meetings are held closes the directory; one while judging does not, since a
fixed reader reads the kept answers again without a second pass.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from agents import call_log
from agents.call_log import prefix_sha256
from agents.llm_client import LLMClient, LLMResponse, Usage
from agents.llm_types import LLMToolResult
from evaluators.exp001.attempts import HarnessFault, Held, Hold, try_directory
from evaluators.exp001.costs import ARMS_MODEL
from evaluators.exp001.deployed_meeting import PREFIX
from evaluators.exp001.deployment import ARMS_ALIAS, Alias
from evaluators.exp001.judge import CALL_LOG, JudgeFault, read_judge_log
from evaluators.exp001.materials import Meeting, MeetingKind
from evaluators.exp001.packets import load_adviser_names
from evaluators.exp001.practice import (
    BATCH,
    INTERRUPTED,
    JUDGING,
    REPORT,
    RUN,
    SUMMARY,
    RefusedError,
    run_practice,
)
from evaluators.exp001.rating import packet_text, read_packets

from ._exp001_judge_test_helpers import _RECALL_JSON, PROMPTS, _memo_json
from ._exp001_run_test_helpers import (
    EXP,
    PANEL,
    SCORED,
    SERIES,
    arm_a_reply,
    channel_meeting,
    no_wait,
)

NAMES = load_adviser_names(EXP / "panel.yaml")
_BINARY = Path("/repo/bin/persatrix-server")


@pytest.fixture(autouse=True)
def _no_ambient_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(call_log.CALL_LOG_ENV, raising=False)
    call_log.reset_call_log()


class _Judge:
    """Answers as the judge would, readably, and keeps what it was asked."""

    name = "anthropic"
    supports_prompt_cache = True

    def __init__(self, *, readable: bool = True) -> None:
        self.asked: list[str] = []
        self.readable = readable

    async def create_message(self, **kwargs: Any) -> LLMResponse:
        packet = kwargs["messages"][0]["content"]
        self.asked.append(packet)
        if not self.readable:
            answer = "I would rather not score this."
        elif kwargs["system"] == PROMPTS.recall:
            answer = _RECALL_JSON
        else:
            answer = _memo_json(c2=None if "decision: none." in packet else 2)
        return LLMResponse(text=answer, usage=Usage(3000, 900))

    def format_tool_definitions(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return tools

    def append_tool_round(
        self, messages: list[Any], response: LLMResponse, tool_results: list[LLMToolResult],
    ) -> list[Any]:
        raise AssertionError("the judge sends no tools")


class _Holds:
    """Stands in for each arm's hold: every meeting held at its first try,
    arm A replying and the channel arms' chair writing the memo; a fault
    raised at (arm, meeting) when named. With *prefixed*, each D′ meeting
    after the briefing keeps the prefix written for it, and its one turn
    carries another. The meeting *paused* at, (arm, meeting), waits until
    ``go_on`` is set."""

    def __init__(
        self, fault: tuple[str, str] | None = None, *, prefixed: bool = False,
        paused: tuple[str, str] | None = None,
    ) -> None:
        self.fault = fault
        self.prefixed = prefixed
        self.paused = paused
        self.go_on = asyncio.Event()
        self.held: list[tuple[str, str]] = []

    def __call__(self, arm: str, directory: Path) -> Hold[Any]:
        async def hold(meeting: Meeting, attempt: int, meeting_try: int) -> Held[Any]:
            if (arm, meeting.id) == self.fault:
                raise HarnessFault(f"{arm}, {SERIES.id}, {meeting.id}: the call log is unreadable")
            if (arm, meeting.id) == self.paused:
                await self.go_on.wait()
            self.held.append((arm, meeting.id))
            if arm == "A":
                return Held(arm_a_reply(meeting, f"Arm A at {meeting.id}."))
            if self.prefixed and meeting.kind is not MeetingKind.BRIEFING:
                _carry(try_directory(directory, attempt, meeting, meeting_try), arm, meeting,
                       attempt, meeting_try)
            return Held(channel_meeting(meeting, attempt, meeting_try, arm=arm,
                                        memo=f"{arm}'s chair at {meeting.id}."))
        return hold


def _carry(directory: Path, arm: str, meeting: Meeting, attempt: int, meeting_try: int) -> None:
    """A D′ try as the harness leaves it: the prefix written for it, and one
    turn in its call log that carried another."""
    directory.mkdir(parents=True)
    (directory / PREFIX).write_text(f"Transcripts before {meeting.id}.")
    line = {
        "tags": {"arm": arm, "series": SERIES.id, "meeting": meeting.id,
                 "meeting_kind": meeting.kind.value, "attempt": str(attempt),
                 "try": str(meeting_try)},
        "agent_id": PANEL.chair.id, "purpose": "turn", "provider": "anthropic",
        "model": "claude-sonnet-4-6", "model_alias": "quality",
        "started_at": "2026-10-01T09:01:00+00:00", "input_tokens": 100, "output_tokens": 10,
        "cache_write_tokens": 2000, "cache_read_tokens": 0,
        "cache_prefix_sha256": prefix_sha256("Another prefix."), "error": None,
    }
    (directory / "calls.jsonl").write_text(json.dumps(line) + "\n")


async def _practice(
    root: Path, holds: _Holds, judge: _Judge | None, arms: tuple[str, ...] = ("A", "C"),
    alias: Alias = ARMS_ALIAS,
) -> dict[str, Any]:
    return await run_practice(
        root, arms, panel=PANEL, series=SERIES, scored=SCORED, names=NAMES,
        client=LLMClient(judge or _Judge()), binary=_BINARY, alias=alias,
        prompts=None if judge is None else PROMPTS, sleep=no_wait, make_hold=holds,
    )


class TestAPracticeRun:
    async def test_each_arm_holds_the_practice_series_in_the_order_named(
        self, tmp_path: Path,
    ) -> None:
        holds = _Holds()
        await _practice(tmp_path, holds, _Judge(), arms=("C", "A"))
        assert holds.held == [("C", m.id) for m in SERIES.meetings] + [
            ("A", m.id) for m in SERIES.meetings
        ]
        assert sorted(p.name for p in (tmp_path / "pairs" / SERIES.id).iterdir()) == ["A", "C"]

    async def test_the_judge_scores_every_packet_in_its_own_order(self, tmp_path: Path) -> None:
        judge = _Judge()
        await _practice(tmp_path, _Holds(), judge)
        drawn = read_packets(tmp_path)
        assert judge.asked == [packet_text(p) for p in drawn.order("judge")]
        assert len(judge.asked) == 6  # two arms: two memos and a recall check each
        calls = read_judge_log(tmp_path / JUDGING / BATCH / CALL_LOG)
        assert {r.series for r in calls.records} == {"practice"}

    async def test_the_report_is_written_and_returned(self, tmp_path: Path) -> None:
        report = await _practice(tmp_path, _Holds(), _Judge())
        assert json.loads((tmp_path / REPORT).read_text()) == json.loads(json.dumps(report))
        text = (tmp_path / SUMMARY).read_text()
        assert text.startswith(f"EXP-001 practice run, series {SERIES.id}: arms A, C")
        assert report["judge"]["calls"] == 6 and report["judge"]["projection"]["fits"]
        assert report["judge"]["projection"]["output_headroom"] > 0  # at the scored sizes
        assert set(report["recall_marks"]) == {"A", "C"}

    async def test_the_people_get_their_packets_and_the_seal_is_kept_apart(
        self, tmp_path: Path,
    ) -> None:
        await _practice(tmp_path, _Holds(), _Judge())
        assert sorted(p.name for p in (tmp_path / "raters").iterdir()) == [
            "person-1.txt", "person-2.txt",
        ]
        assert (tmp_path / "sealed" / "seal.json").is_file()

    async def test_check_3_reads_each_d_prime_try_against_the_prefix_written_for_it(
        self, tmp_path: Path,
    ) -> None:
        report = await _practice(tmp_path, _Holds(prefixed=True), _Judge(), arms=("D-prime",))
        findings = report["check_3"]["findings"]
        assert len(findings) == 3  # the two plan meetings and the recall check
        assert all("not the prefix the harness wrote" in finding for finding in findings)

    async def test_the_usage_counts_every_call_once_set_aside_pairs_included(
        self, tmp_path: Path,
    ) -> None:
        """A crash can stop a set-aside pair's line half written: that log is
        named and left out of the totals, and the report is still written."""
        aside = tmp_path / INTERRUPTED / f"{SERIES.id}-C-1"
        plan, control = SERIES.meetings[1], SERIES.meetings[2]
        _carry(try_directory(aside, 1, plan, 1), "C", plan, 1, 1)
        torn = try_directory(aside, 1, control, 1) / "calls.jsonl"
        torn.parent.mkdir(parents=True)
        torn.write_text('{"tags": {"arm": "C", "series"')
        report = await _practice(tmp_path, _Holds(prefixed=True), _Judge())
        # C's three meetings after the briefing each logged a turn, and the set-aside pair one.
        assert report["usage"]["models"][ARMS_MODEL]["calls"] == 4
        assert report["usage"]["unread"] == [str(torn.relative_to(tmp_path))]
        assert "left out of these totals" in (tmp_path / SUMMARY).read_text()

    async def test_offline_nothing_is_judged(self, tmp_path: Path) -> None:
        """The mock provider cannot answer as the judge."""
        report = await _practice(tmp_path, _Holds(), None)
        assert report["judge"] is None and report["recall_marks"] is None
        assert not (tmp_path / JUDGING).exists()
        assert (tmp_path / "raters" / "person-1.txt").is_file()

    @pytest.mark.parametrize("arms", [("A", "A"), ("A", "E"), ()])
    async def test_arms_named_twice_unknown_or_none_are_refused(
        self, tmp_path: Path, arms: tuple[str, ...],
    ) -> None:
        with pytest.raises(ValueError, match="arms"):
            await _practice(tmp_path, _Holds(), _Judge(), arms=arms)


class TestStartedAgain:
    async def test_it_goes_on_where_it_stopped_and_asks_nothing_twice(
        self, tmp_path: Path,
    ) -> None:
        first = await _practice(tmp_path, _Holds(), _Judge())
        holds, judge = _Holds(), _Judge()
        again = await _practice(tmp_path, holds, judge)
        assert (holds.held, judge.asked) == ([], [])
        assert again == first

    async def test_a_harness_fault_closes_the_directory(self, tmp_path: Path) -> None:
        """A practice run after the fix starts afresh, so none mixes
        meetings held before and after a fix."""
        with pytest.raises(HarnessFault):
            await _practice(tmp_path, _Holds(fault=("C", SERIES.meetings[2].id)), _Judge())
        closed = json.loads((tmp_path / RUN).read_text())
        assert (closed["arms"], closed["fault"]["arm"]) == (["A", "C"], "C")
        assert "the call log is unreadable" in closed["fault"]["message"]
        holds = _Holds()
        with pytest.raises(RefusedError, match="closed this practice run"):
            await _practice(tmp_path, holds, _Judge())
        assert holds.held == []

    async def test_a_run_started_again_with_other_arms_is_refused(self, tmp_path: Path) -> None:
        """Other arms get a practice run of their own; the packets are drawn once."""
        await _practice(tmp_path, _Holds(), _Judge())
        holds = _Holds()
        with pytest.raises(RefusedError, match="holds arms A, C"):
            await _practice(tmp_path, holds, _Judge(), arms=("A",))
        assert holds.held == []
        assert json.loads((tmp_path / RUN).read_text()) == {
            "arms": ["A", "C"], "alias": dataclasses.asdict(ARMS_ALIAS),
        }

    async def test_a_run_started_again_on_another_provider_is_refused(
        self, tmp_path: Path,
    ) -> None:
        """Meetings held offline are never judged, or reported, as the provider's."""
        await _practice(tmp_path, _Holds(), None, alias=Alias("mock", "offline", 0, 0))
        holds, judge = _Holds(), _Judge()
        with pytest.raises(RefusedError, match="mock offline, not anthropic claude-sonnet-4-6"):
            await _practice(tmp_path, holds, judge)
        assert (holds.held, judge.asked) == ([], [])

    async def test_a_second_run_at_once_is_refused(self, tmp_path: Path) -> None:
        """It would set aside the pair the first run is holding, and hold it again."""
        holds = _Holds(paused=("C", SERIES.meetings[1].id))
        first = asyncio.create_task(_practice(tmp_path, holds, _Judge()))
        await asyncio.sleep(0)  # the first run holds C's plan meeting, paused
        second = _Holds()
        with pytest.raises(RefusedError, match="another run is holding this practice run"):
            await _practice(tmp_path, second, _Judge())
        assert second.held == [] and not (tmp_path / INTERRUPTED).exists()
        holds.go_on.set()
        await first
        assert holds.held[-1] == ("C", SERIES.meetings[-1].id)

    async def test_a_fault_while_judging_leaves_it_open_and_asks_nothing_twice(
        self, tmp_path: Path,
    ) -> None:
        with pytest.raises(JudgeFault):
            await _practice(tmp_path, _Holds(), _Judge(readable=False))
        assert "fault" not in json.loads((tmp_path / RUN).read_text())
        judge = _Judge()
        with pytest.raises(JudgeFault):  # the kept answer is read again, still unreadable
            await _practice(tmp_path, _Holds(), judge)
        assert judge.asked == []
