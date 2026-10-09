"""What an EXP-001 scored run shows, as a report.

The scored run's report says how the run ended, by the rules of
pre-registration §3 and part 2 §6, and gives what the decision and the
people who score need from it:

- **The outcome.** Complete; incomplete, when the $150 cap stopped it, when
  fewer than four series were kept, or after a third harness fault; or
  stopped by a harness fault, until its fix merges and the next window
  starts. A judge that reaches its own cap leaves the run complete: part 2
  §4 reads that as an inconclusive score, not an incomplete run.
- **Each window**: when its seven days opened, the fix it started after,
  the fault that closed it, how its meetings ended, and its real spend.
- **The window whose meetings ended**: the series every arm's comparisons
  keep, those dropped and those not held; each try as its record left it,
  with no memo or transcript text, those of a pair the cap or the seven days
  stopped included; and each arm's dollars per plan for every series it held
  to the end, at the fixed table and with bids and summaries repriced (part
  2 §7).
- **Real spend** against the cap, every window counted, and the calls a
  closed window holds that the fixed table cannot price; the judge's spend
  against its own cap, or the provider error that stopped it; and every
  call's tokens by model, to compare by hand with the provider's usage
  report.
- **When scoring is due**: 21 days after the last scored meeting ended
  (part 2 §4): the last try of the last series kept, not a later one of a
  series never held in every arm.

The report never reads the seal, so it names no arm's scores. Building it
reads no file: the scored run gathers what it reads.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

from evaluators.exp001.attempts import HARNESS_FAULTS, MIN_SERIES, SeriesRun
from evaluators.exp001.costs import (
    PRICES,
    REPRICE_MODEL,
    REPRICING_PRICES,
    SPEND_CAP,
    dollars_per_plan,
    repriced,
)
from evaluators.exp001.deployment import Alias
from evaluators.exp001.judge import JUDGING_CAP, Judged
from evaluators.exp001.materials import Series
from evaluators.exp001.pairs import Kept
from evaluators.exp001.practice_report import (
    meeting_rows,
    meeting_words,
    usage_totals,
    usage_words,
)
from evaluators.exp001.runtime import CallLog

SCORING_DAYS = dt.timedelta(days=21)

COMPLETE = "complete"
INCOMPLETE = "incomplete"
STOPPED = "stopped"
IN_PROGRESS = "in progress"
CAP_REACHED = f"the ${SPEND_CAP:.0f} cap was reached"
TOO_FEW = "fewer than four series were kept"
THIRD_FAULT = "a third harness fault"

# Why a window's meetings ended before every series was held, as run.json keeps it.
_STOPPED_BY = {
    "cap": CAP_REACHED,
    "window": "its seven days were over",
    "series": "fewer than four series could still be kept",
}


def outcome(windows: Sequence[Mapping[str, Any]]) -> tuple[str, str | None]:
    """How the run stands, and why, from its windows as ``run.json`` keeps them."""
    if sum(1 for w in windows if w.get("fault")) >= HARNESS_FAULTS:
        return INCOMPLETE, THIRD_FAULT
    last = windows[-1]
    if last.get("fault"):
        number = last["window"]
        return STOPPED, (
            f"a harness fault stopped window {number}; once its fix has merged through a "
            f"reviewed PR, window {number + 1} starts with --fixed-by naming that PR"
        )
    ended = last.get("ended")
    if ended is None:
        return IN_PROGRESS, None
    if ended["stopped_by"] == "cap":
        return INCOMPLETE, CAP_REACHED
    if len(ended["kept"]) < MIN_SERIES:
        return INCOMPLETE, TOO_FEW
    return COMPLETE, None


def is_scored(window: Mapping[str, Any]) -> bool:
    """Whether the window's answers are drawn and judged: no fault closed it,
    and its meetings ended with four series or more kept, the cap not reached."""
    ended = window.get("ended")
    return (
        not window.get("fault") and ended is not None and ended["stopped_by"] != "cap"
        and len(ended["kept"]) >= MIN_SERIES
    )


def build(
    *,
    alias: Alias,
    windows: Sequence[Mapping[str, Any]],
    runs: Sequence[SeriesRun[Kept]],
    calls: Mapping[tuple[str, str], CallLog],
    series: Sequence[Series],
    priced: bool,
    spend: Sequence[float | None],
    everything: CallLog,
    stopped: Sequence[SeriesRun[Kept]] = (),
    unpriced: Sequence[Mapping[str, Any]] = (),
    unread: Sequence[str] = (),
    judge_unread: Sequence[str] = (),
    judge_calls: CallLog | None = None,
    judged: Judged | None = None,
    judging_stopped: str | None = None,
) -> dict[str, Any]:
    """The report of a scored run held on *alias*, in *windows*.

    *runs* are the pairs the last window held to the end, once its meetings
    ended and no fault closed it, and *calls* each one's calls, by series and
    arm; *stopped* are the pairs there the cap or the seven days stopped
    partway, with their tries so far. *spend* is each window's real spend,
    but for the calls of *unpriced*, by window and model, which the fixed
    table cannot price; nothing is priced unless *priced*. *everything* is
    every call of the run, the arms' and the judge's, in every window, but
    for the arms' logs of *unread* and the judge's of *judge_unread*, which
    the harness could not read. *judge_calls* and *judged* are the last
    window's scored judging, when it was judged, and *judging_stopped* the
    provider error that stopped it, when one did.
    """
    state, why = outcome(windows)
    last = windows[-1]
    ended = None if last.get("fault") else last.get("ended")
    dollars = priced and ended is not None
    return {
        "provider": alias.provider,
        "model": alias.model,
        "outcome": state,
        "why": why,
        "windows": [{**w, "spend": s} for w, s in zip(windows, spend, strict=True)],
        "series": None if ended is None else {
            "kept": list(ended["kept"]), "dropped": list(ended["dropped"]),
            "not_held": list(ended["not_held"]),
        },
        "meetings": _meetings([*runs, *stopped], series),
        "dollars_per_plan": _dollars(runs, calls, repricing=False) if dollars else None,
        "dollars_per_plan_repriced": _dollars(runs, calls, repricing=True) if dollars else None,
        "real_spend": {
            "dollars": sum(s or 0.0 for s in spend) if priced else None,
            "cap": SPEND_CAP,
            "unread": list(unread),
            "unpriced": [dict(u) for u in unpriced],
        },
        "judging": (
            None if judge_calls is None or judged is None else _judging(judge_calls, judged)
        ),
        "judging_stopped": judging_stopped,
        "scoring_due": (
            None if state != COMPLETE or ended is None or ended["last_scored_meeting_at"] is None
            else _scoring_due(ended)
        ),
        "usage": {**usage_totals(everything), "unread": [*unread, *judge_unread]},
    }


def _meetings(runs: Sequence[SeriesRun[Kept]], series: Sequence[Series]) -> list[dict[str, Any]]:
    """Each try as its record left it, series by series in the order held."""
    rows: list[dict[str, Any]] = []
    for s in series:
        held = {run.arm: run for run in runs if run.series == s.id}
        rows += [{"series": s.id, **row} for row in meeting_rows(held, s)]
    return rows


def _dollars(
    runs: Sequence[SeriesRun[Kept]], calls: Mapping[tuple[str, str], CallLog], *,
    repricing: bool,
) -> dict[str, dict[str, float]]:
    """Each arm's dollars per plan for every series it held to the end: each
    meeting's finished try, in the attempt that finished."""
    out: dict[str, dict[str, float]] = {}
    for run in runs:
        if run.finished_attempt is None:
            continue  # dropped: its series is in no comparison
        records = calls[(run.series, run.arm)].records
        out.setdefault(run.arm, {})[run.series] = dollars_per_plan(
            repriced(records) if repricing else records, arm=run.arm, series=run.series,
            attempt=run.finished_attempt, tries=run.finished_tries(),
            prices=REPRICING_PRICES if repricing else PRICES,
        )
    return out


