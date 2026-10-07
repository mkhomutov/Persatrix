"""The EXP-001 scored run: every arm's scored series, judged and reported.

Pre-registration §3: once practice runs show the eight checks pass, the
scored series run in order, 1 to 5, and within each series the five arms
run one at a time, in the order drawn for that series
(:func:`evaluators.exp001.costs.arm_orders`). Each arm's series is held as a
pair, each try kept as it ends (:mod:`evaluators.exp001.pairs`).

- **Windows.** Every scored meeting happens within seven days of the first.
  The run is held in windows, each in a directory of its own, ``window-N``.
  A window's seven days open as its first try begins, and no try begins
  once they have passed: a series not yet held in every arm is then not
  held, and the window's meetings end.
- **A harness fault**, while the meetings are held or while the judge
  scores them, stops the run and closes its window: every scored output so
  far is discarded, kept whole where it is and published with the result.
  The fix goes through a reviewed PR; the run is then started again naming
  it, and the next window holds every series again from series 1, its seven
  days opening again. A third fault ends the run, incomplete.
- **The $150 cap.** Before each try, the harness prices every arm call the
  run's call logs hold, in every window, pairs set aside included. Once they
  reach the cap no try begins, so the try that crossed it is the last, and
  the run is incomplete.
- **A dropped series** is dropped from every arm's comparisons, so the arms
  after it in that series' order never hold it. Once fewer than four series
  can still be kept, the run stops, incomplete.

Once a window's meetings end with four series or more kept, the harness
gathers the answers of those series alone, draws the packets once
(:mod:`evaluators.exp001.rating`), and the judge scores them as batch
``scored`` within its own $25 cap. Then it writes the report
(:mod:`evaluators.exp001.scored_report`), as it does whenever the run stops.

A scored run started again in the same directory goes on where it stopped,
as a practice run does: a pair held to the end is read back, one stopped
partway is set aside in its window and held again from its briefing, and
neither the packets nor the judge's answers are asked for twice. The
directory keeps the model alias its meetings are held on, and one run at a
time holds it. Offline, every model alias points at the offline mock
provider: a rehearsal of the run at no cost, which prices nothing, so the
cap never stops it, and judges nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
import functools
import json
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from agents.llm_client import LLMClient
from evaluators.exp001 import scored_report
from evaluators.exp001.attempts import (
    HARNESS_FAULTS,
    MIN_SERIES,
    HarnessFault,
    Held,
    Hold,
    RunStopped,
    SeriesRun,
)
from evaluators.exp001.costs import ARMS, PRICES, SPEND_CAP, arm_orders, real_spend
from evaluators.exp001.deployed_meeting import CALL_LOG
from evaluators.exp001.deployment import ARMS_ALIAS, Alias
from evaluators.exp001.judge import (
    RATER,
    Judged,
    JudgePrompts,
    judge_batch,
    read_judge_log,
    sole_run,
)
from evaluators.exp001.materials import SCORED_SERIES, Meeting, Series
from evaluators.exp001.pairs import Kept, arm_hold, hold_pair, pair_calls, read_pair, write_json
from evaluators.exp001.panel import Panel
from evaluators.exp001.rating import draw_packets, gather_answers
from evaluators.exp001.runtime import CallLog, merge_call_logs, read_call_log

RUN = "run.json"
PAIRS = "pairs"
INTERRUPTED = "interrupted"
JUDGING = "judging"
BATCH = "scored"
REPORT = "report.json"
SUMMARY = "report.txt"
WINDOW = dt.timedelta(days=7)

# Builds an arm's hold for one series, held in the directory given.
MakeHold = Callable[[str, Series, Path], Hold[Any]]


class RefusedError(RuntimeError):
    """The scored run in this directory cannot go on as asked: a harness
    fault awaits its fix, a fix was named with no fault to fix, a third
    fault ended the run, it is held on another model alias, or another run
    holds it."""


class CapReached(RunStopped):
    """Real spend reached the $150 cap before a try."""


class WindowClosed(RunStopped):
    """A try would begin seven days or more after its window's first."""


