"""EXP-001 harness — the LLM judge, the third rater (PR 6a).

Part 2 §2 of the pre-registration: three raters score every memo and recall
answer, and one of them is an LLM judge, ``claude-opus-5`` with its default
settings, one pass per packet, whose prompts are in ``rubric.yaml``. Part 2
§4 caps its spend at $25. These tests pin the judge's prompts, how it reads
a reply, and how a batch of packets is judged: one call each, a provider
error retried, a reply never asked for twice, the cap, and a call log that
names no arm.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from agents import call_log
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.llm_types import LLMToolResult
from evaluators.exp001.costs import PRICES, CallPurpose, judging_spend
from evaluators.exp001.judge import (
    CALL_LOG,
    JUDGE_MODEL,
    MAX_TOKENS,
    REPLIES,
    TEMPERATURE,
    JudgeFault,
    Packet,
    UnreadableReplyError,
    criteria_text,
    judge_batch,
    load_prompts,
    read_judge_log,
    read_memo_reply,
    read_recall_reply,
)
from evaluators.exp001.materials import MeetingKind, Series, load_materials
from evaluators.exp001.packets import (
    Blinded,
    blind,
    memo_packet_text,
    recall_packet_text,
)
from evaluators.exp001.runtime import CallLogError
from evaluators.exp001.scoring import MemoRef, rater_total

MATERIALS = Path(__file__).resolve().parents[3] / "evaluators" / "experiments" / "EXP-001"
RUBRIC = MATERIALS / "rubric.yaml"
PROMPTS = load_prompts(RUBRIC)
PRACTICE: Series = load_materials(MATERIALS).practice
PLAN, CONTROL, RECALL = PRACTICE.meetings[1], PRACTICE.meetings[2], PRACTICE.meetings[3]
_NO_WAIT: list[float] = []


def _ids() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"p{next(counter)}"


def _blinded() -> Blinded:
    """The practice series' two memos and its recall reply, as packets p1..p3."""
    return blind(
        [PRACTICE],
        {
            MemoRef("C", PRACTICE.id, PLAN.id): "## Recommendation\nB.",
            MemoRef("D", PRACTICE.id, CONTROL.id): "## Recommendation\nC.",
        },
        {MemoRef("C", PRACTICE.id, RECALL.id): "1. May.\n2. $25.\n3. Two."},
        (),
        new_id=_ids(),
    )


def _memo_json(c2: int | None = 2, **overrides: Any) -> str:
    reply: dict[str, Any] = {
        "C1": 1, "C1_problems_found": ["K1"], "C2": c2, "C2_facts_used": ["F1", "F2"],
        "C3": 2, "C4": 1, "C5": 2,
        "reasons": {c: "Because." for c in ("C1", "C2", "C3", "C4", "C5")},
    }
    reply.update(overrides)
    return json.dumps(reply)


_RECALL_JSON = json.dumps({"R1": "right", "R2": "right", "R3": "wrong"})


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
    ])
    def test_json_in_a_fence_or_after_a_line_of_prose_is_read(self, wrap: str) -> None:
        assert PLAN.plan_key is not None
        assert read_memo_reply(wrap.format(_memo_json()), PLAN.plan_key).scores["C1"] == 1

    def test_the_lists_and_reasons_may_be_left_out(self) -> None:
        assert PLAN.plan_key is not None
        bare = json.dumps({"C1": 0, "C2": 0, "C3": 0, "C4": 0, "C5": 0})
        read = read_memo_reply(bare, PLAN.plan_key)
        assert (read.problems_found, read.facts_used, read.reasons) == ((), (), {})

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

    def test_the_marks_are_read_in_any_case(self) -> None:
        assert RECALL.recall_key is not None
        text = json.dumps({"R1": "Right", "R2": " WRONG", "R3": "wrong"})
        assert read_recall_reply(text, RECALL.recall_key) == {
            "R1": True, "R2": False, "R3": False,
        }

    @pytest.mark.parametrize(("marks", "where"), [
        ({"R1": "right", "R2": "right"}, "R3"),
        ({"R1": "right", "R2": "right", "R3": "right", "R4": "right"}, "R4"),
        ({"R1": "right", "R2": "partly", "R3": "right"}, "R2"),
        ({"R1": True, "R2": "right", "R3": "right"}, "R1"),
    ])
    def test_a_reply_that_misses_or_adds_a_question_is_unreadable(
        self, marks: dict[str, Any], where: str,
    ) -> None:
        assert RECALL.recall_key is not None
        with pytest.raises(UnreadableReplyError, match=where):
            read_recall_reply(json.dumps(marks), RECALL.recall_key)


# ─── Judging a batch ─────────────────────────────────────────


class RateLimitError(Exception):
    """Named as the provider's SDK names a 429, which is all the harness reads."""


