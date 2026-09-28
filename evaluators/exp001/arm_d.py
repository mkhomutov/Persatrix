"""Arm D: a series' meetings on one deployment, with memory as shipped.

Arm D holds each meeting as arm C does. What differs is what carries from
one meeting to the next: the advisers' memory, as shipped, through the
1 500-token allocator (pre-registration §2). So each attempt at a series
starts on a new deployment with empty stores and keeps it for every meeting
of the series. The briefing's first try writes it, declaring a channel for
each meeting, named for the meeting's place and shaped as B and C shape
theirs. Every meeting has the same members, so the audience gate admits the
series' own memories.

Every try of a meeting starts all of the deployment's processes again, with
the meeting's clock and call-log settings, and stops them once the meeting is
over, as :mod:`evaluators.exp001.deployed_meeting` does for B and C; their
logs are the try's own. An adviser that stops writes the conversations it
still holds open (ISSUE-0172), so a meeting that ended by its idle window,
which tells no adviser, keeps its memory too; an adviser the stop has to
kill may not have, so that fails the meeting. Before a start the harness
puts each adviser's saved persona state (its mood, stress, energy and goal
progress) back to the runtime's defaults. Its memory, and the interaction
counter the runtime keeps though nothing reads it, stay as they are.

A restart must leave the memory stores exactly as they were (check 2). An
adviser that starts replays the channels' recent messages into memory, but
only those from after its clock began, and every try begins the clock again,
so no earlier meeting's message is replayed. The harness checks this every
time. Once each adviser has logged the end of its catch-up pass, the pass
must have replayed nothing, and each store that existed before the start
must hold exactly what it held then, in every table but the saved state.
If not, arm D cannot be held as designed: the meeting raises, and the run
stops on a harness fault. A pass the runtime's budget cut off has ended
too, with the count it reached. An adviser that exits, or whose pass aborts,
before every pass has ended is a start that failed, held again.

A try that a provider error cut short is held again
(:mod:`evaluators.exp001.attempts`) from the memory its advisers had before
the first try. Before a meeting's first try the harness copies the
deployment's stores, each adviser's memory and the orchestrator's databases,
and before any later try it puts that copy back, so the new try also finds
the meeting's channel without the failed try's messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import re
import shutil
import sqlite3
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from evaluators.exp001.attempts import Hold, channel_hold
from evaluators.exp001.channel_arm import LIMITS, ChannelMeeting, Limits, channel_name
from evaluators.exp001.deployed_meeting import DeploymentAPI, read_only, run_on_deployment
from evaluators.exp001.deployment import (
    ARMS_ALIAS,
    REPO,
    Alias,
    DeploymentError,
    Layout,
    StartError,
    channel_config,
    write_deployment,
)
from evaluators.exp001.materials import Meeting, Series
from evaluators.exp001.orchestrator import log_lines
from evaluators.exp001.panel import Panel
from evaluators.exp001.processes import Deployment, Handle, Process, launch

ARM = "D"
# Where a meeting keeps the stores from before its first try, beside its tries.
STORES_BEFORE = "stores-before"
# How long the advisers get to end their catch-up passes after a start; the
# runtime gives each pass 60 seconds.
CATCH_UP_LIMIT = dt.timedelta(seconds=90)
_CATCH_UP_POLL = 1.0
# The adviser logs one of these as its catch-up pass ends: complete; cut off by
# the runtime's budget, with the count it had reached; or aborted by an error,
# with none. Tests hold each to the runtime.
_CAUGHT_UP = re.compile(
    r"channels: catch-up complete agent=(?P<agent>\S+) channels=\d+ events=(?P<events>\d+) ",
)
_CUT_OFF = re.compile(
    r"channels: catch-up exceeded \S+ wall-clock budget for agent=(?P<agent>[^\s;]+); "
    r"partial channels=\d+ events=(?P<events>\d+) ",
)
_ABORTED = re.compile(r"channels: catch-up replay aborted for agent (?P<agent>\S+)")
# The table of an adviser's saved state other than memory: the persona state,
# and the interaction counter.
_SAVED_STATE = "agent_state"

Rows = dict[str, tuple[str, ...]]


def _real_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def series_channels(panel: Panel, series: Series) -> list[dict[str, Any]]:
    """A channel for each of the series' meetings, named and shaped as B's and C's."""
    return [
        channel_config(panel, ARM, name=channel_name(series, m), organisation=series.organisation)
        for m in series.meetings
    ]