def window_directory(root: Path, number: int) -> Path:
    """Where the scored run's *number*-th window keeps its pairs, packets and judging."""
    return root / f"window-{number}"


def _real_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def run_scored(
    root: Path,
    *,
    panel: Panel,
    series: Sequence[Series],
    names: Sequence[str],
    client: LLMClient,
    binary: Path,
    alias: Alias = ARMS_ALIAS,
    prompts: JudgePrompts | None,
    fixed_by: str | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], dt.datetime] = _real_now,
    progress: Callable[[str], None] | None = None,
    make_hold: MakeHold | None = None,
) -> dict[str, Any]:
    """Hold the five scored *series* in *root*, then draw the packets, judge
    them and write the report, which it returns.

    *client* makes arm A's calls and the judge's. The channel arms run
    *binary*, the orchestrator, with every model alias on *alias*. With no
    *prompts* nothing is judged, as offline. *names* are the advisers' IDs
    and names, which no rater reads. After a harness fault, *fixed_by* names
    the reviewed PR that fixed it, and the next window begins; named with
    no fault to fix, or once a third fault has ended the run, it is
    refused. *now* is real time. *make_hold* builds each arm's hold for a
    series, by default :func:`evaluators.exp001.pairs.arm_hold`.
    """
    if tuple(s.id for s in series) != SCORED_SERIES:
        raise ValueError(
            f"the scored run holds the five scored series, in order; got {[s.id for s in series]}",
        )
    root.mkdir(parents=True, exist_ok=True)
    with sole_run(root, RefusedError(f"{root}: another run is holding this scored run")):
        state = _open(root, alias, fixed_by)
        window = state["windows"][-1]
        directory = window_directory(root, window["window"])
        judged: Judged | None = None
        if scored_report.outcome(state["windows"])[0] != scored_report.INCOMPLETE:
            def build(arm: str, held: Series, at: Path) -> Hold[Any]:
                return arm_hold(arm, panel, held, at, client=client, binary=binary, alias=alias)

            try:
                if window.get("ended") is None:
                    say = progress or (lambda _: None)
                    check = _check(root, state, window, priced=alias.model in PRICES, now=now,
                                   progress=say)
                    stopped_by = await _hold(
                        directory, series, make_hold or build, check, sleep=sleep, progress=say,
                    )
                    window["ended"] = _ended(directory, series, stopped_by, now())
                    write_json(root / RUN, state)
                if scored_report.is_scored(window):
                    judged = await _draw_and_judge(
                        directory, series, window, names, client, prompts, sleep,
                    )
            except HarnessFault as fault:
                window["fault"] = {"message": str(fault), "at": now().isoformat()}
                write_json(root / RUN, state)
                # The fault is what stopped the run: a report that cannot be
                # written now is written by the next start.
                with contextlib.suppress(Exception):
                    _write_report(root, state, series, alias, None)
                raise
        return _write_report(root, state, series, alias, judged)


