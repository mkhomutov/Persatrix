"""The EXP-001 practice run: the practice series for every arm, judged and reported.

Pre-registration §3: the practice series runs first, as often as needed,
until the eight checks pass, and it is never scored. A practice run holds it
for each arm named, one arm at a time, each arm's series in a directory of
its own under the run's (:mod:`evaluators.exp001.pairs`). Once every arm has
held it, the harness draws the packets (:mod:`evaluators.exp001.rating`).
The two people score the practice memos together, to agree how the anchors
apply, and throw those scores away. The judge scores them in a batch of its
own, ``practice``, so none of its calls counts toward the scored judging's
cap, and in the order drawn for it. Then the harness writes the report
(:mod:`evaluators.exp001.practice_report`).

A practice run started again in the same directory goes on where it
stopped: an arm's series held to the end is not held again, the packets are
not drawn again, and the judge is not asked again about a packet it
answered. A harness fault while the meetings are held closes the directory:
a practice run after the fix starts in a new one, so none mixes meetings
held before and after a fix. A fault while judging leaves it open, since a
reviewed fix to the judge's reader reads the kept answers again without a
second pass. The directory keeps the arms it was started with and the model
alias its meetings are held on, so none mixes meetings held offline and on
the provider, and one run at a time holds it.

Offline, every model alias points at the offline mock provider, and so does
arm A's call: the meetings cost nothing, and nothing is judged, since the
mock cannot answer as the judge.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from agents.call_log import prefix_sha256
from agents.llm_client import LLMClient
from evaluators.exp001 import practice_report
from evaluators.exp001.arm_d_prime import TryKey
from evaluators.exp001.attempts import HarnessFault, Hold, SeriesRun, try_directory
from evaluators.exp001.costs import ARMS
from evaluators.exp001.deployed_meeting import CALL_LOG, PREFIX
from evaluators.exp001.deployment import ARMS_ALIAS, Alias
from evaluators.exp001.judge import RATER, JudgePrompts, judge_batch, read_judge_log, sole_run
from evaluators.exp001.materials import Series
from evaluators.exp001.pairs import Kept, arm_hold, hold_pair, pair_calls, write_json
from evaluators.exp001.panel import Panel
from evaluators.exp001.rating import draw_packets, gather_answers, read_seal
from evaluators.exp001.runtime import CallLog, merge_call_logs, read_call_log

RUN = "run.json"
PAIRS = "pairs"
INTERRUPTED = "interrupted"
JUDGING = "judging"
BATCH = "practice"
REPORT = "report.json"
SUMMARY = "report.txt"


class RefusedError(RuntimeError):
    """The practice run in this directory cannot go on as asked: a harness
    fault closed it, it was started with other arms or on another model
    alias, or another run holds it."""


async def run_practice(
    root: Path,
    arms: Sequence[str],
    *,
    panel: Panel,
    series: Series,
    scored: Sequence[Series],
    names: Sequence[str],
    client: LLMClient,
    binary: Path,
    alias: Alias = ARMS_ALIAS,
    prompts: JudgePrompts | None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    progress: Callable[[str], None] | None = None,
    make_hold: Callable[[str, Path], Hold[Any]] | None = None,
) -> dict[str, Any]:
    """Hold *series* for each of *arms* in *root*, in the order named, then
    draw the packets, judge them and write the report, which it returns.

    *client* makes arm A's calls and the judge's. The channel arms run
    *binary*, the orchestrator, with every model alias on *alias*. With no
    *prompts* nothing is judged, as offline; judged, the report projects the
    scored judging's spend at the packets of the *scored* series. *names*
    are the advisers' IDs and names, which no rater reads. *make_hold*
    builds each arm's hold for the directory it is held in, by default
    :func:`evaluators.exp001.pairs.arm_hold`.
    *root* keeps the arms it was first started with and *alias*: a start
    with other arms or on another alias, after a harness fault closed it, or
    while another run holds it, is :class:`RefusedError`.
    """
    if not arms or len(set(arms)) != len(arms) or not set(arms) <= set(ARMS):
        raise ValueError(f"the arms must be some of {', '.join(ARMS)}, each once; got {arms}")
    root.mkdir(parents=True, exist_ok=True)
    with sole_run(root, RefusedError(f"{root}: another run is holding this practice run")):
        state = _state(root)
        closed = state.get("fault")
        if closed is not None:
            raise RefusedError(
                f"{root}: a harness fault closed this practice run ({closed['arm']}: "
                f"{closed['message']}); start the next one in a new directory",
            )
        held_on = dataclasses.asdict(alias)
        if "arms" not in state:
            state = {"arms": list(arms), "alias": held_on}
            write_json(root / RUN, state)
        elif state["arms"] != list(arms):
            raise RefusedError(
                f"{root}: this practice run holds arms {', '.join(state['arms'])}; "
                "hold other arms in a new directory",
            )
        elif state.get("alias") != held_on:
            was = state.get("alias") or {}
            raise RefusedError(
                f"{root}: this practice run holds its meetings on {was.get('provider')} "
                f"{was.get('model')}, not {alias.provider} {alias.model}; hold them in a new "
                "directory",
            )

        def build(arm: str) -> Callable[[Path], Hold[Any]]:
            if make_hold is not None:
                return lambda directory: make_hold(arm, directory)
            return lambda directory: arm_hold(
                arm, panel, series, directory, client=client, binary=binary, alias=alias,
            )

        runs: dict[str, SeriesRun[Kept]] = {}
        for arm in arms:
            try:
                runs[arm] = await hold_pair(
                    arm, series, _pair_directory(root, series, arm), build(arm),
                    interrupted=root / INTERRUPTED, sleep=sleep, progress=progress,
                )
            except HarnessFault as fault:
                write_json(root / RUN, {**state, "fault": {
                    "arm": arm, "message": str(fault), "at": dt.datetime.now(dt.UTC).isoformat(),
                }})
                raise
        drawn = draw_packets(root, [series], gather_answers(runs.values(), {series.id: series}),
                             names)
        judged = judge_calls = seal = projection = None
        if prompts is not None:
            batch = root / JUDGING / BATCH
            judged = await judge_batch(client, drawn.order(RATER), prompts, batch, batch=BATCH,
                                       sleep=sleep)
            judge_calls, seal = read_judge_log(batch / CALL_LOG), read_seal(root)
            projection = practice_report.project_judging(
                judge_calls.records, drawn.packets, prompts, scored,
            )
        calls = {arm: pair_calls(_pair_directory(root, series, arm), run)
                 for arm, run in runs.items()}
        set_aside, unread = _set_aside_calls(root)
        report = practice_report.build(
            series=series, runs=runs, calls=calls, written=_written(root, series, runs),
            everything=merge_call_logs([*calls.values(), set_aside]), unread=unread,
            judge_calls=judge_calls, judged=judged, seal=seal, projection=projection,
        )
        write_json(root / REPORT, report)
        (root / SUMMARY).write_text(practice_report.summary(report))
        return report


def _pair_directory(root: Path, series: Series, arm: str) -> Path:
    return root / PAIRS / series.id / arm


def _state(root: Path) -> dict[str, Any]:
    """What *root* keeps of its practice run: the arms it was started with,
    the model alias its meetings are held on, and the harness fault that
    closed it, if one did; nothing before a start."""
    try:
        return dict(json.loads((root / RUN).read_text()))
    except FileNotFoundError:
        return {}


def _written(
    root: Path, series: Series, runs: Mapping[str, SeriesRun[Kept]],
) -> dict[TryKey, str]:
    """The prefix the harness wrote for each of D-prime's tries, by its SHA-256."""
    run = runs.get("D-prime")
    if run is None:
        return {}
    meetings = {m.id: m for m in series.meetings}
    written: dict[TryKey, str] = {}
    for t in run.tries:
        path = try_directory(
            _pair_directory(root, series, run.arm), t.attempt, meetings[t.meeting], t.meeting_try,
        ) / PREFIX
        digest = prefix_sha256(path.read_bytes().decode("utf-8")) if path.exists() else None
        if digest is not None:
            written[(run.arm, series.id, t.meeting, t.attempt, t.meeting_try)] = digest
    return written


def _set_aside_calls(root: Path) -> tuple[CallLog, list[str]]:
    """Every call the pairs set aside logged, and their logs the harness
    cannot read, by path within *root*. A pair stopped partway can have
    stopped a line half written: that log's calls are left out of the
    totals, and it is named, rather than keep the report from being written."""
    logs: list[CallLog] = []
    unread: list[str] = []
    for path in sorted((root / INTERRUPTED).rglob(CALL_LOG)):
        try:
            logs.append(read_call_log(path))
        except ValueError:  # a CallLogError, or text cut inside a character
            unread.append(str(path.relative_to(root)))
    return merge_call_logs(logs), unread
