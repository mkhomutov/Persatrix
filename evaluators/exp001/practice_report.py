"""What an EXP-001 practice run shows, as a report.

The practice series runs first, as often as needed, until the eight checks
of pre-registration §3 pass; the practice run is the evidence that they do.
It also settles what the earlier harness PRs left to it. This module turns a
practice run's kept tries and call records into those figures:

- **Check 3**: :func:`evaluators.exp001.arm_d_prime.check_cache` over every
  arm's calls, with each D′ try's prefix as the harness wrote it. It names a
  memo turn that wrote the prefix again after a quiet spell outlasted the
  cache entry, the gap PR 5d left for the practice run to confirm.
- **D′'s discussions against the cost close**: each D′ discussion's tokens,
  counted as the wallet counts them, against the 1 776 000 at which the cost
  close comes for a room of five. No practice meeting's prefix holds more
  than three transcripts, and a scored series' recall check's holds five,
  so each is projected to five as well; a try over the mark is flagged.
- **What each meeting recorded**, for checks 5 and 6 among others: what
  closed each discussion, the failures its record names, and whether its
  answer is missing. A lease a spending limit refused stops the run instead.
- **Check 2's first half**: the judge's marks on each arm's recall check.
  Each meeting is a new channel, so D's chair can answer only from memory.
- **The judge**: its calls' output tokens against their 16 000 limit, and
  the scored judging's spend projected from the practice batch, 100 memo
  packets and 25 recall packets at the practice means, against the $25 cap.
- **Check 4 and the provider's usage report**: every call's tokens,
  totalled by model and priced with the fixed table, the failed calls
  counted, and the window from the first call to the last, to compare by
  hand with the provider's own report for the run's API key. A set-aside
  log the harness cannot read is named, its calls left out.

Building the report reads no file: the practice run gathers what it reads.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from evaluators.exp001.arm_d_prime import TryKey, check_cache
from evaluators.exp001.attempts import SeriesRun
from evaluators.exp001.costs import ARMS, PRICES, CallPurpose, CallRecord, price_call
from evaluators.exp001.judge import JUDGING_CAP, MAX_TOKENS, Judged
from evaluators.exp001.materials import (
    PLANS_PER_SCORED_SERIES,
    SCORED_SERIES,
    MeetingKind,
    Series,
)
from evaluators.exp001.pairs import Kept
from evaluators.exp001.rating import Seal
from evaluators.exp001.runtime import CallLog

# The discussion budget is raised to 2 000 000 tokens, and the cost close
# comes once the synthesis reserve for a room of five is left: at 1 776 000
# (the harness doc's PR 5d section).
COST_CLOSE_TOKENS = 1_776_000
# A scored series' recall check carries the transcripts of the briefing and
# every plan: the most a D′ prefix holds.
SCORED_TRANSCRIPTS = PLANS_PER_SCORED_SERIES + 1
# What the scored judging asks: every arm's memos and recall checks.
SCORED_MEMO_PACKETS = len(SCORED_SERIES) * PLANS_PER_SCORED_SERIES * len(ARMS)
SCORED_RECALL_PACKETS = len(SCORED_SERIES) * len(ARMS)
# The calls of a discussion: the advisers' turns and their salience bids.
_DISCUSSION = frozenset({CallPurpose.REPLY, CallPurpose.BID})

Tokens = dict[tuple[str, int, int], int]


@dataclass(frozen=True)
class Projection:
    """The scored judging's spend, from the practice batch's mean per packet."""

    memo_packet: float | None  # None when the batch judged no packet of that kind
    recall_packet: float | None
    scored: float | None
    cap: float = JUDGING_CAP

    @property
    def fits(self) -> bool:
        return self.scored is not None and self.scored <= self.cap


def discussion_tokens(run: SeriesRun[Kept], records: Iterable[CallRecord]) -> Tokens:
    """Each try's discussion tokens, as the wallet counts them against the
    discussion's budget: every input token a turn or bid carried, cached
    ones included, and its output, for the calls begun before the close.
    Keyed by meeting, attempt and try; a try that made no such call has none."""
    return {key: sum(map(_counted, calls)) for key, calls in _discussions(run, records).items()}


def projected_tokens(
    run: SeriesRun[Kept], records: Iterable[CallRecord], series: Series,
) -> Tokens:
    """Each try's discussion tokens with the prefix a scored series' recall
    check carries: what its calls wrote to the cache or read from it, the
    prefix, scaled from the transcripts the try carried, one for each
    meeting before it, to :data:`SCORED_TRANSCRIPTS`, at the same number of
    calls. A briefing carries none, so it has none."""
    before = {m.id: number for number, m in enumerate(series.meetings)}
    projected: Tokens = {}
    for key, calls in _discussions(run, records).items():
        carried = before.get(key[0], 0)
        if carried:
            prefix = sum(r.cache_write_tokens + r.cache_read_tokens for r in calls)
            projected[key] = (
                sum(map(_counted, calls)) + prefix * (SCORED_TRANSCRIPTS - carried) // carried
            )
    return projected


def _discussions(
    run: SeriesRun[Kept], records: Iterable[CallRecord],
) -> dict[tuple[str, int, int], list[CallRecord]]:
    """Each try's turns and bids begun before its close, keyed as :data:`Tokens` is."""
    closes: dict[tuple[str, int, int], dt.datetime | None] = {}
    for t in run.tries:
        closed = None if t.held.result is None else t.held.result.record.get("closed_at")
        closes[(t.meeting, t.attempt, t.meeting_try)] = (
            dt.datetime.fromisoformat(closed) if closed else None
        )
    calls: dict[tuple[str, int, int], list[CallRecord]] = defaultdict(list)
    for record in records:
        key = (record.meeting, record.attempt, record.meeting_try)
        close = closes.get(key)
        if record.arm != run.arm or record.purpose not in _DISCUSSION:
            continue
        if close is not None and record.started_at >= close:
            continue
        calls[key].append(record)
    return calls


