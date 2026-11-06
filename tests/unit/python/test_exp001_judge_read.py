"""EXP-001 harness — reading the judge's scores back without asking it anything.

Scoring reads the judge's answers once its batch is over. Reading asks the
judge nothing: a batch not yet over reads as unfinished, and one its $25 cap
stopped reads as over, the packets it never judged named. The checks that
keep one pass, an answer asked with another prompt or kept for a packet not
in the batch, are the batch's own (``test_exp001_judge_batch.py``).
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from agents import call_log
from agents.llm_client import LLMClient, LLMResponse, Usage
from agents.llm_types import LLMToolResult, StopReason
from evaluators.exp001.judge import (
    REPLIES,
    JudgeFault,
    JudgePrompts,
    Packet,
    judge_batch,
    read_batch,
)
from evaluators.exp001.packets import blind
from evaluators.exp001.scoring import MemoRef

from ._exp001_judge_test_helpers import (
    _RECALL_JSON,
    CONTROL,
    PLAN,
    PRACTICE,
    PROMPTS,
    RECALL,
    _memo_json,
)


class _Provider:
    """Answers each call in turn; a call with nothing left to answer fails the test."""

    name = "anthropic"
    supports_prompt_cache = True

    def __init__(self, *replies: str, usage: Usage = Usage(0, 0)) -> None:
        self.replies, self.usage = list(replies), usage

    async def create_message(self, **kwargs: Any) -> LLMResponse:
        return LLMResponse(text=self.replies.pop(0), stop_reason=StopReason.END_TURN,
                           usage=self.usage)

    def format_tool_definitions(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return tools

    def append_tool_round(
        self, messages: list[Any], response: LLMResponse, tool_results: list[LLMToolResult],
    ) -> list[Any]:
        raise AssertionError("the judge sends no tools")


@pytest.fixture(autouse=True)
def _no_ambient_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(call_log.CALL_LOG_ENV, raising=False)
    call_log.reset_call_log()


def _packets() -> list[Packet]:
    """The practice series' two memos and its recall reply, as packets p1..p3."""
    counter = itertools.count(1)
    blinded = blind(
        [PRACTICE],
        {MemoRef("C", PRACTICE.id, PLAN.id): "## Recommendation\nB.",
         MemoRef("D", PRACTICE.id, CONTROL.id): "## Recommendation\nC."},
        {MemoRef("C", PRACTICE.id, RECALL.id): "1. May.\n2. $25.\n3. Two."},
        (),
        new_id=lambda: f"p{next(counter)}",
    )
    return [*blinded.memo_packets, *blinded.recall_packets]


async def _no_wait(seconds: float) -> None:
    """Stands in for asyncio.sleep between retries."""


async def _judge(provider: _Provider, directory: Path, packets: list[Packet], **kwargs: Any) -> Any:
    return await judge_batch(LLMClient(provider), packets, PROMPTS, directory,
                             batch="practice", sleep=_no_wait, **kwargs)


class TestReadBatch:
    async def test_a_finished_batch_reads_back_as_the_judge_scored_it(self, tmp_path: Path) -> None:
        packets = _packets()
        judged = await _judge(
            _Provider(_memo_json(), _memo_json(c2=None), _RECALL_JSON), tmp_path, packets,
        )
        read = read_batch(packets, PROMPTS, tmp_path, batch="practice")
        assert read == judged
        assert read is not None and not read.cap_reached

    async def test_a_batch_not_yet_over_reads_as_unfinished(self, tmp_path: Path) -> None:
        packets = _packets()
        await _judge(_Provider(_memo_json(), _memo_json(c2=None)), tmp_path, packets[:2])
        assert read_batch(packets, PROMPTS, tmp_path, batch="practice") is None

    def test_a_batch_never_begun_reads_as_unfinished(self, tmp_path: Path) -> None:
        assert read_batch(_packets(), PROMPTS, tmp_path / "none", batch="practice") is None

    async def test_a_batch_its_cap_stopped_is_over_with_the_packets_it_left(
        self, tmp_path: Path,
    ) -> None:
        packets = _packets()
        # Each call: 1 000 000 in at $5 plus 40 000 out at $25 = $6.
        provider = _Provider(_memo_json(), _memo_json(c2=None), usage=Usage(1_000_000, 40_000))
        judged = await _judge(provider, tmp_path, packets, cap=12.0)
        read = read_batch(packets, PROMPTS, tmp_path, batch="practice", cap=12.0)
        assert read == judged
        assert read is not None and read.cap_reached and read.left == ("p3",)

    async def test_an_answer_asked_with_another_prompt_is_a_harness_fault(
        self, tmp_path: Path,
    ) -> None:
        packets = _packets()
        await _judge(_Provider(_memo_json(), _memo_json(c2=None), _RECALL_JSON), tmp_path, packets)
        other = JudgePrompts(memo=PROMPTS.memo + " Be brief.", recall=PROMPTS.recall)
        with pytest.raises(JudgeFault, match="another prompt"):
            read_batch(packets, other, tmp_path, batch="practice")

    async def test_an_answer_for_a_packet_not_in_the_batch_is_a_harness_fault(
        self, tmp_path: Path,
    ) -> None:
        packets = _packets()
        await _judge(_Provider(_memo_json(), _memo_json(c2=None), _RECALL_JSON), tmp_path, packets)
        with pytest.raises(JudgeFault, match="not in this batch"):
            read_batch(packets[:2], PROMPTS, tmp_path, batch="practice")

    async def test_an_unreadable_answer_is_a_harness_fault(self, tmp_path: Path) -> None:
        packets = _packets()
        await _judge(_Provider(_memo_json(), _memo_json(c2=None), _RECALL_JSON), tmp_path, packets)
        kept = tmp_path / REPLIES
        lines = [json.loads(line) for line in kept.read_text().splitlines()]
        lines[0]["text"] = "No score today."
        kept.write_text("".join(json.dumps(line) + "\n" for line in lines))
        with pytest.raises(JudgeFault, match="unreadable"):
            read_batch(packets, PROMPTS, tmp_path, batch="practice")
