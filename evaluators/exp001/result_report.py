"""EXP-001's scores in plain words: how far scoring has got, then the result.

Before the seal is opened, :func:`progress_words` says how many packets
each rater has left. Once it is open, :func:`summary` gives the result as
:mod:`evaluators.exp001.result` works it out, one section per thing part 2
of the pre-registration names: the rule that fired, or why none did; the
raters' agreement; each check the decision read; what §7 reports without
deciding; each arm's figures; and each meeting.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from evaluators.exp001.arm_a import ARM as ARM_A
from evaluators.exp001.costs import REPRICE_MODEL
from evaluators.exp001.judge import RATER

if TYPE_CHECKING:
    from evaluators.exp001.scoring_run import Progress

# Strategy §4's rules, as part 2 §5 names them.
RULES = {
    "rule 1": "stop adding society features for this job; reposition on the gate alone",
    "rule 2": "the allocator is not the asset",
    "rule 3": "long context replaces the allocator",
    "rule 4": "the full stack is justified",
}
_SHOWN = 5  # packets named when a rater has more left


def progress_words(progress: Progress) -> str:
    """How far scoring has got, the seal still shut."""
    lines = [f"Scoring: {progress.packets} packets, the seal shut"]
    if progress.due is not None:
        lines.append(f"  scoring is due by {progress.due.isoformat()}: it finishes when the "
                     "seal is opened, so open it by then")
    for person in progress.written:
        lines.append(f"  wrote {person}'s blank score sheet: {progress.sheets[person]}")
    for rater, left in progress.left.items():
        named = ", ".join(left[:_SHOWN]) + (", ..." if len(left) > _SHOWN else "")
        if rater == RATER and progress.judge_cap_reached:
            # Its batch is over, but not every packet has its score.
            lines.append(f"  {rater}: stopped by its cap, {len(progress.never_judged)} packets "
                         "never judged: the result will be inconclusive")
        else:
            lines.append(f"  {rater}: "
                         + (f"{len(left)} left ({named})" if left else "every score in"))
    if not progress.complete:
        lines.append("The seal opens only once every score is in")
    elif progress.judge_cap_reached:
        lines.append("Scoring is over: open the seal with --open")
    else:
        lines.append("Every score is in: open the seal with --open")
    return "\n".join(lines) + "\n"


def summary(result: Mapping[str, Any]) -> str:
    """The result in plain words."""
    where = f", window {result['window']}" if result["window"] is not None else ""
    lines = [f"EXP-001 {result['run']} run{where}: the seal was opened at {result['opened_at']}",
             f"Outcome: {_outcome(result['outcome'], result['why'])}"]
    if result["scoring_due"] is not None:
        lines.append(f"Scoring was due by {result['scoring_due']}: "
                     + ("finished on time" if result["on_time"] else "finished LATE"))
    raters = ", ".join(result["raters"])
    lines.append(f"Quality averages {raters}" + (
        "; the judge's cap stopped it before every packet" if result["judge_cap_reached"] else ""
    ))
    lines.append(f"Series kept: {', '.join(result['series'])}")
    lines += ["", *_agreement_words(result["agreement"])]
    deciding = [c for c in result["comparisons"] if c["deciding"]]
    if deciding:
        lines += ["", "The checks, in order:", *(_comparison_words(c) for c in deciding)]
    lines += ["", "Reported, never deciding:",
              f"  the rule with bids and memory summaries repriced at {REPRICE_MODEL}: "
              + _outcome(result["repriced_outcome"], None)]
    alone = result["people_alone"]
    figure = "none" if alone["agreement"] is None else f"{alone['agreement']:.3f}"
    lines.append(f"  the rule on the two people's scores alone: "
                 f"{_outcome(alone['outcome'], alone['why'])} (their agreement {figure})")
    lines += [_comparison_words(c) for c in result["comparisons"] if not c["deciding"]]
    lines += ["", "Each arm:"]
    for arm, figures in result["arms"].items():
        lines += _arm_words(arm, figures)
    lines += ["", "Each meeting:"]
    lines += [f"  {m['series']} {m['arm']} {m['meeting']}: {meeting_words(m)}"
              for m in result["meetings"]]
    return "\n".join(lines) + "\n"


def _outcome(outcome: str, why: str | None) -> str:
    if outcome in RULES:
        return f"{outcome} fires: {RULES[outcome]}"
    return f"{outcome}, no rule fires" + (f": {why}" if why else "")


def _agreement_words(agreement: Mapping[str, Any]) -> list[str]:
    lines = ["Agreement, each pair's per-plan rank correlation averaged over the plans:"]
    for pair in agreement["pairs"]:
        pooled = "none" if pair["pooled"] is None else f"{pair['pooled']:.3f}"
        lines.append(f"  {' and '.join(pair['raters'])}: {_figure(pair['mean'])} over "
                     f"{pair['plans_used']} plans, {pair['plans_skipped']} skipped; "
                     f"over every memo at once {pooled}")
    verdict = "enough to carry a decision" if agreement["sufficient"] else (
        f"below {agreement['floor']}: the scores cannot carry a decision"
    )
    lines.append(f"  their average: {_figure(agreement['mean'])}, {verdict}")
    return lines


def _comparison_words(c: Mapping[str, Any]) -> str:
    interval = c["interval"]
    verdict = "clearly beats" if c["clearly_beats"] else "beats" if c["beats"] else (
        "does not beat"
    )
    words = (f"  {c['x']} against {c['y']}: {c['x']} {verdict} {c['y']}; mean "
             f"{interval['mean']:.3f}, interval {interval['low']:.3f} to {interval['high']:.3f};"
             f" series differences " + ", ".join(f"{d:.3f}" for d in c["differences"]))
    if c["verdict"] is not None:
        holds = "holds" if c["verdict"]["holds"] else "fails"
        prices = ", ".join(f"${lo:.2f} to " + ("any higher" if hi is None else f"${hi:.2f}")
                           for lo, hi in c["price_range"]) or "no price"
        again = c["repriced"]["clearly_beats" if c["verdict"]["name"] == "clearly beats"
                              else "beats"]
        words += (f"\n    '{c['x']} {c['verdict']['name']} {c['y']}' {holds} for a quality "
                  f"point priced at {prices}; repriced at {REPRICE_MODEL}, it "
                  + ("holds" if again else "fails"))
    return words


def _arm_words(arm: str, f: Mapping[str, Any]) -> list[str]:
    recall = f["recall"]
    left_out = f" ({f['minutes_left_out']} with no memo left out)" if f["minutes_left_out"] else ""
    return [
        f"  {arm}: quality {f['quality']:.3f} (plans {_figure(f['plans_quality'])}, control "
        f"plans {_figure(f['control_quality'])}); ${f['dollars_per_plan']:.4f} a plan, "
        f"${f['dollars_per_plan_repriced']:.4f} repriced; {_figure(f['quality_per_dollar'])} "
        "quality a dollar",
        f"    {_figure(f['minutes_per_plan'])} minutes a plan{left_out}; "
        f"{_figure(f['quality_per_minute'])} quality a minute; recall {recall['right']} of "
        f"{recall['asked']} right",
        "    criteria: " + ", ".join(f"{c} {_figure(v)}" for c, v in f["criteria"].items()),
    ]


def meeting_words(m: Mapping[str, Any]) -> str:
    """One meeting's finished try in plain words: what closed its
    discussion, and what it left. Only arm A's meeting is one call; a
    channel arm's with nothing to close it never closed, or left no record."""
    if not m["recorded"]:
        words = ["no record"]
    elif m["closed_by"]:
        words = [f"closed by {m['closed_by']}"]
    else:
        words = ["one call" if m["arm"] == ARM_A else "never closed"]
    if m["energy"]:
        words.append("energy " + ", ".join(
            f"{adviser} {'none' if level is None else f'{level:.2f}'}"
            for adviser, level in m["energy"].items()
        ))
    if m["minutes"] is not None:
        words.append(f"{m['minutes']:.1f} minutes")
    if m["cut"]:
        words.append(f"memo cut at 400 of {m['words']} words")
    if m["answered"] is False:  # None at a briefing, which asks for no answer
        words.append("no reply, every answer wrong" if m["kind"] == "recall"
                     else "no memo, scored 0")
    return "; ".join(words)


def _figure(value: float | None) -> str:
    return "none" if value is None else f"{value:.3f}"