def _counted(record: CallRecord) -> int:
    """A call's tokens as the wallet counts them: every input token, cached
    or not, and its output."""
    return (
        record.input_tokens + record.cache_write_tokens + record.cache_read_tokens
        + record.output_tokens
    )


def project_judging(records: Iterable[CallRecord]) -> Projection:
    """The scored judging's spend at the practice batch's mean cost of a memo
    packet and of a recall packet: every call a packet made, priced."""
    cost: dict[str, float] = defaultdict(float)
    recall: set[str] = set()
    for record in records:
        cost[record.meeting] += price_call(record)
        if record.meeting_kind is MeetingKind.RECALL:
            recall.add(record.meeting)
    memos = [c for packet, c in cost.items() if packet not in recall]
    recalls = [c for packet, c in cost.items() if packet in recall]
    memo = statistics.fmean(memos) if memos else None
    reply = statistics.fmean(recalls) if recalls else None
    scored = (
        None if memo is None or reply is None
        else SCORED_MEMO_PACKETS * memo + SCORED_RECALL_PACKETS * reply
    )
    return Projection(memo, reply, scored)


def usage_totals(calls: CallLog) -> dict[str, Any]:
    """Every call's tokens by model, priced, the failed calls by class, and
    the window from the first call's start to the last's, UTC. A model the
    fixed table does not list, such as the offline mock's, is not priced."""
    models: dict[str, dict[str, Any]] = {}
    for record in calls.records:
        totals = models.setdefault(record.model, {
            "calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0,
            "cache_read_tokens": 0, "dollars": 0.0 if record.model in PRICES else None,
        })
        totals["calls"] += 1
        totals["input_tokens"] += record.input_tokens
        totals["output_tokens"] += record.output_tokens
        totals["cache_write_tokens"] += record.cache_write_tokens
        totals["cache_read_tokens"] += record.cache_read_tokens
        if totals["dollars"] is not None:
            totals["dollars"] += price_call(record)
    starts = [r.started_at for r in calls.records] + [f.started_at for f in calls.failures]
    return {
        "models": models,
        "failed_calls": dict(sorted(Counter(f.error for f in calls.failures).items())),
        "window": {"first": min(starts).isoformat(), "last": max(starts).isoformat()}
        if starts else None,
    }


def recall_marks_by_arm(judged: Judged, seal: Seal) -> dict[str, dict[str, bool]]:
    """The judge's marks on each arm's recall check, read back through the seal."""
    return {seal.packets[packet].arm: marks for packet, marks in judged.recall_marks.items()}


