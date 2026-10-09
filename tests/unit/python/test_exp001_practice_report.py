"""EXP-001 harness — the practice run's report (PR 6b).

The practice run is the evidence that the pre-registration's checks pass
before any scored meeting, and it settles what the earlier harness PRs left
to it: check 3 on the real provider, whether a D′ discussion comes near the
cost close, whether the judge's scored spend fits its cap, and whether the
call log's totals match the provider's usage report. The report's figures
come from the call records and the kept tries; these tests hold each to
its rule.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from evaluators.exp001.attempts import Held, SeriesRun, Try
from evaluators.exp001.costs import ARMS_MODEL, CallPurpose, CallRecord, price_call
from evaluators.exp001.judge import JUDGE_MODEL, MAX_TOKENS, Judged, Packet, load_prompts
from evaluators.exp001.materials import MeetingKind, load_materials, load_series
from evaluators.exp001.packets import (
    MemoPacket,
    RecallPacket,
    memo_packet_text,
    recall_packet_text,
)
from evaluators.exp001.pairs import Kept
from evaluators.exp001.practice_report import (
    COST_CLOSE_TOKENS,
    SCORED_MEMO_PACKETS,
    SCORED_RECALL_PACKETS,
    SCORED_TRANSCRIPTS,
    Projection,
    build,
    discussion_tokens,
    project_judging,
    projected_tokens,
    recall_marks_by_arm,
    summary,
    usage_totals,
)
from evaluators.exp001.rating import Seal, packet_text
from evaluators.exp001.runtime import CallLog, FailedCall
from evaluators.exp001.scoring import MemoRef

_EXP = Path(__file__).resolve().parents[3] / "evaluators" / "experiments" / "EXP-001"
SERIES = load_series(_EXP / "practice.yaml")
BRIEFING, PLAN, CONTROL, RECALL = SERIES.meetings
SCORED = load_materials(_EXP).series
PROMPTS = load_prompts(_EXP / "rubric.yaml")
_T0 = dt.datetime(2026, 10, 1, 9, 0, tzinfo=dt.UTC)
_PREFIX = "a" * 64


def _at(minutes: float) -> dt.datetime:
    return _T0 + dt.timedelta(minutes=minutes)


def _record(
    minutes: float, purpose: CallPurpose = CallPurpose.REPLY, *, arm: str = "D-prime",
    meeting: str = PLAN.id, meeting_try: int = 1, tokens: tuple[int, int, int, int] = (0, 0, 0, 0),
    model: str = ARMS_MODEL, prefix: str | None = None, kind: MeetingKind = MeetingKind.PLAN,
) -> CallRecord:
    """A call: *tokens* are input, output, cache write and cache read."""
    return CallRecord(
        arm=arm, series=SERIES.id, meeting=meeting, meeting_kind=kind, attempt=1,
        adviser=None if arm in ("A", "") else "ripple-kite", purpose=purpose,
        started_at=_at(minutes), model=model, input_tokens=tokens[0], output_tokens=tokens[1],
        cache_write_tokens=tokens[2], cache_read_tokens=tokens[3], meeting_try=meeting_try,
        cache_prefix=prefix,
    )


def _failed(minutes: float, error: str = "RateLimitError", *, arm: str = "C") -> FailedCall:
    return FailedCall(
        arm=arm, series=SERIES.id, meeting=PLAN.id, attempt=1, meeting_try=1,
        adviser="ripple-kite", started_at=_at(minutes), error=error, purpose=CallPurpose.REPLY,
    )


def _kept(record: Mapping[str, Any] | None, answer: str | None = "The memo.") -> Held[Kept]:
    return Held(None if record is None else Kept(answer, record))


def _run(arm: str, *tries: Try[Kept]) -> SeriesRun[Kept]:
    return SeriesRun(arm, SERIES.id, tries, 1)


class TestTheDiscussionsTokens:
    def test_each_turn_and_bid_before_the_close_counts_as_the_wallet_counts_it(self) -> None:
        """The wallet charges a call every input token it carried, cached
        ones included, and its output, as ``agents/llm_client.py`` settles
        the lease."""
        records = [
            _record(1, tokens=(100, 20, 3000, 0)),
            _record(2, CallPurpose.BID, tokens=(50, 5, 0, 3000)),
            _record(9, tokens=(100, 20, 0, 3000)),  # after the close
            _record(11, CallPurpose.MEMO, tokens=(100, 200, 0, 3000)),
            _record(3, CallPurpose.SUMMARY, tokens=(700, 70, 0, 0)),
        ]
        run = _run("D-prime", Try(PLAN.id, 1, 1, _kept({"closed_at": _at(8).isoformat()})))
        assert discussion_tokens(run, records) == {(PLAN.id, 1, 1): 3120 + 3055}

    def test_a_keepalive_is_no_part_of_the_discussion(self) -> None:
        """The chair's keep-alive (PR 5e) takes no lease, so the wallet never
        charges it against the discussion's budget."""
        records = [
            _record(1, tokens=(100, 20, 3000, 0)),
            _record(5, CallPurpose.KEEPALIVE, tokens=(329, 0, 0, 3000)),
        ]
        run = _run("D-prime", Try(PLAN.id, 1, 1, _kept({"closed_at": _at(8).isoformat()})))
        assert discussion_tokens(run, records) == {(PLAN.id, 1, 1): 3120}

    def test_a_discussion_that_never_closed_counts_every_turn_and_bid(self) -> None:
        records = [_record(1, tokens=(10, 1, 0, 0)), _record(70, tokens=(10, 1, 0, 0))]
        run = _run("D-prime", Try(PLAN.id, 1, 1, _kept({"closed_at": None}, answer=None)))
        assert discussion_tokens(run, records) == {(PLAN.id, 1, 1): 22}

    def test_a_try_cut_short_is_a_discussion_too(self) -> None:
        records = [_record(1, meeting_try=1, tokens=(10, 1, 0, 0)),
                   _record(20, meeting_try=2, tokens=(20, 2, 0, 0))]
        run = _run("D-prime",
                   Try(PLAN.id, 1, 1, Held(None, errors=("RateLimitError",))),
                   Try(PLAN.id, 1, 2, _kept({"closed_at": _at(30).isoformat()})))
        assert discussion_tokens(run, records) == {(PLAN.id, 1, 1): 11, (PLAN.id, 1, 2): 22}

    def test_the_mark_is_the_cost_close_for_a_room_of_five(self) -> None:
        assert COST_CLOSE_TOKENS == 1_776_000

    def test_each_try_is_projected_to_the_transcripts_a_scored_recall_check_carries(
        self,
    ) -> None:
        """No practice meeting's prefix holds more than three transcripts; a
        scored series' recall check's holds five. The prefix, what a call
        wrote to the cache or read from it, grows with them."""
        records = [
            _record(1, meeting=BRIEFING.id, tokens=(500, 50, 0, 0)),  # carries none
            _record(1, tokens=(100, 20, 3000, 0)),  # the plan carries the briefing's
            _record(2, CallPurpose.BID, tokens=(50, 5, 0, 0)),
            _record(1, meeting=CONTROL.id, tokens=(100, 20, 0, 6000)),  # the control two
        ]
        closed = _kept({"closed_at": _at(8).isoformat()})
        run = _run("D-prime", *(Try(m.id, 1, 1, closed) for m in (BRIEFING, PLAN, CONTROL)))
        assert projected_tokens(run, records, SERIES) == {
            (PLAN.id, 1, 1): 3175 + 3000 * 4,
            (CONTROL.id, 1, 1): 6120 + 6000 * 3 // 2,
        }

    def test_a_scored_recall_check_carries_the_briefing_and_every_plan(self) -> None:
        assert SCORED_TRANSCRIPTS == 5


