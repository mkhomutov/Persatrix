"""EXP-001 harness — scoring a run: the people's sheets, the judge's
answers, and the seal opened only once every score is in (the scoring PR).

Pre-registration §6 step 4: two people and the LLM judge score every
packet, and the map from packet ID to arm stays sealed until every score is
in. Scoring a run's directory first writes each person a blank score sheet,
then reports how far each rater has got, reading neither the seal nor any
answer's arm. Once every score is in, and only when asked, it opens the
seal, keeps the moment and what each sheet held, and writes the figures of
part 2: these tests check the figures reach the report from a real run
directory; their arithmetic is in ``test_exp001_result.py``.

The runs here are made up: every answer names its arm, so the stand-in
raters score by a table (``_exp001_scores_test_helpers.py``).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from evaluators.exp001 import scoring_run
from evaluators.exp001.materials import Materials, load_materials
from evaluators.exp001.result_report import progress_words
from evaluators.exp001.scoring_run import (
    OPENED,
    RESULT,
    RESULT_SUMMARY,
    Progress,
    ScoringRefused,
    score,
)
from evaluators.exp001.sheets import Sheet, read_sheet, sheet_path

from ._exp001_judge_test_helpers import PROMPTS
from ._exp001_run_test_helpers import EXP, T0
from ._exp001_scores_test_helpers import (
    LONG_MEMO,
    MISSING_MEMO,
    ArmJudge,
    build_practice,
    build_scored,
    fill_sheets,
    reversed_scorer,
    table_scorer,
)

MATERIALS: Materials = load_materials(EXP)
_ON_TIME = T0 + dt.timedelta(days=1)
_LATE = T0 + dt.timedelta(days=21, seconds=1)
_BOTH = {"person-1": table_scorer, "person-2": table_scorer}


@pytest.fixture(scope="module")
def _held(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One scored run, held and judged once for the whole module."""
    root = tmp_path_factory.mktemp("scored") / "run"
    asyncio.run(build_scored(root))
    return root


@pytest.fixture
def run(_held: Path, tmp_path: Path) -> Path:
    """A copy of the scored run for one test to score."""
    return Path(shutil.copytree(_held, tmp_path / "run"))


def _score(root: Path, **kwargs: Any) -> Any:
    kwargs.setdefault("now", lambda: _ON_TIME)
    return score(root, materials=MATERIALS, prompts=PROMPTS, **kwargs)


def _opened(root: Path, scorers: dict[str, Any] = _BOTH, **kwargs: Any) -> dict[str, Any]:
    _score(root)  # writes the blank sheets
    fill_sheets(root / "window-1", scorers)
    result = _score(root, open_seal=True, **kwargs)
    assert isinstance(result, dict)
    return result


@pytest.fixture
def sealed_shut(run: Path) -> Iterator[Path]:
    """The run with its seal unreadable, so reading it fails loudly."""
    sealed = run / "window-1" / "sealed"
    sealed.chmod(0)
    yield run
    sealed.chmod(0o755)


class TestBeforeEveryScoreIsIn:
    def test_a_first_look_writes_each_persons_blank_sheet(self, sealed_shut: Path) -> None:
        progress = _score(sealed_shut)
        assert isinstance(progress, Progress)
        assert progress.written == ("person-1", "person-2")
        assert sheet_path(sealed_shut / "window-1", "person-1").is_file()
        assert len(progress.left["person-1"]) == progress.packets == 124  # one memo missing
        assert progress.left["judge"] == ()
        assert not progress.complete

    def test_progress_says_when_scoring_is_due(self, sealed_shut: Path) -> None:
        progress = _score(sealed_shut)
        assert progress.due == T0 + dt.timedelta(days=21)
        assert f"due by {progress.due.isoformat()}" in progress_words(progress)

    def test_progress_reads_neither_the_seal_nor_opens_it_when_asked_early(
        self, sealed_shut: Path,
    ) -> None:
        _score(sealed_shut)
        fill_sheets(sealed_shut / "window-1", _BOTH, leave=3)
        progress = _score(sealed_shut, open_seal=True)
        assert isinstance(progress, Progress)
        assert [len(progress.left[p]) for p in ("person-1", "person-2")] == [3, 3]
        assert not (sealed_shut / "window-1" / OPENED).exists()
        assert not (sealed_shut / RESULT).exists()

    def test_every_score_in_still_waits_to_be_asked(self, sealed_shut: Path) -> None:
        _score(sealed_shut)
        fill_sheets(sealed_shut / "window-1", _BOTH)
        progress = _score(sealed_shut)
        assert isinstance(progress, Progress) and progress.complete
        assert not (sealed_shut / "window-1" / OPENED).exists()

    async def test_a_run_the_judge_has_not_finished_cannot_be_opened(self, tmp_path: Path) -> None:
        await build_scored(tmp_path)
        (tmp_path / "window-1" / "judging" / "scored" / "replies.jsonl").write_text("")
        (tmp_path / "window-1" / "judging" / "scored" / "calls.jsonl").write_text("")
        _score(tmp_path)
        fill_sheets(tmp_path / "window-1", _BOTH)
        progress = _score(tmp_path, open_seal=True)
        assert isinstance(progress, Progress)
        assert len(progress.left["judge"]) == 124
        assert not progress.complete


