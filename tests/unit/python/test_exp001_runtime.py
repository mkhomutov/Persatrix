"""EXP-001 harness — each meeting's clock and reading the call log (PR 3).

Pre-registration §3, check 7: every adviser's clock shows the meeting's story
date; the frozen "Story clock" choice starts it at 10:00 that day. Check 4:
every call is recorded with its arm, meeting, adviser, purpose, time and
tokens. The harness gives each agent process the settings for both, and
turns the lines the agents write into the call records ``costs`` prices.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path

import pytest

from agents import call_log
from agents.clock import CLOCK_ANCHOR_ENV, CLOCK_START_ENV
from agents.llm_client import LLMClient, LLMResponse, Usage
from agents.llm_types import LLMCallPurpose
from agents.observability import metrics
from agents.prompt_prefix import PROMPT_PREFIX_ENV
from evaluators.exp001.costs import CallPurpose, CallRecord
from evaluators.exp001.materials import MeetingKind
from evaluators.exp001.runtime import (
    CallLogError,
    MemoTurn,
    call_log_env,
    call_log_scope,
    meeting_clock_env,
    prompt_prefix_env,
    read_call_log,
    story_start,
)

_BEGAN = dt.datetime(2026, 10, 1, 9, 30, 15, tzinfo=dt.UTC)
_T = dt.datetime(2026, 10, 1, 9, 40, tzinfo=dt.UTC)


class TestMeetingClock:
    def test_starts_at_ten_on_the_story_date_anchored_to_the_real_start(self):
        env = meeting_clock_env(dt.date(2036, 10, 13), _BEGAN)
        assert env == {
            CLOCK_START_ENV: "2036-10-13T10:00:00+00:00",
            CLOCK_ANCHOR_ENV: "2026-10-01T09:30:15+00:00",
        }

    def test_a_real_start_without_a_zone_is_refused(self):
        with pytest.raises(ValueError, match="zone"):
            meeting_clock_env(dt.date(2036, 10, 13), _BEGAN.replace(tzinfo=None))

    def test_the_story_start_is_ten_utc_on_the_date(self):
        assert story_start(dt.date(2036, 10, 13)) == dt.datetime(2036, 10, 13, 10, tzinfo=dt.UTC)


class TestCallLogScope:
    async def test_the_harness_tags_its_own_calls_per_meeting(self, tmp_path, monkeypatch):
        """Arm A's call and the judge run inside the harness process, so they
        are tagged with a scope rather than the process settings."""
        monkeypatch.delenv(call_log.CALL_LOG_ENV, raising=False)
        call_log.reset_call_log()
        path = tmp_path / "calls.jsonl"

        class _Provider:
            name = "anthropic"

            async def create_message(self, **_: object) -> LLMResponse:
                return LLMResponse(text="ok", usage=Usage(1, 1))

        for meeting in ("plan-1", "plan-2"):
            with call_log_scope(path, arm="A", series="series-3", meeting=meeting,
                                meeting_kind=MeetingKind.PLAN, attempt=1, meeting_try=1):
                await LLMClient(_Provider()).create_message(
                    model="claude-sonnet-4-6", messages=[], system="s", tools=[],
                    max_tokens=10, temperature=0.7, purpose=LLMCallPurpose.TURN,
                )
        records = read_call_log(path).records
        assert [(r.arm, r.meeting, r.adviser) for r in records] == [
            ("A", "plan-1", None), ("A", "plan-2", None),
        ]


class TestCallLogEnv:
    def test_names_the_file_and_the_meeting(self, tmp_path):
        env = call_log_env(
            tmp_path / "calls.jsonl", arm="D-prime", series="series-2",
            meeting="plan-3", meeting_kind=MeetingKind.PLAN, attempt=1, meeting_try=2,
        )
        assert env[call_log.CALL_LOG_ENV] == str(tmp_path / "calls.jsonl")
        assert json.loads(env[call_log.CALL_TAGS_ENV]) == {
            "arm": "D-prime", "series": "series-2", "meeting": "plan-3",
            "meeting_kind": "plan", "attempt": "1", "try": "2",
        }

    def test_an_unknown_arm_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="arm"):
            call_log_env(
                tmp_path / "c.jsonl", arm="E", series="series-1", meeting="plan-1",
                meeting_kind=MeetingKind.PLAN, attempt=1, meeting_try=1,
            )


def _line(
    *,
    arm: str = "C",
    agent_id: str = "ember-owl",
    purpose: str | None = "turn",
    started_at: dt.datetime = _T,
    error: str | None = None,
    meeting: str = "plan-1",
    **tokens: int,
) -> dict:
    return {
        "tags": {"arm": arm, "series": "series-1", "meeting": meeting,
                 "meeting_kind": "plan", "attempt": "2", "try": "3"},
        "agent_id": agent_id,
        "purpose": purpose,
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "model_alias": "quality",
        "started_at": started_at.isoformat(),
        "input_tokens": tokens.get("input_tokens", 100),
        "output_tokens": tokens.get("output_tokens", 20),
        "cache_write_tokens": tokens.get("cache_write_tokens", 0),
        "cache_read_tokens": tokens.get("cache_read_tokens", 0),
        "error": error,
    }


def _write(path: Path, *lines: dict) -> Path:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


class TestReadCallLog:
    def test_a_line_becomes_a_call_record(self, tmp_path):
        log = read_call_log(_write(
            tmp_path / "c.jsonl", _line(cache_write_tokens=900, cache_read_tokens=40),
        ))
        assert log.records == (
            CallRecord(
                arm="C", series="series-1", meeting="plan-1", meeting_kind=MeetingKind.PLAN,
                attempt=2, adviser="ember-owl", purpose=CallPurpose.REPLY, started_at=_T,
                model="claude-sonnet-4-6", input_tokens=100, output_tokens=20,
                cache_write_tokens=900, cache_read_tokens=40, meeting_try=3,
            ),
        )
        assert log.failures == ()

    @pytest.mark.parametrize(("runtime", "purpose"), [
        ("turn", CallPurpose.REPLY),
        ("critic", CallPurpose.REPLY),
        ("revise", CallPurpose.REPLY),
        ("bid", CallPurpose.BID),
        ("summary", CallPurpose.SUMMARY),
        ("compress", CallPurpose.SUMMARY),
    ])
    def test_runtime_purposes_map_onto_the_pre_registered_ones(self, tmp_path, runtime, purpose):
        [record] = read_call_log(_write(tmp_path / "c.jsonl", _line(purpose=runtime))).records
        assert record.purpose is purpose

    def test_every_purpose_an_adviser_calls_with_has_a_mapping(self, tmp_path):
        lines = [_line(purpose=p.value) for p in LLMCallPurpose if p is not LLMCallPurpose.JUDGE]
        assert len(read_call_log(_write(tmp_path / "c.jsonl", *lines)).records) == len(lines)

    def test_a_judge_line_in_an_arms_log_is_refused(self, tmp_path):
        """The judge logs to its own file, tagged with no arm (PR 6a)."""
        path = _write(tmp_path / "c.jsonl", _line(purpose=LLMCallPurpose.JUDGE.value))
        with pytest.raises(CallLogError, match="judge"):
            read_call_log(path)

    @pytest.mark.parametrize("purpose", [None, "gossip"])
    def test_a_line_without_a_known_purpose_is_refused(self, tmp_path, purpose):
        path = _write(tmp_path / "c.jsonl", _line(purpose=purpose))
        with pytest.raises(CallLogError, match="c.jsonl:1"):
            read_call_log(path)

    @pytest.mark.parametrize(
        "tag", ["arm", "series", "meeting", "meeting_kind", "attempt", "try"],
    )
    def test_a_line_missing_a_tag_is_refused(self, tmp_path, tag):
        line = _line()
        del line["tags"][tag]
        with pytest.raises(CallLogError, match=tag):
            read_call_log(_write(tmp_path / "c.jsonl", line))

    def test_a_line_that_is_not_json_is_refused_with_its_line_number(self, tmp_path):
        path = tmp_path / "c.jsonl"
        path.write_text(json.dumps(_line()) + "\n{broken\n")
        with pytest.raises(CallLogError, match="c.jsonl:2"):
            read_call_log(path)

    def test_the_chairs_turns_after_the_memo_request_are_the_memo(self, tmp_path):
        asked = _T + dt.timedelta(minutes=5)
        memo = MemoTurn(arm="C", series="series-1", meeting="plan-1", attempt=2,
                        meeting_try=3, chair="ember-owl", asked_at=asked)
        log = read_call_log(
            _write(
                tmp_path / "c.jsonl",
                _line(started_at=asked - dt.timedelta(seconds=1)),  # still the discussion
                _line(started_at=asked),
                _line(started_at=asked, purpose="critic"),
                _line(started_at=asked, purpose="summary"),  # closing the meeting's memory
                _line(started_at=asked, agent_id="quiet-lynx"),  # not the chair
                _line(started_at=asked, meeting="plan-2"),  # another meeting
            ),
            memo_turns=[memo],
        )
        assert [r.purpose for r in log.records] == [
            CallPurpose.REPLY, CallPurpose.MEMO, CallPurpose.MEMO,
            CallPurpose.SUMMARY, CallPurpose.REPLY, CallPurpose.REPLY,
        ]

    @pytest.mark.parametrize(("arm", "counts"), [
        ("B", False), ("C", False), ("D-prime", False), ("D", True),
    ])
    def test_memory_writes_count_only_in_the_arm_that_has_memory(self, tmp_path, arm, counts):
        [summary, reply] = read_call_log(_write(
            tmp_path / "c.jsonl", _line(arm=arm, purpose="summary"), _line(arm=arm),
        )).records
        assert summary.counts_in_arm is counts
        assert reply.counts_in_arm is True

    def test_arm_a_has_no_adviser(self, tmp_path):
        [record] = read_call_log(_write(tmp_path / "c.jsonl", _line(arm="A"))).records
        assert record.adviser is None

    def test_failed_calls_are_kept_apart(self, tmp_path):
        log = read_call_log(_write(
            tmp_path / "c.jsonl", _line(), _line(error="TimeoutError", input_tokens=0),
        ))
        assert len(log.records) == 1
        [failure] = log.failures
        assert (failure.meeting, failure.meeting_try, failure.adviser, failure.error) == (
            "plan-1", 3, "ember-owl", "TimeoutError",
        )

    @pytest.mark.parametrize(("arm", "counts"), [("C", False), ("D", True)])
    def test_a_failed_memory_write_counts_as_its_answer_would(self, tmp_path, arm, counts):
        log = read_call_log(_write(
            tmp_path / "c.jsonl", _line(arm=arm, purpose="summary", error="RateLimitError"),
            _line(arm=arm, error="RateLimitError"),
        ))
        assert [f.counts_in_arm for f in log.failures] == [counts, True]

    def test_a_memo_request_marks_only_its_own_try(self, tmp_path):
        """A meeting held again after a provider error writes its calls under
        the next try; the memo asked for in one try is not the other's."""
        memo = MemoTurn(arm="C", series="series-1", meeting="plan-1", attempt=2,
                        meeting_try=2, chair="ember-owl", asked_at=_T)
        [record] = read_call_log(
            _write(tmp_path / "c.jsonl", _line()), memo_turns=[memo],
        ).records
        assert (record.meeting_try, record.purpose) == (3, CallPurpose.REPLY)

    def test_a_memo_request_without_a_zone_is_refused(self):
        with pytest.raises(ValueError, match="zone"):
            MemoTurn(arm="C", series="series-1", meeting="plan-1", attempt=2,
                     meeting_try=3, chair="ember-owl", asked_at=_T.replace(tzinfo=None))

    def test_no_file_is_an_empty_log(self, tmp_path):
        log = read_call_log(tmp_path / "missing.jsonl")
        assert (log.records, log.failures) == ((), ())

    def test_the_prefix_a_call_carried_is_kept(self, tmp_path):
        """Arm D′'s turns carry the earlier transcripts (PR 5d); the line names
        the prefix by its SHA-256, and a line from before PR 5d names none."""
        carried = {**_line(), "cache_prefix_sha256": "ab" * 32}
        records = read_call_log(_write(tmp_path / "c.jsonl", carried, _line())).records
        assert [r.cache_prefix for r in records] == ["ab" * 32, None]

    def test_a_failed_call_keeps_the_prefix_it_carried(self, tmp_path):
        """A turn can write the cache entry and still fail, so check 3 reads
        failed calls' prefixes too."""
        failed = {**_line(error="OverloadedError"), "cache_prefix_sha256": "ab" * 32}
        [failure] = read_call_log(_write(tmp_path / "c.jsonl", failed)).failures
        assert failure.cache_prefix == "ab" * 32

    @pytest.mark.parametrize("value", [5, "", ["ab"]])
    def test_a_prefix_that_is_not_a_digest_is_refused(self, tmp_path, value):
        line = {**_line(), "cache_prefix_sha256": value}
        with pytest.raises(CallLogError, match="c.jsonl:1"):
            read_call_log(_write(tmp_path / "c.jsonl", line))