def _practice_packets() -> tuple[MemoPacket, RecallPacket]:
    """A practice memo packet and recall packet, as the judge read them."""
    assert PLAN.plan_key is not None and RECALL.recall_key is not None
    return (
        MemoPacket("m1", SERIES.organisation, (BRIEFING.message, PLAN.message), PLAN.plan_key,
                   "A memo of a few words."),
        RecallPacket("r1", SERIES.organisation, RECALL.recall_key, "R1: the answer. R2: another."),
    )


class TestTheJudgesProjection:
    """PR 6b's review, F-8: a scored packet holds more of the operator's
    messages than a practice one, so the scored judging is projected at the
    scored packets' own sizes (PR 6c)."""

    MEMO, RECALL_PACKET = _practice_packets()

    def _judged(self, packet: Packet, output_tokens: int) -> CallRecord:
        """The judge's call for *packet*, one input token a character of its request."""
        recall = isinstance(packet, RecallPacket)
        system = PROMPTS.recall if recall else PROMPTS.memo
        return _record(0, CallPurpose.JUDGE, arm="", meeting=packet.id, model=JUDGE_MODEL,
                       kind=MeetingKind.RECALL if recall else MeetingKind.PLAN,
                       tokens=(len(system) + len(packet_text(packet)), output_tokens, 0, 0))

    def _project(self, *records: CallRecord) -> Projection:
        packets: dict[str, Packet] = {"m1": self.MEMO, "r1": self.RECALL_PACKET}
        return project_judging(records, packets, PROMPTS, SCORED)

    def test_the_scored_count_is_every_arms_memos_and_recall_checks(self) -> None:
        assert (SCORED_MEMO_PACKETS, SCORED_RECALL_PACKETS) == (100, 25)

    def test_each_scored_packet_is_read_at_its_own_size(self) -> None:
        """Its request's characters at the practice batch's tokens a
        character, with the practice answers' mean length standing in for
        the answer not yet written; the output is the practice batch's."""
        projection = self._project(self._judged(self.MEMO, 1000),
                                   self._judged(self.RECALL_PACKET, 400))
        expected = 0.0
        for series in SCORED:
            for number, meeting in enumerate(series.meetings):
                upto = tuple(m.message for m in series.meetings[: number + 1])
                if meeting.plan_key is not None:
                    text = memo_packet_text(MemoPacket(
                        "", series.organisation, upto, meeting.plan_key, self.MEMO.memo,
                    ))
                    read, output = len(PROMPTS.memo) + len(text), 1000
                elif meeting.recall_key is not None:
                    text = recall_packet_text(RecallPacket(
                        "", series.organisation, meeting.recall_key, self.RECALL_PACKET.reply,
                    ))
                    read, output = len(PROMPTS.recall) + len(text), 400
                else:
                    continue
                expected += 5 * (read * 5.00 + output * 25.00) / 1_000_000  # one per arm
        assert projection.scored == pytest.approx(expected)
        assert projection.fits

    def test_a_scored_memo_packet_costs_more_to_read_than_a_practice_one(self) -> None:
        practice = self._judged(self.MEMO, 0)
        projection = self._project(practice, self._judged(self.RECALL_PACKET, 0))
        assert projection.memo_packet is not None
        assert projection.memo_packet > price_call(practice)

    def test_it_names_the_mean_output_at_which_the_scored_judging_reaches_its_cap(
        self,
    ) -> None:
        """Longer packets may take more thinking, which no practice packet shows."""
        projection = self._project(self._judged(self.MEMO, 1000),
                                   self._judged(self.RECALL_PACKET, 400))
        assert projection.scored is not None
        reading = projection.scored - (100 * 1000 + 25 * 400) * 25.00 / 1_000_000
        assert projection.output_headroom == int((25 - reading) / (125 * 25.00 / 1_000_000))

    def test_a_projection_over_the_cap_does_not_fit(self) -> None:
        projection = self._project(self._judged(self.MEMO, 10_400),
                                   self._judged(self.RECALL_PACKET, 1))
        assert projection.scored is not None and projection.scored > 25
        assert not projection.fits

    def test_with_no_packet_of_a_kind_there_is_no_projection(self) -> None:
        projection = self._project(self._judged(self.MEMO, 4_000))
        assert (projection.scored, projection.output_headroom) == (None, None)
        assert not projection.fits

    def test_with_no_packet_of_a_kind_the_other_kind_is_still_projected(self) -> None:
        """A batch its cap stopped before any recall packet still shows what
        a scored memo packet would cost, as the report did before PR 6c."""
        alone = self._project(self._judged(self.MEMO, 4_000))
        both = self._project(self._judged(self.MEMO, 4_000), self._judged(self.RECALL_PACKET, 1))
        assert alone.memo_packet == pytest.approx(both.memo_packet)
        assert alone.recall_packet is None