class TestOpeningTheSeal:
    def test_it_opens_once_asked_and_keeps_when_and_what_each_sheet_held(self, run: Path) -> None:
        result = _opened(run)
        opened = json.loads((run / "window-1" / OPENED).read_text())
        assert opened["at"] == _ON_TIME.isoformat() == result["opened_at"]
        assert set(opened["sheets"]) == {"person-1", "person-2"}
        assert json.loads((run / RESULT).read_text()) == result
        assert "rule 4" in (run / RESULT_SUMMARY).read_text()

    def test_once_open_a_later_look_reads_the_same_result(self, run: Path) -> None:
        result = _opened(run)
        assert _score(run, now=lambda: _LATE) == result

    def test_a_sheet_saved_again_with_the_same_scores_still_reads(self, run: Path) -> None:
        result = _opened(run)
        sheet = sheet_path(run / "window-1", "person-1")
        sheet.write_bytes(b"\xef\xbb\xbf" + sheet.read_bytes().replace(b"\r\n", b"\n")
                          .replace(b"\n", b"\r\n"))
        assert _score(run) == result

    def test_a_sheet_saved_while_the_seal_opens_is_refused_after(
        self, run: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # person-2 saves a changed score just after the harness read their sheet.
        def read_then_saved(path: Path, drawn: Any, person: str) -> Sheet:
            sheet = read_sheet(path, drawn, person)
            if person == "person-2":
                path.write_text(path.read_text().replace(",2,", ",1,", 1))
            return sheet

        _score(run)
        fill_sheets(run / "window-1", _BOTH)
        monkeypatch.setattr(scoring_run, "read_sheet", read_then_saved)
        assert isinstance(_score(run, open_seal=True), dict)
        monkeypatch.undo()
        with pytest.raises(ScoringRefused, match="person-2's sheet changed"):
            _score(run)

    def test_a_file_the_harness_wrote_changed_once_open_is_refused(self, run: Path) -> None:
        _opened(run)
        seal = run / "window-1" / "sealed" / "seal.json"
        seal.write_text(json.dumps(json.loads(seal.read_text()), indent=4))
        with pytest.raises(ScoringRefused, match="seal changed after the seal was opened"):
            _score(run)

    def test_a_sheet_changed_once_the_seal_is_open_is_refused(self, run: Path) -> None:
        _opened(run)
        sheet = sheet_path(run / "window-1", "person-2")
        sheet.write_text(sheet.read_text().replace(",2,", ",1,", 1))
        with pytest.raises(ScoringRefused, match="person-2's sheet changed"):
            _score(run)

    def test_the_rule_the_table_fires_and_the_checks_it_read(self, run: Path) -> None:
        result = _opened(run)
        assert (result["outcome"], result["why"]) == ("rule 4", None)
        assert result["series"] == [s.id for s in MATERIALS.series]
        assert result["raters"] == ["person-1", "person-2", "judge"]
        deciding = [(c["x"], c["y"]) for c in result["comparisons"] if c["deciding"]]
        assert deciding == [("C", "A"), ("D", "C"), ("D", "D-prime")]
        assert result["on_time"] is True
        assert result["scoring_due"] == (T0 + dt.timedelta(days=21)).isoformat()

    def test_a_memo_never_written_scores_zero_and_ranks_with_none(self, run: Path) -> None:
        memos = {(m["arm"], m["series"], m["meeting"]): m for m in _opened(run)["memos"]}
        missing = memos[MISSING_MEMO]
        assert (missing["missing"], missing["packet"], missing["quality"]) == (True, None, 0.0)
        assert memos[("D", "series-1", "series-1-plan-1")]["totals"] == dict.fromkeys(
            ("person-1", "person-2", "judge"), 10.0,
        )

    def test_each_meeting_says_what_closed_it_and_whether_the_memo_was_cut(
        self, run: Path,
    ) -> None:
        rows = {(m["arm"], m["series"], m["meeting"]): m for m in _opened(run)["meetings"]}
        assert len(rows) == 5 * 5 * 6
        assert rows[LONG_MEMO]["cut"] is True and rows[LONG_MEMO]["words"] == 454
        plan = rows[("C", "series-2", "series-2-plan-2")]
        assert (plan["closed_by"], plan["energy"], plan["cut"]) == (
            "vote", {"lunar-stoat": 0.8}, False,
        )
        assert plan["minutes"] == pytest.approx(6.0)
        assert rows[("A", "series-2", "series-2-plan-2")]["minutes"] == pytest.approx(1 / 3)
        assert rows[MISSING_MEMO]["minutes"] is None

    def test_each_arm_is_reported_on_dollars_minutes_and_recall(self, run: Path) -> None:
        arms = _opened(run)["arms"]
        # Each try logs $0.15: five a series, the briefing and four plans, over four plans.
        assert arms["C"]["dollars_per_plan"] == pytest.approx(0.1875)
        assert arms["C"]["quality_per_dollar"] == pytest.approx(arms["C"]["quality"] / 0.1875)
        assert arms["A"]["minutes_per_plan"] == pytest.approx(1 / 3)
        assert arms["B"]["minutes_left_out"] == 1
        assert arms["D"]["recall"] == {"right": 20, "asked": 20}
        assert arms["A"]["recall"] == {"right": 0, "asked": 20}

    def test_agreement_on_the_table_is_perfect(self, run: Path) -> None:
        agreement = _opened(run)["agreement"]
        assert [p["mean"] for p in agreement["pairs"]] == pytest.approx([1.0, 1.0, 1.0])
        assert agreement["sufficient"] is True

    def test_scores_finished_after_their_21_days_are_inconclusive(self, run: Path) -> None:
        result = _opened(run, now=lambda: _LATE)
        assert result["on_time"] is False
        assert (result["outcome"], result["why"]) == (
            "inconclusive", "scoring finished after its 21 days",
        )

    def test_people_who_disagree_make_the_scores_inconclusive(self, run: Path) -> None:
        result = _opened(run, {"person-1": table_scorer, "person-2": reversed_scorer})
        assert result["agreement"]["mean"] < 0.4
        assert (result["outcome"], result["why"]) == (
            "inconclusive", "the raters' agreement is below 0.4",
        )
        assert result["people_alone"]["outcome"] == "inconclusive"


class TestWhatCannotBeScored:
    async def test_a_judge_stopped_by_its_cap_leaves_the_people_alone(
        self, tmp_path: Path,
    ) -> None:
        # Each judge call costs $5.02, so its fifth reaches the $25 cap.
        await build_scored(tmp_path, judge=ArmJudge(output=200_000))
        result = _opened(tmp_path)
        assert result["judge_cap_reached"] is True
        assert result["raters"] == ["person-1", "person-2"]
        assert (result["outcome"], result["why"]) == (
            "inconclusive", "the judge's spend reached its cap",
        )
        assert result["people_alone"]["outcome"] == "rule 4"

    def test_a_window_a_harness_fault_closed_is_refused(self, run: Path) -> None:
        state = json.loads((run / "run.json").read_text())
        state["windows"][-1]["fault"] = {"message": "a fault", "at": T0.isoformat()}
        (run / "run.json").write_text(json.dumps(state))
        with pytest.raises(ScoringRefused, match="stopped"):
            _score(run)

    def test_a_seal_that_names_other_series_than_the_run_kept_is_refused(
        self, run: Path,
    ) -> None:
        state = json.loads((run / "run.json").read_text())
        state["windows"][-1]["ended"]["kept"].remove("series-5")
        (run / "run.json").write_text(json.dumps(state))
        _score(run)
        fill_sheets(run / "window-1", _BOTH)
        with pytest.raises(ScoringRefused, match="does not name every answer"):
            _score(run, open_seal=True)
        assert not (run / "window-1" / OPENED).exists()

    def test_a_seal_gone_missing_opens_nothing(self, run: Path) -> None:
        _score(run)
        fill_sheets(run / "window-1", _BOTH)
        (run / "window-1" / "sealed" / "seal.json").unlink()
        with pytest.raises(ScoringRefused, match="no seal"):
            _score(run, open_seal=True)
        assert not (run / "window-1" / OPENED).exists()

    def test_a_window_with_no_scored_meeting_ended_is_refused(self, run: Path) -> None:
        state = json.loads((run / "run.json").read_text())
        state["windows"][-1]["ended"]["last_scored_meeting_at"] = None
        (run / "run.json").write_text(json.dumps(state))
        with pytest.raises(ScoringRefused, match="no scored meeting"):
            _score(run)

    def test_a_directory_with_no_run_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ScoringRefused, match="no run"):
            _score(tmp_path)

    async def test_a_practice_run_is_scored_but_fires_no_rule(self, tmp_path: Path) -> None:
        await build_practice(tmp_path)
        progress = _score(tmp_path)
        assert progress.due is None and "due by" not in progress_words(progress)
        fill_sheets(tmp_path, _BOTH)
        result = _score(tmp_path, open_seal=True)
        assert (result["run"], result["window"], result["scoring_due"]) == ("practice", None, None)
        assert (result["outcome"], result["why"]) == ("incomplete", "fewer than four series")
        assert result["comparisons"] == []
        assert result["arms"]["D"]["quality"] == pytest.approx(10.0)
        # Each try logs $0.15: three a practice series, the briefing and two plans, over two.
        assert result["arms"]["D"]["dollars_per_plan"] == pytest.approx(0.225)
        assert os.path.exists(tmp_path / OPENED)