def _open(root: Path, alias: Alias, fixed_by: str | None) -> dict[str, Any]:
    """The run's state in *root*, a new window begun when *fixed_by* names
    the fix for the fault that closed the last one."""
    try:
        state: dict[str, Any] = json.loads((root / RUN).read_text())
    except FileNotFoundError:
        state = {}
    held_on = dataclasses.asdict(alias)
    if fixed_by is not None and not fixed_by.strip():
        fixed_by = None  # a blank name names no fix
    nothing_to_fix = RefusedError(
        f"{root}: no harness fault has stopped this scored run, so there is no fix to name",
    )
    if not state:
        if fixed_by is not None:
            raise nothing_to_fix
        state = {"alias": held_on, "windows": [_window(1, None)]}
        write_json(root / RUN, state)
        return state
    if state["alias"] != held_on:
        was = state["alias"]
        raise RefusedError(
            f"{root}: this scored run holds its meetings on {was['provider']} {was['model']}, "
            f"not {alias.provider} {alias.model}; hold them in a new directory",
        )
    windows = state["windows"]
    last = windows[-1]
    if sum(1 for w in windows if w.get("fault")) >= HARNESS_FAULTS:
        if fixed_by is not None:
            raise RefusedError(
                f"{root}: a third harness fault ended this scored run, incomplete; "
                "nothing is held again",
            )
        return state
    if last.get("fault"):
        if fixed_by is None:
            number = last["window"]
            raise RefusedError(
                f"{root}: a harness fault stopped window {number} ({last['fault']['message']}); "
                f"once its fix has merged through a reviewed PR, start window {number + 1} "
                "with --fixed-by naming that PR",
            )
        windows.append(_window(last["window"] + 1, fixed_by))
        write_json(root / RUN, state)
        return state
    if fixed_by is not None:
        raise nothing_to_fix
    return state


def _window(number: int, fixed_by: str | None) -> dict[str, Any]:
    return {"window": number, "fixed_by": fixed_by, "opened_at": None, "fault": None,
            "ended": None}


def _check(
    root: Path, state: dict[str, Any], window: dict[str, Any], *, priced: bool,
    now: Callable[[], dt.datetime], progress: Callable[[str], None],
) -> Callable[[], None]:
    """What the harness asks before each try: whether real spend has reached
    the cap, every window counted, and whether the window's seven days,
    which the first try opens, have passed."""
    def check() -> None:
        if priced:
            spent = sum(real_spend(_window_calls(root, w)[0].records) for w in state["windows"])
            if spent >= SPEND_CAP:
                raise CapReached(f"real spend has reached ${spent:.2f}, the ${SPEND_CAP:.0f} cap")
        at = now()
        if window["opened_at"] is None:
            window["opened_at"] = at.isoformat()
            write_json(root / RUN, state)
            closes = (at + WINDOW).isoformat()
            progress(f"window {window['window']} opens: no try begins at or after {closes}")
        elif at - dt.datetime.fromisoformat(window["opened_at"]) >= WINDOW:
            raise WindowClosed(f"seven days have passed since window {window['window']} opened")

    return check


async def _hold(
    directory: Path, series: Sequence[Series], make_hold: MakeHold, check: Callable[[], None],
    *, sleep: Callable[[float], Awaitable[None]], progress: Callable[[str], None],
) -> str | None:
    """Hold the window's series in order, each one's arms in its drawn order;
    what stopped the meetings before every series was held, if anything."""
    dropped = 0
    try:
        for held, order in zip(series, arm_orders(len(series)), strict=True):
            for arm in order:
                run = await hold_pair(
                    arm, held, directory / PAIRS / held.id / arm,
                    _checked(functools.partial(make_hold, arm, held), check),
                    interrupted=directory / INTERRUPTED, sleep=sleep, progress=progress,
                )
                if run.dropped:
                    dropped += 1
                    break  # in no arm's comparisons, so held no further
            if len(series) - dropped < MIN_SERIES:
                progress("fewer than four series can still be kept: the run stops")
                return "series"
    except CapReached as stop:
        progress(f"{stop}: the run stops")
        return "cap"
    except WindowClosed as stop:
        progress(f"{stop}: no more of its meetings are held")
        return "window"
    return None


def _checked(
    make: Callable[[Path], Hold[Any]], check: Callable[[], None],
) -> Callable[[Path], Hold[Any]]:
    """*make*, with each try it holds begun only once *check* allows it."""
    def build(directory: Path) -> Hold[Any]:
        hold = make(directory)

        async def checked(meeting: Meeting, attempt: int, meeting_try: int) -> Held[Any]:
            check()
            return await hold(meeting, attempt, meeting_try)

        return checked

    return build