def meeting_rows(runs: Mapping[str, SeriesRun[Kept]], series: Series) -> list[dict[str, Any]]:
    """Each try as its record left it: what closed the discussion, the
    failures it names, and whether the answer asked for is missing."""
    briefings = {m.id for m in series.meetings if m.kind is MeetingKind.BRIEFING}
    rows = []
    for arm, run in runs.items():
        for t in run.tries:
            kept = t.held.result
            record = {} if kept is None else kept.record
            rows.append({
                "arm": arm, "meeting": t.meeting, "attempt": t.attempt, "try": t.meeting_try,
                "cut_short": t.held.cut_short, "errors": list(t.held.errors),
                "closed_by": record.get("closed_by"), "failures": list(record.get("failures", ())),
                "answer_missing": (
                    t.meeting not in briefings and not t.held.cut_short
                    and (kept is None or kept.answer is None)
                ),
            })
    return rows


def build(
    *,
    series: Series,
    runs: Mapping[str, SeriesRun[Kept]],
    calls: Mapping[str, CallLog],
    written: Mapping[TryKey, str],
    everything: CallLog,
    unread: Sequence[str] = (),
    judge_calls: CallLog | None,
    judged: Judged | None,
    seal: Seal | None,
) -> dict[str, Any]:
    """The report of a practice run: *runs* and *calls* by arm, the pairs held
    to the end; *written*, the prefixes the harness wrote for D′'s tries;
    *everything*, every arm call the run made, pairs set aside included,
    but for those of *unread*, the set-aside logs it could not read; and the
    judge's calls and marks, none when the run was not judged."""
    records = [r for log in calls.values() for r in log.records]
    failures = [f for log in calls.values() for f in log.failures]
    d_prime = runs.get("D-prime")
    projected = (
        {} if d_prime is None else projected_tokens(d_prime, calls["D-prime"].records, series)
    )
    judge_records = judge_calls.records if judge_calls is not None else ()
    judge_failures = judge_calls.failures if judge_calls is not None else ()
    return {
        "series": series.id,
        "arms": list(runs),
        "meetings": meeting_rows(runs, series),
        "check_3": {"findings": check_cache(records, failures=failures, written=written)},
        "d_prime_discussions": None if d_prime is None else {
            "cost_close_tokens": COST_CLOSE_TOKENS,
            "scored_transcripts": SCORED_TRANSCRIPTS,
            "tries": [
                {"meeting": meeting, "attempt": attempt, "try": meeting_try, "tokens": tokens,
                 "projected": projected.get((meeting, attempt, meeting_try))}
                for (meeting, attempt, meeting_try), tokens
                in discussion_tokens(d_prime, calls["D-prime"].records).items()
            ],
        },
        "recall_marks": (
            None if judged is None or seal is None else recall_marks_by_arm(judged, seal)
        ),
        "judge": None if judge_calls is None or judged is None else _judge(judge_calls, judged),
        "usage": {
            **usage_totals(CallLog(
                (*everything.records, *judge_records), (*everything.failures, *judge_failures),
            )),
            "unread": list(unread),
        },
    }


def _judge(calls: CallLog, judged: Judged) -> dict[str, Any]:
    output = [r.output_tokens for r in calls.records]
    projection = project_judging(calls.records)
    return {
        "calls": len(calls.records),
        "failed_calls": len(calls.failures),
        "output_tokens_max": max(output, default=0),
        "output_tokens_mean": statistics.fmean(output) if output else None,
        "max_tokens": MAX_TOKENS,
        "spend": judged.spend,
        "cap_reached": judged.cap_reached,
        "left": list(judged.left),
        "projection": {
            "memo_packet": projection.memo_packet,
            "recall_packet": projection.recall_packet,
            "scored_memo_packets": SCORED_MEMO_PACKETS,
            "scored_recall_packets": SCORED_RECALL_PACKETS,
            "scored": projection.scored,
            "cap": projection.cap,
            "fits": projection.fits,
        },
    }