class TestPromptPrefixEnv:
    def test_names_the_file_every_turn_carries(self, tmp_path):
        assert prompt_prefix_env(tmp_path / "prefix.txt") == {
            PROMPT_PREFIX_ENV: str(tmp_path / "prefix.txt"),
        }


async def test_a_call_that_carried_a_prefix_reads_back_with_its_digest(tmp_path, monkeypatch):
    """The runtime logs the prefix's SHA-256; the harness reads it back, so a
    record can be matched to the transcript file the harness wrote."""

    class _Provider:
        name = "anthropic"
        supports_prompt_cache = True

        async def create_message(self, **_: object) -> LLMResponse:
            return LLMResponse(text="ok", usage=Usage(120, 30, cache_write_tokens=3000))

    path = tmp_path / "calls.jsonl"
    for key, value in call_log_env(path, arm="D-prime", series="series-4", meeting="plan-2",
                                   meeting_kind=MeetingKind.PLAN, attempt=1,
                                   meeting_try=1).items():
        monkeypatch.setenv(key, value)
    call_log.reset_call_log()
    try:
        await LLMClient(_Provider()).create_message(
            model="claude-sonnet-4-6", messages=[], system="s", tools=[], max_tokens=10,
            temperature=0.7, purpose=LLMCallPurpose.TURN, cache_prefix="Meeting 1\n",
        )
    finally:
        call_log.reset_call_log()
    [record] = read_call_log(path).records
    assert record.cache_prefix == hashlib.sha256(b"Meeting 1\n").hexdigest()
    assert record.cache_write_tokens == 3000


