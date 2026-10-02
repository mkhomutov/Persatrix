"""EXP-001 harness — arm D′: reading check 3 from the call records (PR 5d).

Check 3 asks that the first call of a meeting that carries the prefix writes
it to the cache, that every later call of the meeting reads it, and that no
other arm sets a cache breakpoint. ``check_cache`` reads it try by try from
the call records, and from the calls that failed, which carry the prefix as
well; a try that made no turn call has nothing to show. Moved here from
``test_exp001_arm_d_prime.py``, which holds the hold's and the deployment's
tests, when that file reached its size warning.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from evaluators.exp001.arm_d_prime import ARM, check_cache
from evaluators.exp001.costs import CallPurpose, CallRecord
from evaluators.exp001.materials import MeetingKind
from evaluators.exp001.runtime import FailedCall

_T0 = dt.datetime(2026, 10, 1, 9, 0, tzinfo=dt.UTC)
_KEY = (ARM, "series-1", "plan-1", 1, 1)


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


def _failed(
    *, adviser: str = "ripple-kite", purpose: CallPurpose = CallPurpose.REPLY, second: int = 0,
    prefix: str | None = "a" * 64, error: str = "OverloadedError",
) -> FailedCall:
    return FailedCall(
        arm=ARM, series="series-1", meeting="plan-1", attempt=1, meeting_try=1, adviser=adviser,
        started_at=_T0 + dt.timedelta(seconds=second), error=error, purpose=purpose,
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

    def test_keepalives_that_read_the_prefix_carry_the_meeting_to_its_memo(self) -> None:
        """The chair's keep-alives (PR 5e) carry the prefix through the quiet
        spell before the memo, so each reads it, and so does the memo."""
        calls = [
            *_meeting_that_holds()[:4],
            _record(adviser="lunar-stoat", purpose=CallPurpose.KEEPALIVE, second=290, read=12_000),
            _record(adviser="lunar-stoat", purpose=CallPurpose.KEEPALIVE, second=540, read=12_000),
            _record(adviser="lunar-stoat", purpose=CallPurpose.MEMO, second=655, read=12_000),
        ]
        assert check_cache(calls) == []

    def test_a_keepalive_that_wrote_the_prefix_again_is_a_finding(self) -> None:
        """One sent after the entry had gone pays to write it again."""
        late = _record(adviser="lunar-stoat", purpose=CallPurpose.KEEPALIVE, second=400,
                       write=12_000)
        [finding] = check_cache([*_meeting_that_holds()[:4], late])
        assert "lunar-stoat's keepalive call" in finding
        assert "wrote 12000 and read 0 tokens of the cache, 355 s after" in finding

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

    def test_a_first_call_that_only_read_names_what_can_hide_a_write(self) -> None:
        """The provider's library retries a failed request inside one call, so
        an attempt that wrote the entry and failed leaves one line that reads
        it; only the provider's usage report shows the attempt."""
        [finding] = check_cache([_record(read=12_000)])
        assert "wrote 0 tokens to the cache and read 12000" in finding
        assert "retried" in finding and "usage report" in finding

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

    def test_arm_a_names_the_arm_for_its_call(self) -> None:
        """Arm A's one call plays every adviser, so it has none of its own."""
        [finding] = check_cache([dataclasses.replace(_record(arm="A"), adviser=None)])
        assert f"arm A's reply call at {_T0.isoformat()} carried a prefix" in finding

    def test_each_try_is_judged_on_its_own(self) -> None:
        """A second try writes the prefix again, as the wait before it makes sure."""
        records = [
            _record(write=12_000), _record(second=30, read=12_000),
            _record(meeting_try=2, second=900, write=12_000),
        ]
        assert check_cache(records) == []

    def test_a_call_must_carry_the_prefix_the_harness_wrote(self) -> None:
        records = [_record(write=12_000), _record(second=30, read=12_000)]
        [finding] = check_cache(records, written={_KEY: "c" * 64})
        assert "not the prefix the harness wrote" in finding
        assert check_cache(records, written={_KEY: "a" * 64}) == []


class TestEveryTurnCarriesIt:
    def test_a_turn_of_a_try_given_a_prefix_that_carried_none_is_a_finding(self) -> None:
        records = [
            _record(write=12_000),
            _record(adviser="lunar-stoat", second=30, prefix=None),
            _record(adviser="lunar-stoat", purpose=CallPurpose.MEMO, second=90, prefix=None),
        ]
        findings = check_cache(records, written={_KEY: "a" * 64})
        assert findings == [
            f"{ARM}, series-1, plan-1, attempt 1, try 1: lunar-stoat's {purpose} call at "
            f"{(_T0 + dt.timedelta(seconds=second)).isoformat()} carried no prefix, though "
            "the try was given one"
            for purpose, second in (("reply", 30), ("memo", 90))
        ]

    def test_without_the_file_one_turn_that_carried_it_shows_the_try_was_given_one(
        self,
    ) -> None:
        records = [_record(write=12_000), _record(adviser="lunar-stoat", second=30, prefix=None)]
        [finding] = check_cache(records)
        assert "lunar-stoat's reply call" in finding and "though the try was given one" in finding

    def test_a_try_that_made_no_turn_call_has_nothing_to_show(self) -> None:
        """A try cut short in its bids, or whose deployment never started,
        wrote no cache entry and needed none."""
        bids = [_record(purpose=CallPurpose.BID, prefix=None, second=s) for s in (0, 1, 2)]
        assert check_cache(bids, written={_KEY: "a" * 64}) == []
        assert check_cache([], written={_KEY: "a" * 64}) == []


class TestFailedCalls:
    def test_a_turn_that_failed_still_carried_the_prefix(self) -> None:
        bids = [_record(purpose=CallPurpose.BID, prefix=None)]
        assert check_cache(bids, failures=[_failed(second=5)], written={_KEY: "a" * 64}) == []

    def test_a_turn_that_failed_without_the_prefix_is_a_finding(self) -> None:
        [finding] = check_cache([], failures=[_failed(prefix=None)], written={_KEY: "a" * 64})
        assert "ripple-kite's reply call" in finding and "carried no prefix" in finding

    def test_a_first_writer_that_failed_lets_the_next_call_read(self) -> None:
        """A call can write the entry and fail once its response has begun;
        the turn after it then reads what that call wrote."""
        failures = [_failed(second=0)]
        records = [_record(adviser="lunar-stoat", second=60, read=12_000)]
        assert check_cache(records, failures=failures) == []

    def test_a_failed_call_of_another_arm_that_carried_a_prefix_is_a_finding(self) -> None:
        [finding] = check_cache([], failures=[dataclasses.replace(_failed(), arm="C")])
        assert finding.endswith(f"carried a prefix, which only arm {ARM}'s turns do")