def _judging(calls: CallLog, judged: Judged) -> dict[str, Any]:
    """The scored judging by its calls and spend, never its scores."""
    return {
        "calls": len(calls.records),
        "failed_calls": len(calls.failures),
        "spend": judged.spend,
        "cap": JUDGING_CAP,
        "cap_reached": judged.cap_reached,
        "left": list(judged.left),
        "output_tokens_max": max((r.output_tokens for r in calls.records), default=0),
    }


def _scoring_due(ended: Mapping[str, Any]) -> dict[str, str]:
    at = dt.datetime.fromisoformat(ended["last_scored_meeting_at"])
    return {"last_meeting_ended_at": at.isoformat(), "by": (at + SCORING_DAYS).isoformat()}


def summary(report: Mapping[str, Any]) -> str:
    """The report in plain words, one section per thing the run shows."""
    state = report["outcome"] + (f": {report['why']}" if report["why"] else "")
    lines = [f"EXP-001 scored run on {report['provider']} {report['model']}: {state}",
             "", "Windows:"]
    lines += [f"  {_window_words(window)}" for window in report["windows"]]
    spend = report["real_spend"]
    lines += ["", "Real spend: not priced" if spend["dollars"] is None else
              f"Real spend: ${spend['dollars']:.2f} of the ${spend['cap']:.0f} cap, "
              "every window counted"]
    if spend["unpriced"]:
        lines.append("  left out of real spend, as the fixed table cannot price them: " + "; ".join(
            f"window {u['window']}, {u['calls']} call{'' if u['calls'] == 1 else 's'} on "
            f"{u['model']}" for u in spend["unpriced"]
        ))
    if report["series"] is not None:
        lines += ["", "Series " + "; ".join(
            f"{part.replace('_', ' ')}: {', '.join(ids) or 'none'}"
            for part, ids in report["series"].items()
        )]
    if report["meetings"]:
        lines += ["", "Meetings:"]
        lines += [f"  {row['series']} {row['arm']} {row['meeting']}, attempt {row['attempt']}, "
                  f"try {row['try']}: {meeting_words(row)}" for row in report["meetings"]]
    for key, title in (
        ("dollars_per_plan", "Dollars per plan, at the fixed table"),
        ("dollars_per_plan_repriced",
         f"Dollars per plan, bids and memory summaries repriced at {REPRICE_MODEL}"),
    ):
        if report[key]:
            lines += ["", f"{title}:"]
            lines += [f"  {arm}: " + ", ".join(f"{s} ${d:.4f}" for s, d in by_series.items())
                      for arm, by_series in report[key].items()]
    lines += ["", _judging_words(report["judging"], report["judging_stopped"])]
    due = report["scoring_due"]
    if due is not None:
        lines.append(f"Scoring: the last scored meeting ended at {due['last_meeting_ended_at']}; "
                     f"scoring is due by {due['by']}")
    lines += ["", *usage_words(report["usage"])]
    return "\n".join(lines) + "\n"