def _held(directory: Path, series: Sequence[Series]) -> list[SeriesRun[Kept]]:
    """The window's pairs held to the end, in the order held."""
    runs = []
    for held, order in zip(series, arm_orders(len(series)), strict=True):
        for arm in order:
            run = read_pair(directory / PAIRS / held.id / arm)
            if run is not None:
                runs.append(run)
    return runs


def _ended(
    directory: Path, series: Sequence[Series], stopped_by: str | None, at: dt.datetime,
) -> dict[str, Any]:
    """How the window's meetings ended: a series is kept when every arm held
    it to the end, dropped when any arm dropped it, and otherwise not held."""
    runs = _held(directory, series)
    finished = {(run.series, run.arm) for run in runs if not run.dropped}
    dropped = [s.id for s in series if any(r.series == s.id and r.dropped for r in runs)]
    kept = [s.id for s in series if all((s.id, arm) in finished for arm in ARMS)]
    return {
        "at": at.isoformat(), "kept": kept, "dropped": dropped,
        "not_held": [s.id for s in series if s.id not in kept and s.id not in dropped],
        "stopped_by": stopped_by,
    }


async def _draw_and_judge(
    directory: Path, series: Sequence[Series], window: dict[str, Any], names: Sequence[str],
    client: LLMClient, prompts: JudgePrompts | None, sleep: Callable[[float], Awaitable[None]],
) -> Judged | None:
    """Draw the packets of the kept series' answers, once, and have the
    judge score them as batch ``scored``; nothing is judged with no *prompts*."""
    kept = set(window["ended"]["kept"])
    runs = [run for run in _held(directory, series) if run.series in kept]
    drawn = draw_packets(
        directory, series, gather_answers(runs, {s.id: s for s in series}), names,
    )
    if prompts is None:
        return None
    batch = directory / JUDGING / BATCH
    return await judge_batch(client, drawn.order(RATER), prompts, batch, batch=BATCH, sleep=sleep)


def _window_calls(root: Path, window: dict[str, Any]) -> tuple[CallLog, list[str]]:
    """Every arm call the window's pairs logged, those set aside included,
    and the logs the harness cannot read, by path within *root*. A pair
    stopped by a crash can have stopped a line half written: that log's calls
    are left out and it is named, as in a practice run."""
    directory = window_directory(root, window["window"])
    logs: list[CallLog] = []
    unread: list[str] = []
    for part in (PAIRS, INTERRUPTED):
        for path in sorted((directory / part).rglob(CALL_LOG)):
            try:
                logs.append(read_call_log(path))
            except ValueError:  # a CallLogError, or text cut inside a character
                unread.append(str(path.relative_to(root)))
    return merge_call_logs(logs), unread


def _write_report(
    root: Path, state: dict[str, Any], series: Sequence[Series], alias: Alias,
    judged: Judged | None,
) -> dict[str, Any]:
    """Gather what the report reads, write it, and return it."""
    windows = state["windows"]
    priced = alias.model in PRICES
    logs = [_window_calls(root, window) for window in windows]
    judging = [read_judge_log(window_directory(root, w["window"]) / JUDGING / BATCH / CALL_LOG)
               for w in windows]
    last = windows[-1]
    directory = window_directory(root, last["window"])
    runs = [] if last["fault"] or last["ended"] is None else _held(directory, series)
    report = scored_report.build(
        alias=alias,
        windows=windows,
        runs=runs,
        calls={(run.series, run.arm): pair_calls(directory / PAIRS / run.series / run.arm, run)
               for run in runs},
        series=series,
        priced=priced,
        spend=[real_spend(log.records) if priced else None for log, _ in logs],
        everything=merge_call_logs([*(log for log, _ in logs), *judging]),
        unread=[path for _, paths in logs for path in paths],
        judge_calls=None if judged is None else judging[-1],
        judged=judged,
    )
    write_json(root / REPORT, report)
    (root / SUMMARY).write_text(scored_report.summary(report))
    return report