def arm_d_hold(
    panel: Panel,
    series: Series,
    root: Path,
    *,
    binary: Path,
    watch_seconds: float = 5.0,
    **options: Any,
) -> Hold[ChannelMeeting]:
    """Arm D's meetings, held by the rules of attempts and failures as B's and
    C's are: each try in its own directory under *root*, and each attempt on
    a deployment of its own, ``attempt-N/deployment``. *options* go to
    :func:`run_meeting` as they are."""

    async def run(
        _panel: Panel, _arm: str, _series: Series, meeting: Meeting, *,
        attempt: int, meeting_try: int, directory: Path, ended: Callable[[dt.datetime], None],
    ) -> ChannelMeeting:
        # channel_hold lays each try out as attempt-N/<meeting>/try-M.
        return await run_meeting(
            panel, series, meeting, attempt=attempt, meeting_try=meeting_try,
            directory=directory, deployment=directory.parents[1] / "deployment",
            before=directory.parent / STORES_BEFORE, binary=binary, ended=ended, **options,
        )

    return channel_hold(panel, ARM, series, root, run=run, watch_seconds=watch_seconds)


async def run_meeting(
    panel: Panel,
    series: Series,
    meeting: Meeting,
    *,
    attempt: int,
    meeting_try: int,
    directory: Path,
    deployment: Path,
    before: Path,
    binary: Path,
    python: Path = Path(sys.executable),
    repo: Path = REPO,
    alias: Alias = ARMS_ALIAS,
    spawn: Callable[[Process, Mapping[str, str]], Handle] = launch,
    room: DeploymentAPI | None = None,
    now: Callable[[], dt.datetime] = _real_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    limits: Limits = LIMITS,
    catch_up: dt.timedelta = CATCH_UP_LIMIT,
    ended: Callable[[dt.datetime], None] | None = None,
) -> ChannelMeeting:
    """Hold one try of an arm-D meeting on its series' *deployment*.

    The series' first meeting writes the deployment at its first try.
    *before* keeps the stores from before the meeting's first try, and a
    later try puts them back. *directory* must be empty; it holds the try's
    call log, its record and its processes' logs. *ended* is told when the
    meeting is over, as :func:`~evaluators.exp001.deployed_meeting.run_on_deployment`
    tells it.
    """
    # An earlier run's call log would be appended to, and counted again.
    if directory.exists() and any(directory.iterdir()):
        raise DeploymentError(f"{directory} is not empty; each try has a directory of its own")
    layout = Layout(deployment)
    if meeting == series.meetings[0] and meeting_try == 1:
        write_deployment(layout, panel, ARM, channels=series_channels(panel, series), alias=alias)
    elif not (layout.config / "channels.yaml").is_file():
        raise DeploymentError(f"{deployment} holds no deployment; the briefing writes it")
    if meeting_try == 1:
        keep_stores(layout, before)
    else:
        restore_stores(layout, before)
    reset_persona_state(layout, panel)
    held = {a.id: memory_rows(layout.memory_db(a.id)) for a in panel.advisers}
    logs = directory / "logs"

    async def check_restart(deployment: Deployment) -> None:
        await _check_restart(
            layout, logs, panel, held, deployment, now=now, sleep=sleep, limit=catch_up,
        )

    directory.mkdir(parents=True, exist_ok=True)
    return await run_on_deployment(
        layout, panel, ARM, series, meeting, attempt=attempt, meeting_try=meeting_try,
        directory=directory, logs=logs, binary=binary, python=python, repo=repo, spawn=spawn,
        room=room, now=now, sleep=sleep, limits=limits, started=check_restart, ended=ended,
    )