def summary(report: Mapping[str, Any]) -> str:
    """The report in plain words, one section per thing the run shows."""
    lines = [f"EXP-001 practice run, series {report['series']}: arms {', '.join(report['arms'])}",
             "", "Meetings:"]
    for row in report["meetings"]:
        lines.append(f"  {row['arm']} {row['meeting']}, attempt {row['attempt']}, "
                     f"try {row['try']}: {_meeting_words(row)}")
    findings = report["check_3"]["findings"]
    lines += ["", f"Check 3: {len(findings)} finding{'' if len(findings) == 1 else 's'}"
              if findings else "Check 3: no findings"]
    lines += [f"  - {finding}" for finding in findings]
    discussions = report["d_prime_discussions"]
    if discussions is not None:
        close = discussions["cost_close_tokens"]
        lines += ["", f"D′ discussions, whose cost close comes at {_n(close)} tokens, each also "
                      f"projected to the {discussions['scored_transcripts']} transcripts a scored "
                      "series' recall check carries:"]
        lines += [f"  {t['meeting']}, attempt {t['attempt']}, try {t['try']}: "
                  f"{_n(t['tokens'])} tokens{_over(t['tokens'], close)}"
                  + ("" if t["projected"] is None
                     else f"; projected {_n(t['projected'])}{_over(t['projected'], close)}")
                  for t in discussions["tries"]]
    lines += ["", *_judge_words(report)]
    lines += ["", *_usage_words(report["usage"])]
    return "\n".join(lines) + "\n"


def _meeting_words(row: Mapping[str, Any]) -> str:
    if row["cut_short"]:
        return f"cut short ({', '.join(row['errors']) or 'did not start'})"
    words = [f"closed by {row['closed_by']}" if row["closed_by"] else "held"]
    if row["failures"]:
        words.append(f"failures: {', '.join(row['failures'])}")
    if row["answer_missing"]:
        words.append("answer missing")
    return "; ".join(words)


def _judge_words(report: Mapping[str, Any]) -> list[str]:
    judge = report["judge"]
    if judge is None:
        return ["The judge: not judged"]
    projection = judge["projection"]
    lines = [
        f"The judge: {judge['calls']} calls, {judge['failed_calls']} failed; output tokens "
        f"up to {_n(judge['output_tokens_max'])} of {_n(judge['max_tokens'])}; "
        f"spend ${judge['spend']:.2f}" + ("; its cap was reached" if judge["cap_reached"] else ""),
    ]
    if projection["scored"] is None:
        lines.append("  no projection: the batch judged no memo packet or no recall packet")
    else:
        verdict = "within" if projection["fits"] else "OVER"
        lines.append(
            f"  scored judging, projected: {projection['scored_memo_packets']} × "
            f"${projection['memo_packet']:.4f} + {projection['scored_recall_packets']} × "
            f"${projection['recall_packet']:.4f} = ${projection['scored']:.2f}, "
            f"{verdict} the ${projection['cap']:.0f} cap",
        )
    marks = report["recall_marks"] or {}
    lines.append("Recall checks as the judge marked them (check 2: D answers only from memory):")
    lines += [f"  {arm}: " + ", ".join(f"{q} {'right' if right else 'wrong'}"
                                       for q, right in answers.items())
              for arm, answers in sorted(marks.items())]
    return lines


def _usage_words(usage: Mapping[str, Any]) -> list[str]:
    lines = ["Usage, to compare with the provider's report for this run's API key:"]
    window = usage["window"]
    lines.append(f"  from {window['first']} to {window['last']}" if window else "  no calls")
    for model, t in sorted(usage["models"].items()):
        lines.append(
            f"  {model}: {_n(t['calls'])} calls; input {_n(t['input_tokens'])}, output "
            f"{_n(t['output_tokens'])}, cache write {_n(t['cache_write_tokens'])}, cache read "
            f"{_n(t['cache_read_tokens'])} tokens; "
            + ("not priced" if t["dollars"] is None else f"${t['dollars']:.4f}"),
        )
    failed = usage["failed_calls"]
    if failed:
        lines.append("  failed calls: " + ", ".join(f"{e} {n}" for e, n in failed.items()))
    if usage.get("unread"):
        lines.append("  set-aside logs the harness cannot read, their calls left out of these "
                     "totals: " + ", ".join(usage["unread"]))
    return lines


def _over(tokens: int, close: int) -> str:
    return ", OVER the close" if tokens >= close else ""


def _n(value: int) -> str:
    """A count as the documents write it, thousands apart by a space."""
    return f"{value:,}".replace(",", " ")
