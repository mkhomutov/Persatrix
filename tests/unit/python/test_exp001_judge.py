"""EXP-001 harness — the LLM judge, the third rater (PR 6a).

Part 2 §2 of the pre-registration: three raters score every memo and recall
answer, and one of them is an LLM judge, ``claude-opus-5`` with its default
settings, one pass per packet, whose prompts are in ``rubric.yaml``. Part 2
§4 caps its spend at $25. These tests pin the judge's prompts, how it reads
a reply, and how its call log reads back. How a batch of packets is judged
is in ``test_exp001_judge_batch.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from evaluators.exp001.costs import PRICES
from evaluators.exp001.judge import JUDGE_MODEL, criteria_text, load_prompts, read_judge_log
from evaluators.exp001.judge_answers import (
    UnreadableReplyError,
    read_memo_reply,
    read_recall_reply,
)
from evaluators.exp001.materials import MeetingKind
from evaluators.exp001.runtime import CallLogError
from evaluators.exp001.scoring import rater_total

from ._exp001_judge_test_helpers import (
    _RECALL_JSON,
    CONTROL,
    PLAN,
    PROMPTS,
    RECALL,
    RUBRIC,
    _judge_line,
    _memo_json,
)

# ─── The prompts ─────────────────────────────────────────────


class TestPrompts:
    def test_the_memo_prompt_is_the_rubrics_with_the_criteria_filled_in(self) -> None:
        rubric = yaml.safe_load(RUBRIC.read_text())
        before, after = rubric["judge"]["memo_prompt"].split("{criteria}")
        assert PROMPTS.memo == before + criteria_text(rubric["memo"]["criteria"]) + after

    def test_the_recall_prompt_is_the_rubrics_with_the_rule_filled_in(self) -> None:
        rubric = yaml.safe_load(RUBRIC.read_text())
        before, after = rubric["judge"]["recall_prompt"].split("{rule}")
        assert PROMPTS.recall == before + rubric["recall"]["rule"] + after

    def test_the_json_the_judge_is_asked_for_stays_literal(self) -> None:
        assert '{"C1": 0, "C1_problems_found": ["K1"], "C2": 0, "C2_facts_used": ["F2"],' in (
            PROMPTS.memo
        )
        assert '{"R1": "right", "R2": "wrong", ...}' in PROMPTS.recall

    def test_a_criterion_reads_as_the_rubric_gives_it(self) -> None:
        rubric = yaml.safe_load(RUBRIC.read_text())
        text = criteria_text(rubric["memo"]["criteria"])
        assert text.startswith(
            "C1: Finds the plan's own problems\n"
            "Reads: the key's three problems\n"
            "0: Finds none of the three problems on the key.\n"
            "1: Finds one of them.\n"
            "2: Finds two or all three.\n"
            "Note: A problem counts when the memo names it and says why it matters, in any "
            "words. Other problems, true or not, earn nothing here.\n"
            "\n"
            "C2: Uses what the organisation said at earlier meetings\n"
        )
        # A criterion with no reads or note has neither line.
        assert (
            "C3: Recommends a clear decision\n"
            "0: Does not pick exactly one of the lettered options.\n"
            "1: Picks one option but gives no reason, or no condition that would change it.\n"
            "2: Picks one option, gives the main reason, and says what would change it.\n"
            "\n"
            "C4: "
        ) in text
        assert text.endswith("2: Contains no errors.")

    def test_a_rubric_whose_prompt_lost_its_placeholder_is_refused(self, tmp_path: Path) -> None:
        rubric = yaml.safe_load(RUBRIC.read_text())
        rubric["judge"]["memo_prompt"] = rubric["judge"]["memo_prompt"].replace("{criteria}", "")
        (tmp_path / "rubric.yaml").write_text(yaml.safe_dump(rubric))
        with pytest.raises(ValueError, match="criteria"):
            load_prompts(tmp_path / "rubric.yaml")

    def test_the_judge_is_the_model_the_rubric_and_the_price_table_name(self) -> None:
        rubric = yaml.safe_load(RUBRIC.read_text())
        assert rubric["judge"]["model"].split(",")[0] == JUDGE_MODEL
        assert JUDGE_MODEL in PRICES


# ─── Reading a reply ─────────────────────────────────────────


class TestReadMemoReply:
    def test_a_plans_scores_read_as_the_scoring_module_takes_them(self) -> None:
        assert PLAN.plan_key is not None
        read = read_memo_reply(_memo_json(), PLAN.plan_key)
        assert read.scores == {"C1": 1, "C2": 2, "C3": 2, "C4": 1, "C5": 2}
        assert (read.problems_found, read.facts_used) == (("K1",), ("F1", "F2"))
        assert read.reasons["C4"] == "Because."
        assert rater_total(read.scores, MeetingKind.PLAN) == 8

    def test_a_control_plans_c2_is_null(self) -> None:
        assert CONTROL.plan_key is not None
        read = read_memo_reply(_memo_json(c2=None, C2_facts_used=[]), CONTROL.plan_key)
        assert read.scores["C2"] is None
        assert rater_total(read.scores, MeetingKind.CONTROL) == pytest.approx(7.5)

    @pytest.mark.parametrize("wrap", [
        "```json\n{}\n```",
        "Here are the scores:\n{}",
        "{}\n",
        "{}\nI weighed {{F1}} heavily.",
        "The memo cites {{F2}}; the scores:\n```json\n{}\n```",
    ])
    def test_json_in_a_fence_or_among_lines_of_prose_is_read(self, wrap: str) -> None:
        assert PLAN.plan_key is not None
        assert read_memo_reply(wrap.format(_memo_json()), PLAN.plan_key).scores["C1"] == 1

    def test_the_lists_and_reasons_may_be_left_out(self) -> None:
        assert PLAN.plan_key is not None
        bare = json.dumps({"C1": 0, "C2": 0, "C3": 0, "C4": 0, "C5": 0})
        read = read_memo_reply(bare, PLAN.plan_key)
        assert (read.problems_found, read.facts_used, read.reasons) == ((), (), {})

    def test_a_null_list_or_reason_counts_as_left_out(self) -> None:
        """The prompt asks for a null C2 on a control plan; a null beside it changes no score."""
        assert CONTROL.plan_key is not None and PLAN.plan_key is not None
        text = _memo_json(c2=None, C2_facts_used=None, reasons={"C1": "Because.", "C2": None})
        read = read_memo_reply(text, CONTROL.plan_key)
        assert (read.scores["C2"], read.facts_used, read.reasons) == (None, (), {"C1": "Because."})
        read = read_memo_reply(_memo_json(C1_problems_found=None, reasons=None), PLAN.plan_key)
        assert (read.problems_found, read.reasons) == ((), {})

    @pytest.mark.parametrize(("text", "where"), [
        ("", "no JSON"),
        ("I cannot score this memo.", "no JSON"),
        ("{not json}", "not JSON"),
        ("[1, 2]", "no JSON"),
        (_memo_json(C1=3), "C1"),
        (_memo_json(C3=True), "C3"),
        (_memo_json(C4="2"), "C4"),
        (_memo_json(c2=None), "C2"),  # a plan's C2 must be scored
        (json.dumps({"C1": 1, "C2": 1, "C3": 1, "C4": 1}), "C5"),
        (_memo_json(C1_problems_found="K1"), "C1_problems_found"),
        (_memo_json(reasons=["Because."]), "reasons"),
        # An echoed example beside the answer: nothing says which one is meant.
        (_memo_json(C1=0) + "\n" + _memo_json(), "2 JSON objects"),
        ('{"C1": 0, ' + _memo_json()[1:], "C1 given twice"),
    ])
    def test_a_reply_that_breaks_the_scale_is_unreadable(self, text: str, where: str) -> None:
        assert PLAN.plan_key is not None
        with pytest.raises(UnreadableReplyError, match=where):
            read_memo_reply(text, PLAN.plan_key)

    def test_a_c2_score_on_a_control_plan_is_unreadable(self) -> None:
        assert CONTROL.plan_key is not None
        with pytest.raises(UnreadableReplyError, match="C2"):
            read_memo_reply(_memo_json(c2=1), CONTROL.plan_key)


class TestReadRecallReply:
    def test_each_answer_is_right_or_wrong(self) -> None:
        assert RECALL.recall_key is not None
        assert read_recall_reply(_RECALL_JSON, RECALL.recall_key) == {
            "R1": True, "R2": True, "R3": False,
        }

    def test_the_marks_and_questions_are_read_in_any_case(self) -> None:
        assert RECALL.recall_key is not None
        text = json.dumps({"R1": "Right", " r2 ": " WRONG", "r3": "wrong"})
        assert read_recall_reply(text, RECALL.recall_key) == {
            "R1": True, "R2": False, "R3": False,
        }

    @pytest.mark.parametrize(("marks", "where"), [
        ({"R1": "right", "R2": "right"}, "R3"),
        ({"R1": "right", "R2": "right", "R3": "right", "R4": "right"}, "R4"),
        ({"R1": "right", "R2": "partly", "R3": "right"}, "R2"),
        ({"R1": True, "R2": "right", "R3": "right"}, "R1"),
        ({"R1": "wrong", "R2": "right", "R3": "right", "r1": "right"}, "R1 is marked twice"),
    ])
    def test_a_reply_that_misses_or_adds_a_question_is_unreadable(
        self, marks: dict[str, Any], where: str,
    ) -> None:
        assert RECALL.recall_key is not None
        with pytest.raises(UnreadableReplyError, match=where):
            read_recall_reply(json.dumps(marks), RECALL.recall_key)

    def test_a_question_marked_twice_is_unreadable(self) -> None:
        assert RECALL.recall_key is not None
        text = '{"R1": "wrong", "R2": "right", "R3": "right", "R1": "right"}'
        with pytest.raises(UnreadableReplyError, match="R1 given twice"):
            read_recall_reply(text, RECALL.recall_key)


# ─── The judge's call log ────────────────────────────────────


class TestReadJudgeLog:
    def _write(self, path: Path, *lines: dict[str, Any]) -> Path:
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))
        return path

    def test_no_file_is_an_empty_log(self, tmp_path: Path) -> None:
        log = read_judge_log(tmp_path / "none.jsonl")
        assert (log.records, log.failures) == ((), ())

    def test_an_arms_line_is_refused(self, tmp_path: Path) -> None:
        tags = {"arm": "C", "series": "series-1", "meeting": "m", "meeting_kind": "plan",
                "attempt": "1", "try": "1"}
        path = self._write(tmp_path / "j.jsonl", _judge_line(tags=tags))
        with pytest.raises(CallLogError, match="j.jsonl:1"):
            read_judge_log(path)

    def test_another_raters_line_is_refused(self, tmp_path: Path) -> None:
        tags = {**_judge_line()["tags"], "rater": "person-a"}
        path = self._write(tmp_path / "j.jsonl", _judge_line(tags=tags))
        with pytest.raises(CallLogError, match="not a judge's line"):
            read_judge_log(path)

    def test_a_line_with_another_purpose_is_refused(self, tmp_path: Path) -> None:
        path = self._write(tmp_path / "j.jsonl", _judge_line(purpose="turn"))
        with pytest.raises(CallLogError, match="purpose"):
            read_judge_log(path)

    def test_a_line_torn_by_a_crash_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "j.jsonl"
        path.write_text(json.dumps(_judge_line()) + "\n" + '{"tags": {"rater": "judge"\n')
        with pytest.raises(CallLogError, match="j.jsonl:2"):
            read_judge_log(path)