class TestTheUsageTotals:
    def test_each_models_calls_and_tokens_are_totalled_with_their_dollars(self) -> None:
        records = [
            _record(1, tokens=(100, 10, 1000, 0)),
            _record(2, tokens=(200, 20, 0, 1000)),
            _record(3, CallPurpose.JUDGE, arm="", model=JUDGE_MODEL, tokens=(500, 50, 0, 0)),
        ]
        usage = usage_totals(CallLog(tuple(records), (_failed(0.5), _failed(4, "CancelledError"))))
        assert usage["models"][ARMS_MODEL] == {
            "calls": 2, "input_tokens": 300, "output_tokens": 30, "cache_write_tokens": 1000,
            "cache_read_tokens": 1000,
            "dollars": pytest.approx(price_call(records[0]) + price_call(records[1])),
        }
        assert usage["models"][JUDGE_MODEL]["calls"] == 1
        assert usage["failed_calls"] == {"CancelledError": 1, "RateLimitError": 1}
        assert usage["window"] == {"first": _at(0.5).isoformat(), "last": _at(4).isoformat()}

    def test_a_model_the_fixed_table_does_not_list_is_totalled_but_not_priced(self) -> None:
        """Offline every call is the mock provider's, which the table does not price."""
        usage = usage_totals(CallLog((_record(1, model="offline", tokens=(10, 1, 0, 0)),), ()))
        assert usage["models"]["offline"]["input_tokens"] == 10
        assert usage["models"]["offline"]["dollars"] is None
        assert "offline: 1 calls" in summary({**TestTheReport()._build(), "usage": usage})
        assert "not priced" in summary({**TestTheReport()._build(), "usage": usage})

    def test_no_calls_give_no_window(self) -> None:
        assert usage_totals(CallLog((), ()))["window"] is None


