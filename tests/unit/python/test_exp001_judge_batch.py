"""EXP-001 harness — the LLM judge judging a batch of packets (PR 6a).

One call per packet, a provider error retried, an answer never asked for
twice, the $25 cap (part 2 §4), and a call log that names no arm. The
judge's prompts and how it reads an answer are in ``test_exp001_judge.py``.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents import call_log
from agents.llm_client import AnthropicProvider, LLMClient, LLMResponse, StopReason, Usage
from agents.llm_types import LLMProvider, LLMToolResult
from evaluators.exp001.attempts import HarnessFault
from evaluators.exp001.costs import CallPurpose, judging_spend
from evaluators.exp001.judge import (
    CALL_LOG,
    JUDGE_MODEL,
    MAX_TOKENS,
    REPLIES,
    TEMPERATURE,
    JudgeFault,
    JudgePrompts,
    Packet,
    judge_batch,
    read_judge_log,
)
from evaluators.exp001.packets import (
    Blinded,
    blind,
    memo_packet_text,
    recall_packet_text,
)
from evaluators.exp001.scoring import MemoRef

from ._exp001_judge_test_helpers import (
    _RECALL_JSON,
    CONTROL,
    PLAN,
    PRACTICE,
    PROMPTS,
    RECALL,
    _judge_line,
    _memo_json,
)

_NO_WAIT: list[float] = []
# A judge line for packet p1 of the practice batch this file judges.
_PRACTICE_TAGS = {**_judge_line()["tags"], "batch": "practice", "packet": "p1"}


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


async def _judge(
    provider: LLMProvider, directory: Path, prompts: JudgePrompts = PROMPTS, **kwargs: Any,
) -> Any:
    blinded = _blinded()
    packets: list[Packet] = [*blinded.memo_packets, *blinded.recall_packets]
    return await judge_batch(
        LLMClient(provider), packets, prompts, directory, batch="practice", sleep=_no_wait,
        **kwargs,
    )


def _all_three() -> _Provider:
    return _Provider(_memo_json(), _memo_json(c2=None), _RECALL_JSON)


class TestJudgeBatch:
    async def test_one_call_per_packet_with_the_default_settings(self, tmp_path: Path) -> None:
        provider = _all_three()
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
        await _judge(_all_three(), tmp_path)
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

    async def test_a_refusal_is_kept_as_one_and_is_unreadable(self, tmp_path: Path) -> None:
        """The adapter reads Anthropic's ``refusal`` as END_TURN; the kept answer keeps the name."""
        with patch.dict(sys.modules, {"anthropic": MagicMock()}):
            provider = AnthropicProvider(api_key="test-key")
        provider._client = AsyncMock()
        usage = SimpleNamespace(input_tokens=10, output_tokens=0)
        provider._client.messages.create = AsyncMock(
            return_value=SimpleNamespace(content=[], stop_reason="refusal", usage=usage),
        )
        with pytest.raises(JudgeFault, match="refused"):
            await _judge(provider, tmp_path)
        [kept] = [json.loads(t) for t in (tmp_path / REPLIES).read_text().splitlines()]
        assert (kept["text"], kept["stop_reason"]) == ("", "refusal")

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

    async def test_a_resumed_packet_counts_its_tries_on(self, tmp_path: Path) -> None:
        with pytest.raises(RateLimitError):
            await _judge(_Provider(*(RateLimitError("429") for _ in range(4))), tmp_path)
        await _judge(_all_three(), tmp_path)
        lines = [json.loads(t) for t in (tmp_path / CALL_LOG).read_text().splitlines()]
        tries = [line["tags"]["try"] for line in lines if line["tags"]["packet"] == "p1"]
        assert tries == ["1", "2", "3", "4", "5"]

    async def test_an_error_the_harness_caused_is_a_fault(self, tmp_path: Path) -> None:
        class BadRequestError(Exception):
            pass

        with pytest.raises(JudgeFault, match="BadRequestError"):
            await _judge(_Provider(BadRequestError("400")), tmp_path)

    def test_a_judge_fault_is_a_harness_fault(self) -> None:
        """So the run's count of harness faults (pre-registration §3) includes it."""
        assert issubclass(JudgeFault, HarnessFault)

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

    @pytest.mark.parametrize("tags", [{"batch": "scored"}, {"packet": "zz"}])
    async def test_a_call_for_another_batch_or_packet_is_a_harness_fault(
        self, tmp_path: Path, tags: dict[str, str],
    ) -> None:
        """Only the batch's own calls count toward its cap."""
        line = _judge_line(tags={**_PRACTICE_TAGS, **tags})
        (tmp_path / CALL_LOG).write_text(json.dumps(line) + "\n")
        provider = _Provider()
        with pytest.raises(JudgeFault, match="another batch or packet"):
            await _judge(provider, tmp_path)
        assert provider.calls == []

    @pytest.mark.parametrize("line", [
        '{"tags": {"rater": "judge"',  # torn by a crash
        # The price table has no cache price for the judge's model.
        json.dumps(_judge_line(tags=_PRACTICE_TAGS, cache_read_tokens=5)),
    ])
    async def test_a_call_log_the_harness_cannot_read_or_price_is_a_harness_fault(
        self, tmp_path: Path, line: str,
    ) -> None:
        (tmp_path / CALL_LOG).write_text(line + "\n")
        with pytest.raises(JudgeFault, match="cannot be read"):
            await _judge(_Provider(), tmp_path)

    async def test_a_reply_kept_for_another_batch_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / REPLIES).write_text(json.dumps({
            "packet": "zz", "kind": "memo", "text": "{}", "stop_reason": "end_turn", "asked": "",
        }) + "\n")
        with pytest.raises(JudgeFault, match="zz"):
            await _judge(_Provider(), tmp_path)

    async def test_a_torn_kept_answer_is_a_harness_fault(self, tmp_path: Path) -> None:
        (tmp_path / REPLIES).write_text('{"packet": "p1", "kind": "me\n')
        provider = _Provider()
        with pytest.raises(JudgeFault, match="replies.jsonl:1: not a kept answer"):
            await _judge(provider, tmp_path)
        assert provider.calls == []