async def _check_restart(
    layout: Layout,
    logs: Path,
    panel: Panel,
    held: Mapping[str, Rows | None],
    deployment: Deployment,
    *,
    now: Callable[[], dt.datetime],
    sleep: Callable[[float], Awaitable[None]],
    limit: dt.timedelta,
) -> None:
    """Refuse a start that changed memory (check 2): once every adviser has
    ended its catch-up pass, none may have replayed a message, and every store
    that existed before the start must hold what it *held* then. A process
    that exits first is a start that failed, as it is while starting."""
    deadline = now() + limit
    while True:
        for name, code in deployment.exited().items():
            raise StartError(f"{name} exited ({code}) before every catch-up pass ended; see {logs}")
        # Each adviser's own log, named as run_on_deployment names it.
        replayed = {a.id: catch_up_replayed(logs / f"{a.id}.log", a.id) for a in panel.advisers}
        waiting = [aid for aid, count in replayed.items() if count is None]
        if not waiting:
            break
        if now() >= deadline:
            raise DeploymentError(
                f"no catch-up pass ended within {limit.total_seconds():g} s: {', '.join(waiting)}",
            )
        await sleep(_CATCH_UP_POLL)
    if any(replayed.values()):
        raise DeploymentError("a restart replayed messages into memory (check 2): " + "; ".join(
            f"{aid}: {count}" for aid, count in replayed.items() if count
        ))
    changed = {
        aid: _changed(rows, memory_rows(layout.memory_db(aid)))
        for aid, rows in held.items() if rows is not None
    }
    if any(changed.values()):
        raise DeploymentError("a restart changed the memory stores (check 2): " + "; ".join(
            f"{aid}: {', '.join(tables)}" for aid, tables in changed.items() if tables
        ))


def _changed(before: Rows, after: Rows | None) -> list[str]:
    """The tables whose rows differ; every table, when the store is gone."""
    after = after or {}
    return sorted(t for t in before.keys() | after.keys() if before.get(t) != after.get(t))


def memory_rows(db: Path) -> Rows | None:
    """Every row an adviser's store holds, as text, sorted, table by table,
    leaving out the saved state; None when there is no store."""
    if not db.exists():
        return None
    with read_only(db) as store:
        tables = [name for (name,) in store.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
        )]
        return {
            table: tuple(sorted(repr(row) for row in store.execute(f'SELECT * FROM "{table}"')))
            for table in tables if table != _SAVED_STATE
        }


def reset_persona_state(layout: Layout, panel: Panel) -> None:
    """Put each adviser's saved persona state back to the runtime's defaults,
    as a new adviser starts; its memory and interaction counter stay."""
    for adviser in panel.advisers:
        db = layout.memory_db(adviser.id)
        if not db.exists():
            continue
        with contextlib.closing(sqlite3.connect(db)) as store, store:
            store.execute(
                "UPDATE agent_state SET persona_state_json = NULL WHERE agent_id = ?",
                (adviser.id,),
            )


def catch_up_replayed(log: Path, adviser_id: str) -> int | None:
    """How many messages the adviser's catch-up pass replayed into memory, read
    from its log; None until it has logged the pass's end. A pass the runtime's
    budget cut off ends with the count it had reached. One that aborted is a
    start that failed, since what it replayed first is not known."""
    for line in log_lines(log):
        message = str(line.get("message", ""))
        for ending in (_CAUGHT_UP, _CUT_OFF):
            match = ending.match(message)
            if match is not None and match["agent"] == adviser_id:
                return int(match["events"])
        match = _ABORTED.match(message)
        if match is not None and match["agent"] == adviser_id:
            raise StartError(f"{adviser_id}'s catch-up pass aborted; see {log}")
    return None


def _stores(layout: Layout) -> list[Path]:
    """The deployment's stores: each adviser's memory, and the orchestrator's databases."""
    return sorted(p for p in (*layout.memory.glob("*"), *layout.data.glob("*.db*")) if p.is_file())


def keep_stores(layout: Layout, before: Path) -> None:
    """Copy the deployment's stores into *before*, which must be new. No
    process of the deployment may be running."""
    before.mkdir(parents=True)
    for store in _stores(layout):
        kept = before / store.relative_to(layout.root)
        kept.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(store, kept)


def restore_stores(layout: Layout, before: Path) -> None:
    """Put back the stores *before* keeps, dropping any made since."""
    if not before.is_dir():
        raise FileNotFoundError(f"{before}: no stores were kept before the meeting's first try")
    for store in _stores(layout):
        store.unlink()
    for kept in sorted(before.rglob("*")):
        if kept.is_file():
            store = layout.root / kept.relative_to(before)
            store.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(kept, store)