async def test_what_the_runtime_writes_the_harness_reads(tmp_path, monkeypatch):
    """Check 4 end to end: the harness's settings, a real ``LLMClient`` call,
    and the record that comes back."""

    class _Provider:
        name = "anthropic"

        async def create_message(self, **_: object) -> LLMResponse:
            return LLMResponse(text="ok", usage=Usage(120, 30, cache_read_tokens=2000))

    path = tmp_path / "calls.jsonl"
    env = call_log_env(path, arm="D-prime", series="series-4", meeting="plan-2",
                       meeting_kind=MeetingKind.PLAN, attempt=1, meeting_try=1)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(metrics, "_AGENT_ID", "quiet-lynx")
    call_log.reset_call_log()
    try:
        await LLMClient(_Provider()).create_message(
            model="claude-sonnet-4-6", messages=[], system="s", tools=[],
            max_tokens=10, temperature=0.7, purpose=LLMCallPurpose.BID,
        )
    finally:
        call_log.reset_call_log()
    [record] = read_call_log(path).records
    assert (record.arm, record.series, record.meeting, record.attempt, record.meeting_try) == (
        "D-prime", "series-4", "plan-2", 1, 1,
    )
    assert (record.adviser, record.purpose, record.cache_read_tokens) == (
        "quiet-lynx", CallPurpose.BID, 2000,
    )
    assert os.environ[call_log.CALL_LOG_ENV] == str(path)