class TestRecallMarks:
    def test_the_judges_marks_are_read_back_to_their_arms(self) -> None:
        """Check 2's first half: D's chair can answer only from memory."""
        seal = Seal(
            packets={"p1": MemoRef("D", SERIES.id, RECALL.id), "p2": MemoRef("B", SERIES.id,
                                                                             RECALL.id),
                     "p3": MemoRef("D", SERIES.id, PLAN.id)},
            missing=(), cuts={},
        )
        judged = Judged(memo_scores={}, recall_marks={"p1": {"R1": True, "R2": False},
                                                      "p2": {"R1": False, "R2": False}},
                        spend=0.1, cap_reached=False, left=())
        assert recall_marks_by_arm(judged, seal) == {
            "B": {"R1": False, "R2": False}, "D": {"R1": True, "R2": False},
        }


class TestTheReport:
    def _build(self, **overrides: Any) -> dict[str, Any]:
        record = {"closed_at": _at(8).isoformat(), "closed_by": "idle",
                  "failures": ["memo_turn_went_on"]}
        runs = {"D-prime": _run("D-prime", Try(BRIEFING.id, 1, 1, _kept({"closed_at": None,
                                                                          "closed_by": "vote",
                                                                          "failures": []},
                                                                         answer=None)),
                                Try(PLAN.id, 1, 1, _kept(record, answer=None)))}
        calls = {"D-prime": CallLog((
            _record(1, prefix=_PREFIX, tokens=(10, 1, 0, 2000)),  # read, never wrote
            _record(12, CallPurpose.MEMO, prefix=_PREFIX, tokens=(10, 1, 2000, 0)),
        ), ())}
        arguments: dict[str, Any] = {
            "series": SERIES, "runs": runs, "calls": calls,
            "written": {("D-prime", SERIES.id, PLAN.id, 1, 1): _PREFIX},
            "everything": CallLog(tuple(calls["D-prime"].records), ()),
            "judge_calls": None, "judged": None, "seal": None, "projection": None,
        }
        arguments.update(overrides)
        return build(**arguments)

    def test_check_3s_findings_are_named(self) -> None:
        report = self._build()
        findings = report["check_3"]["findings"]
        assert len(findings) == 2
        assert "should write the prefix and read nothing" in findings[0]
        assert "should read the prefix and write nothing" in findings[1]

    def test_check_3_holds_each_d_prime_try_to_the_prefix_the_harness_wrote(self) -> None:
        written = {("D-prime", SERIES.id, PLAN.id, 1, 1): "b" * 64}
        findings = self._build(written=written)["check_3"]["findings"]
        assert any("not the prefix the harness wrote" in finding for finding in findings)

    def test_each_meetings_close_failures_and_missing_answer_are_listed(self) -> None:
        rows = self._build()["meetings"]
        assert rows == [
            {"arm": "D-prime", "meeting": BRIEFING.id, "attempt": 1, "try": 1,
             "cut_short": False, "errors": [], "closed_by": "vote", "failures": [],
             "answer_missing": False},
            {"arm": "D-prime", "meeting": PLAN.id, "attempt": 1, "try": 1,
             "cut_short": False, "errors": [], "closed_by": "idle",
             "failures": ["memo_turn_went_on"], "answer_missing": True},
        ]

    def test_a_try_cut_short_is_listed_with_its_errors_and_no_answer_missing(self) -> None:
        """It is held again; what it lacks is not a missing answer."""
        runs = {"C": _run("C", Try(PLAN.id, 1, 1, Held(None, errors=("RateLimitError",))))}
        rows = self._build(runs=runs, calls={"C": CallLog((), ())}, written={})["meetings"]
        assert rows == [{"arm": "C", "meeting": PLAN.id, "attempt": 1, "try": 1,
                         "cut_short": True, "errors": ["RateLimitError"], "closed_by": None,
                         "failures": [], "answer_missing": False}]

    def test_the_d_prime_discussions_are_set_against_the_cost_close(self) -> None:
        report = self._build()
        assert report["d_prime_discussions"] == {
            "cost_close_tokens": COST_CLOSE_TOKENS,
            "scored_transcripts": SCORED_TRANSCRIPTS,
            "tries": [{"meeting": PLAN.id, "attempt": 1, "try": 1, "tokens": 2011,
                       "projected": 2011 + 2000 * 4}],
        }

    def test_a_try_at_the_close_measured_or_projected_is_flagged(self) -> None:
        calls = {"D-prime": CallLog((_record(1, prefix=_PREFIX, tokens=(10, 1, 0, 400_000)),), ())}
        text = summary(self._build(calls=calls, everything=calls["D-prime"]))
        assert "400 011 tokens; projected 2 000 011, OVER the close" in text

    def test_without_judging_the_judge_is_not_reported(self) -> None:
        report = self._build()
        assert report["judge"] is None and report["recall_marks"] is None

    def test_the_judge_is_reported_with_its_projection(self) -> None:
        judge_calls = CallLog((
            _record(0, CallPurpose.JUDGE, arm="", meeting="m1", model=JUDGE_MODEL,
                    tokens=(3000, 6000, 0, 0)),
            _record(0, CallPurpose.JUDGE, arm="", meeting="r1", model=JUDGE_MODEL,
                    kind=MeetingKind.RECALL, tokens=(1000, 900, 0, 0)),
        ), ())
        judged = Judged({}, {"r1": {"R1": True}}, spend=0.2, cap_reached=False, left=())
        seal = Seal({"r1": MemoRef("D", SERIES.id, RECALL.id)}, (), {})
        projection = Projection(memo_packet=0.04, recall_packet=0.01, scored=4.25,
                                output_headroom=7_213)
        report = self._build(judge_calls=judge_calls, judged=judged, seal=seal,
                             projection=projection)
        assert report["judge"]["calls"] == 2
        assert report["judge"]["output_tokens_max"] == 6000
        assert report["judge"]["max_tokens"] == MAX_TOKENS
        assert report["judge"]["projection"] == {
            "memo_packet": 0.04, "recall_packet": 0.01, "scored_memo_packets": 100,
            "scored_recall_packets": 25, "scored": 4.25, "cap": 25.0, "fits": True,
            "output_headroom": 7_213,
        }
        assert report["recall_marks"] == {"D": {"R1": True}}
        text = summary(report)
        assert "projected at the scored packets' own sizes" in text
        assert "$4.25 in all, within the $25 cap" in text
        assert "only if its 125 calls average 7 213 output tokens or more" in text
        assert "the practice batch's largest answer used 6 000" in text

    def test_the_summary_names_what_the_run_shows(self) -> None:
        text = summary(self._build())
        assert "Check 3: 2 findings" in text
        assert "should write the prefix and read nothing" in text
        assert "1 776 000" in text
        assert "memo_turn_went_on" in text
        assert ARMS_MODEL in text
        assert "not judged" in text