class _Provider:
    """Answers each call in turn and keeps what it was sent. An exception in
    the list is raised in its turn; a string is a finished reply."""

    name = "anthropic"
    supports_prompt_cache = True

    def __init__(self, *replies: str | LLMResponse | Exception, usage: Usage = Usage(0, 0)):
        self.calls: list[dict[str, Any]] = []
        self._replies = list(replies)
        self._usage = usage

    async def create_message(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, LLMResponse):
            return reply
        return LLMResponse(text=reply, stop_reason=StopReason.END_TURN, usage=self._usage)

    def format_tool_definitions(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return tools

    def append_tool_round(
        self, messages: list[Any], response: LLMResponse, tool_results: list[LLMToolResult],
    ) -> list[Any]:
        raise AssertionError("the judge sends no tools")


async def _no_wait(seconds: float) -> None:
    _NO_WAIT.append(seconds)


@pytest.fixture(autouse=True)
def _no_ambient_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(call_log.CALL_LOG_ENV, raising=False)
    call_log.reset_call_log()
    _NO_WAIT.clear()


async def _judge(provider: _Provider, directory: Path, **kwargs: Any) -> Any:
    blinded = _blinded()
    packets: list[Packet] = [*blinded.memo_packets, *blinded.recall_packets]
    return await judge_batch(
        LLMClient(provider), packets, PROMPTS, directory, batch="practice", sleep=_no_wait,
        **kwargs,
    )


class TestJudgeBatch:
    async def test_one_call_per_packet_with_the_default_settings(self, tmp_path: Path) -> None:
        provider = _Provider(_memo_json(), _memo_json(c2=None), _RECALL_JSON)
        judged = await _judge(provider, tmp_path)
        blinded = _blinded()
        plan, control = blinded.memo_packets
        [recall] = blinded.recall_packets
        assert provider.calls == [
            {
                "model": JUDGE_MODEL,
                "system": PROMPTS.memo,
                "messages": [{"role": "user", "content": memo_packet_text(plan)}],
                "tools": [],
                "max_tokens": MAX_TOKENS,
                "temperature": TEMPERATURE,
            },
            {
                "model": JUDGE_MODEL,
                "system": PROMPTS.memo,
                "messages": [{"role": "user", "content": memo_packet_text(control)}],
                "tools": [],
                "max_tokens": MAX_TOKENS,
                "temperature": TEMPERATURE,
            },
            {
                "model": JUDGE_MODEL,
                "system": PROMPTS.recall,
                "messages": [{"role": "user", "content": recall_packet_text(recall)}],
                "tools": [],
                "max_tokens": MAX_TOKENS,
                "temperature": TEMPERATURE,
            },
        ]
        assert judged.memo_scores["p1"].scores["C2"] == 2
        assert judged.memo_scores["p2"].scores["C2"] is None
        assert judged.recall_marks == {"p3": {"R1": True, "R2": True, "R3": False}}
        assert (judged.cap_reached, judged.left) == (False, ())

    async def test_the_call_log_names_the_packet_and_never_the_arm(self, tmp_path: Path) -> None:
        provider = _Provider(
            _memo_json(), _memo_json(c2=None), _RECALL_JSON, usage=Usage(1000, 200),
        )
        await _judge(provider, tmp_path)
        lines = [json.loads(t) for t in (tmp_path / CALL_LOG).read_text().splitlines()]
        assert [line["purpose"] for line in lines] == ["judge"] * 3
        assert [line["tags"] for line in lines] == [
            {"rater": "judge", "batch": "practice", "packet": "p1", "kind": "plan", "try": "1"},
            {"rater": "judge", "batch": "practice", "packet": "p2", "kind": "control", "try": "1"},
            {"rater": "judge", "batch": "practice", "packet": "p3", "kind": "recall", "try": "1"},
        ]
        records = read_judge_log(tmp_path / CALL_LOG).records
        assert {(r.arm, r.series, r.adviser, r.purpose, r.model) for r in records} == {
            ("", "practice", None, CallPurpose.JUDGE, JUDGE_MODEL),
        }
        assert [r.meeting for r in records] == ["p1", "p2", "p3"]
        # $5 a million in, $25 a million out: 3 × (0.005 + 0.005).
        assert judging_spend(records) == pytest.approx(0.03)

    async def test_a_judged_packet_is_never_asked_again(self, tmp_path: Path) -> None:
        await _judge(_Provider(_memo_json(), _memo_json(c2=None), _RECALL_JSON), tmp_path)
        again = _Provider()
        judged = await _judge(again, tmp_path)
        assert again.calls == []
        assert judged.memo_scores["p1"].scores["C1"] == 1
        assert judged.recall_marks["p3"]["R3"] is False

    async def test_a_stopped_batch_resumes_where_it_stopped(self, tmp_path: Path) -> None:
        with pytest.raises(JudgeFault):
            await _judge(_Provider(_memo_json(), "not scores"), tmp_path)
        replies = [json.loads(t) for t in (tmp_path / REPLIES).read_text().splitlines()]
        assert [(r["packet"], r["text"]) for r in replies] == [
            ("p1", _memo_json()), ("p2", "not scores"),
        ]
        # The unreadable reply is kept, so asking again would be a second pass.
        again = _Provider()
        with pytest.raises(JudgeFault, match="p2"):
            await _judge(again, tmp_path)
        assert again.calls == []

    async def test_a_reply_cut_off_at_the_token_limit_is_unreadable(self, tmp_path: Path) -> None:
        cut = LLMResponse(text=_memo_json(), stop_reason=StopReason.MAX_TOKENS, usage=Usage(0, 0))
        with pytest.raises(JudgeFault, match="token limit"):
            await _judge(_Provider(cut), tmp_path)

    async def test_a_provider_error_is_retried_as_no_pass(self, tmp_path: Path) -> None:
        provider = _Provider(
            RateLimitError("429"), _memo_json(), _memo_json(c2=None), _RECALL_JSON,
        )
        judged = await _judge(provider, tmp_path)
        assert len(provider.calls) == 4
        assert _NO_WAIT == [60.0]
        assert judged.memo_scores["p1"].scores["C1"] == 1
        log = read_judge_log(tmp_path / CALL_LOG)
        assert [(f.meeting, f.meeting_try, f.error) for f in log.failures] == [
            ("p1", 1, "RateLimitError"),
        ]
        assert [(r.meeting, r.meeting_try) for r in log.records] == [
            ("p1", 2), ("p2", 1), ("p3", 1),
        ]

    async def test_after_three_retries_the_provider_error_stops_the_batch(
        self, tmp_path: Path,
    ) -> None:
        provider = _Provider(*(RateLimitError("429") for _ in range(4)))
        with pytest.raises(RateLimitError):
            await _judge(provider, tmp_path)
        assert len(provider.calls) == 4
        assert _NO_WAIT == [60.0, 300.0, 900.0]
        assert not (tmp_path / REPLIES).exists() or (tmp_path / REPLIES).read_text() == ""

    async def test_an_error_the_harness_caused_is_a_fault(self, tmp_path: Path) -> None:
        class BadRequestError(Exception):
            pass

        with pytest.raises(JudgeFault, match="BadRequestError"):
            await _judge(_Provider(BadRequestError("400")), tmp_path)

    async def test_judging_stops_at_the_cap(self, tmp_path: Path) -> None:
        # Each call: 1 000 000 in at $5 plus 40 000 out at $25 = $6.
        provider = _Provider(_memo_json(), _memo_json(c2=None), usage=Usage(1_000_000, 40_000))
        judged = await _judge(provider, tmp_path, cap=12.0)
        assert len(provider.calls) == 2
        assert judged.cap_reached
        assert judged.left == ("p3",)
        assert judged.spend == pytest.approx(12.0)
        assert judged.recall_marks == {}

    async def test_the_cap_counts_the_calls_of_an_earlier_run(self, tmp_path: Path) -> None:
        provider = _Provider(_memo_json(), usage=Usage(1_000_000, 40_000))
        with pytest.raises(JudgeFault):  # the provider has no second reply: the run stops
            await _judge(provider, tmp_path, cap=6.5)
        again = _Provider(_memo_json(c2=None), usage=Usage(1_000_000, 40_000))
        judged = await _judge(again, tmp_path, cap=6.5)
        assert len(again.calls) == 1
        assert judged.cap_reached and judged.left == ("p3",)

    async def test_a_reply_kept_for_another_batch_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / REPLIES).write_text(json.dumps({
            "packet": "zz", "kind": "memo", "text": "{}", "stop_reason": "end_turn",
        }) + "\n")
        with pytest.raises(JudgeFault, match="zz"):
            await _judge(_Provider(), tmp_path)