class TestOnePass:
    """A packet is asked once, and a batch that cannot promise that stops."""

    async def test_a_second_answer_kept_for_a_packet_is_a_harness_fault(
        self, tmp_path: Path,
    ) -> None:
        await _judge(_all_three(), tmp_path)
        kept = tmp_path / REPLIES
        kept.write_text(kept.read_text() + kept.read_text().splitlines()[0] + "\n")
        again = _Provider()
        with pytest.raises(JudgeFault, match="second answer for packet p1"):
            await _judge(again, tmp_path)
        assert again.calls == []

    async def test_a_packet_listed_twice_is_refused(self, tmp_path: Path) -> None:
        plan = _blinded().memo_packets[0]
        provider = _Provider(_memo_json(), _memo_json())
        with pytest.raises(JudgeFault, match="listed twice"):
            await judge_batch(
                LLMClient(provider), [plan, plan], PROMPTS, tmp_path, batch="practice",
                sleep=_no_wait,
            )
        assert provider.calls == []

    async def test_a_second_run_of_the_batch_at_once_is_refused(self, tmp_path: Path) -> None:
        answered = asyncio.Event()

        class Held(_Provider):
            async def create_message(self, **kwargs: Any) -> LLMResponse:
                await answered.wait()
                return await super().create_message(**kwargs)

        first = asyncio.create_task(
            _judge(Held(_memo_json(), _memo_json(c2=None), _RECALL_JSON), tmp_path),
        )
        await asyncio.sleep(0)  # the first run holds the batch, waiting on its first call
        second = _Provider()
        with pytest.raises(RuntimeError, match="another run") as refused:
            await _judge(second, tmp_path)
        assert not isinstance(refused.value, HarnessFault)
        answered.set()
        assert (await first).recall_marks == {"p3": {"R1": True, "R2": True, "R3": False}}
        assert second.calls == []

    async def test_a_call_answered_with_no_answer_kept_is_a_harness_fault(
        self, tmp_path: Path,
    ) -> None:
        """The answer was paid for but lost, say on a full disk: asking again is a second pass."""
        await _judge(_all_three(), tmp_path)
        (tmp_path / REPLIES).unlink()
        again = _Provider()
        with pytest.raises(JudgeFault, match="second pass: p1, p2, p3"):
            await _judge(again, tmp_path)
        assert again.calls == []

    async def test_a_write_that_stops_short_is_finished(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write = os.write

        def short(fd: int, data: bytes) -> int:  # a kept answer goes out 5 bytes at a time
            return write(fd, data[:5] if b'"stop_reason"' in data else data)

        with monkeypatch.context() as patched:
            patched.setattr(os, "write", short)
            await _judge(_all_three(), tmp_path)
        again = _Provider()
        judged = await _judge(again, tmp_path)
        assert (again.calls, judged.recall_marks["p3"]["R3"]) == ([], False)

    async def test_an_answer_asked_another_way_is_a_harness_fault(self, tmp_path: Path) -> None:
        """A resumed batch must not mix answers from two different calls."""
        await _judge(_all_three(), tmp_path)
        changed = JudgePrompts(memo=PROMPTS.memo + "\nBe brief.", recall=PROMPTS.recall)
        again = _Provider()
        with pytest.raises(JudgeFault, match="p1: its kept answer was asked with another"):
            await _judge(again, tmp_path, changed)
        assert again.calls == []
