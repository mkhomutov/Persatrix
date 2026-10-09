"""What an EXP-001 scored run's call logs hold, and what its arm calls cost.

The scored run keeps each window in a directory of its own, ``window-N``:
its pairs, the pairs set aside, and the scored judging's batch, each with
its call logs. The cap and the report read them back from there.

- **Real spend** prices every arm call in every window with the fixed
  table, discarded or not, set aside or not (pre-registration §3). A call
  the table cannot price is a harness fault in the window that logged it,
  since PR 1's row refuses to price it at zero. No fix can price the calls
  already made, so a closed window's are left out and named.
- **A log the harness cannot read**, such as one a crash stopped mid-line,
  is named and its calls are left out, rather than keep the report from
  being written: an arm's log, as in a practice run, or the judge's.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evaluators.exp001.attempts import HarnessFault
from evaluators.exp001.costs import real_spend
from evaluators.exp001.deployed_meeting import CALL_LOG
from evaluators.exp001.judge import read_judge_log
from evaluators.exp001.runtime import CallLog, read_call_logs

PAIRS = "pairs"
INTERRUPTED = "interrupted"
JUDGING = "judging"
BATCH = "scored"


def window_directory(root: Path, number: int) -> Path:
    """Where the scored run's *number*-th window keeps its pairs, packets and judging."""
    return root / f"window-{number}"


def window_calls(root: Path, window: Mapping[str, Any]) -> tuple[CallLog, list[str]]:
    """Every arm call the window's pairs logged, those set aside included,
    and the logs the harness cannot read, by path within *root*."""
    directory = window_directory(root, window["window"])
    return read_call_logs(
        (path for part in (PAIRS, INTERRUPTED)
         for path in sorted((directory / part).rglob(CALL_LOG))),
        root,
    )


def judge_calls(root: Path, window: Mapping[str, Any]) -> tuple[CallLog, list[str]]:
    """The window's scored judging's calls, and its log, by path within
    *root*, when the harness cannot read it."""
    log = window_directory(root, window["window"]) / JUDGING / BATCH / CALL_LOG
    return read_call_logs([log], root, read_judge_log)


@dataclass(frozen=True)
class Spent:
    """A window's real spend, and its arm calls the fixed table cannot price, by model."""

    dollars: float
    unpriced: dict[str, int]


def priced(calls: CallLog) -> Spent:
    """The real spend of *calls*, each call the fixed table can price counted."""
    dollars, unpriced = 0.0, Counter[str]()
    for record in calls.records:
        try:
            dollars += real_spend((record,))
        except (KeyError, ValueError):  # a model it does not list, or cache tokens it cannot price
            unpriced[record.model] += 1
    return Spent(dollars, dict(sorted(unpriced.items())))


def spend_so_far(
    root: Path, windows: list[Mapping[str, Any]], current: Mapping[str, Any],
) -> float:
    """Real spend so far, every one of *windows* counted. A call the fixed
    table cannot price is a harness fault in *current*, the window that
    logged it; a closed window's are left out."""
    total = 0.0
    for window in windows:
        spent = priced(window_calls(root, window)[0])
        if spent.unpriced and window["window"] == current["window"]:
            calls = ", ".join(f"{n} on {model}" for model, n in spent.unpriced.items())
            raise HarnessFault(
                f"window {window['window']}: arm calls the fixed table cannot price ({calls})",
            )
        total += spent.dollars
    return total