class TestReadJudgeLog:
    def _line(self, **overrides: Any) -> dict[str, Any]:
        line = {
            "tags": {"rater": "judge", "batch": "scored", "packet": "ab12cd34", "kind": "plan",
                     "try": "1"},
            "agent_id": "", "purpose": "judge", "provider": "anthropic", "model": JUDGE_MODEL,
            "model_alias": None, "started_at": "2036-10-06T10:00:00+00:00",
            "input_tokens": 10, "output_tokens": 2, "cache_write_tokens": 0,
            "cache_read_tokens": 0, "cache_prefix_sha256": None, "error": None,
        }
        line.update(overrides)
        return line

    def _write(self, path: Path, *lines: dict[str, Any]) -> Path:
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))
        return path

    def test_no_file_is_an_empty_log(self, tmp_path: Path) -> None:
        log = read_judge_log(tmp_path / "none.jsonl")
        assert (log.records, log.failures) == ((), ())

    def test_an_arms_line_is_refused(self, tmp_path: Path) -> None:
        tags = {"arm": "C", "series": "series-1", "meeting": "m", "meeting_kind": "plan",
                "attempt": "1", "try": "1"}
        path = self._write(tmp_path / "j.jsonl", self._line(tags=tags))
        with pytest.raises(CallLogError, match="j.jsonl:1"):
            read_judge_log(path)

    def test_a_line_with_another_purpose_is_refused(self, tmp_path: Path) -> None:
        path = self._write(tmp_path / "j.jsonl", self._line(purpose="turn"))
        with pytest.raises(CallLogError, match="purpose"):
            read_judge_log(path)