def _window_words(window: Mapping[str, Any]) -> str:
    after = f", after {window['fixed_by']}" if window.get("fixed_by") else ""
    fault, ended = window.get("fault"), window.get("ended")
    if fault:
        how = f"stopped by a harness fault: {fault['message']}"
    elif ended is not None:
        how = f"its meetings ended at {ended['at']}"
        if ended["stopped_by"]:
            how += f", before every series was held: {_STOPPED_BY[ended['stopped_by']]}"
    else:
        how = "its meetings go on"
    opened = window.get("opened_at")
    words = [f"window {window['window']}{after}: {how}",
             f"opened {opened}" if opened else "no try begun"]
    if window.get("spend") is not None:
        words.append(f"${window['spend']:.2f} real spend")
    return "; ".join(words)


def _judging_words(judging: Mapping[str, Any] | None, stopped: str | None) -> str:
    if stopped is not None:
        return (f"The judge: stopped by a provider error its retries did not clear ({stopped}); "
                "start the run again to resume it")
    if judging is None:
        return "The judge: not judged"
    largest = f"{judging['output_tokens_max']:,}".replace(",", " ")
    words = (
        f"The judge: {judging['calls']} calls, {judging['failed_calls']} failed; spend "
        f"${judging['spend']:.2f} of its ${judging['cap']:.0f} cap; output tokens up to {largest}"
    )
    if judging["cap_reached"]:
        words += "; its cap was reached, so these packets were never judged: " + ", ".join(
            judging["left"],
        )
    return words
