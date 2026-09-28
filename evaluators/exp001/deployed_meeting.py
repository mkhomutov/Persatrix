"""One channel meeting on a deployment's processes, start to finish.

Arms B and C hold each meeting on a new deployment of the four advisers,
written into the meeting's own directory (:func:`run_meeting`), so every
meeting starts with empty stores. Arm D holds each of a series' meetings on
the series' one deployment (:mod:`evaluators.exp001.arm_d`). Either way the
harness runs the processes the same way (:func:`run_on_deployment`): it
starts them with the meeting's clock and call-log settings, goes on only once
the orchestrator has logged its rate limiter off and serves the wallet,
holds the meeting (:func:`evaluators.exp001.channel_arm.hold_meeting`), and
stops them however the meeting ends, a hangup included. Then it keeps the
meeting's record beside the call log; a meeting that fails midway still
leaves one, naming the error.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
import json
import signal
import sqlite3
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol

import aiohttp

from evaluators.exp001.channel_arm import (
    LEASE_REFUSED,
    LIMITS,
    PROCESS_EXITED,
    ChannelMeeting,
    Limits,
    Room,
    channel_name,
    hold_meeting,
)
from evaluators.exp001.deployment import (
    ARMS_ALIAS,
    REPO,
    Alias,
    DeploymentError,
    Layout,
    channel_config,
    write_deployment,
)
from evaluators.exp001.materials import Meeting, Series
from evaluators.exp001.orchestrator import Orchestrator, OrchestratorLog, read_orchestrator_log
from evaluators.exp001.panel import Panel
from evaluators.exp001.processes import (
    LOOPBACK,
    Deployment,
    Handle,
    Ports,
    Process,
    Registry,
    adviser_process,
    free_ports,
    launch,
    orchestrator_process,
)
from evaluators.exp001.runtime import call_log_env, meeting_clock_env

# The arms whose every meeting gets a new deployment.
ARMS = ("B", "C")
# What a meeting's directory holds besides the processes' logs.
CALL_LOG = "calls.jsonl"
RECORD = "meeting.json"


def _real_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class DeploymentAPI(Room, Registry, Protocol):
    """The whole of what a meeting asks its deployment's orchestrator."""


async def run_meeting(
    panel: Panel,
    arm: str,
    series: Series,
    meeting: Meeting,
    *,
    attempt: int,
    meeting_try: int,
    directory: Path,
    binary: Path,
    python: Path = Path(sys.executable),
    repo: Path = REPO,
    alias: Alias = ARMS_ALIAS,
    spawn: Callable[[Process, Mapping[str, str]], Handle] = launch,
    room: DeploymentAPI | None = None,
    now: Callable[[], dt.datetime] = _real_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    limits: Limits = LIMITS,
) -> ChannelMeeting:
    """Hold one meeting of arm B or C on a new deployment in *directory*.

    *directory* must be empty; it then holds the deployment, the call log
    its advisers write and the meeting's record, which a meeting that fails
    midway still leaves, naming the error. The deployment is stopped however
    the meeting ends, a hangup included. *room* stands in for the
    orchestrator's REST API in tests; by default the harness talks to the
    deployment's own.
    """
    if arm not in ARMS:
        raise ValueError(f"arm {arm} is not held on a new deployment per meeting; only {ARMS} are")
    # An earlier run's call log would be appended to, and counted again.
    if directory.exists() and any(directory.iterdir()):
        raise DeploymentError(f"{directory} is not empty; each meeting has a directory of its own")
    layout = Layout(directory / "deployment")
    name = channel_name(series, meeting)
    entry = channel_config(panel, arm, name=name, organisation=series.organisation)
    write_deployment(layout, panel, arm, channels=[entry], alias=alias)
    return await run_on_deployment(
        layout, panel, arm, series, meeting, attempt=attempt, meeting_try=meeting_try,
        directory=directory, logs=layout.logs, binary=binary, python=python, repo=repo,
        spawn=spawn, room=room, now=now, sleep=sleep, limits=limits,
    )


async def run_on_deployment(
    layout: Layout,
    panel: Panel,
    arm: str,
    series: Series,
    meeting: Meeting,
    *,
    attempt: int,
    meeting_try: int,
    directory: Path,
    logs: Path,
    binary: Path,
    python: Path = Path(sys.executable),
    repo: Path = REPO,
    spawn: Callable[[Process, Mapping[str, str]], Handle] = launch,
    room: DeploymentAPI | None = None,
    now: Callable[[], dt.datetime] = _real_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    limits: Limits = LIMITS,
    started: Callable[[], Awaitable[None]] | None = None,
) -> ChannelMeeting:
    """Hold one meeting on the deployment written at *layout*, then stop it.

    *directory* gets the call log and the meeting's record, and *logs* the
    processes' own logs. *started* runs once every process has started and
    the orchestrator is set up as the design needs, before the operator
    speaks; what it raises fails the meeting as any error does.
    """
    ports = free_ports()
    env = {
        **meeting_clock_env(meeting.story_date, now()),
        **call_log_env(
            directory / CALL_LOG, arm=arm, series=series.id, meeting=meeting.id,
            meeting_kind=meeting.kind, attempt=attempt, meeting_try=meeting_try,
        ),
    }
    orchestrator = _logged(orchestrator_process(layout, ports, binary=binary), logs)
    deployment = Deployment(
        orchestrator,
        [_logged(adviser_process(layout, a.id, ports, python=python, repo=repo, env=env), logs)
         for a in panel.advisers],
        spawn=spawn, clock=lambda: now().timestamp(), sleep=sleep,
    )

    def orchestrator_log() -> OrchestratorLog:
        return read_orchestrator_log(orchestrator.log)

    def energy() -> dict[str, float | None]:
        return {a.id: _energy(layout.memory_db(a.id), a.id) for a in panel.advisers}

    channel = f"group:{channel_name(series, meeting)}"
    with _cancelled_on_hangup():
        async with _connect(room, ports) as api:
            try:
                await deployment.start(api)
                begun = orchestrator_log()
                if not begun.rate_limit_off:
                    raise DeploymentError(
                        f"the orchestrator's rate limiter is on; see {orchestrator.log}",
                    )
                if not begun.wallet_served:
                    raise DeploymentError(
                        f"the orchestrator serves no wallet; see {orchestrator.log}",
                    )
                if started is not None:
                    await started()
                result = await hold_meeting(
                    api, orchestrator_log, panel, arm, series, meeting, channel=channel,
                    attempt=attempt, meeting_try=meeting_try, now=now, sleep=sleep,
                    limits=limits, read_energy=energy,
                )
                exited = deployment.exited()
            except Exception as exc:
                _write_failure(
                    directory / RECORD, error=exc, arm=arm, series=series.id,
                    meeting=meeting.id, attempt=attempt, meeting_try=meeting_try,
                    channel=channel,
                    exited=deployment.exited(),
                )
                raise
            finally:
                try:
                    await deployment.stop()
                except BaseException:  # interrupted again: nothing may be left running
                    deployment.kill()
                    raise
    log = orchestrator_log()
    failures = [*result.failures]
    if exited:
        failures.append(PROCESS_EXITED)
    if log.refused_leases:
        failures.append(LEASE_REFUSED)
    result = dataclasses.replace(result, failures=tuple(failures), exited=exited)
    write_record(directory / RECORD, result)
    if log.spending_limit_refusals:
        raise DeploymentError(
            "a spending limit the deployment turns off refused leases (check 6): "
            + "; ".join(log.spending_limit_refusals),
        )
    return result


def _logged(process: Process, logs: Path) -> Process:
    """*process*, writing its output to its own file in *logs*."""
    return dataclasses.replace(process, log=logs / f"{process.name}.log")


@contextlib.contextmanager
def _cancelled_on_hangup() -> Iterator[None]:
    """A hangup or SIGTERM cancels the meeting as Ctrl-C does, so its
    deployment, which the terminal's own signals never reach, is still stopped."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    installed: list[signal.Signals] = []
    if task is not None:
        for sig in (signal.SIGHUP, signal.SIGTERM):
            with contextlib.suppress(ValueError, RuntimeError):  # the main thread's alone
                loop.add_signal_handler(sig, task.cancel)
                installed.append(sig)
    try:
        yield
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


def _energy(db: Path, adviser_id: str) -> float | None:
    """The energy an adviser's store last saved for it; None before it saved any."""
    try:
        with contextlib.closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as store:
            row = store.execute(
                "SELECT persona_state_json FROM agent_state WHERE agent_id = ?", (adviser_id,),
            ).fetchone()
    except sqlite3.Error:
        return None
    energy = json.loads(row[0]).get("energy") if row is not None and row[0] else None
    return None if energy is None else float(energy)


@asynccontextmanager
async def _connect(room: DeploymentAPI | None, ports: Ports) -> AsyncIterator[DeploymentAPI]:
    if room is not None:
        yield room
        return
    async with aiohttp.ClientSession() as session:
        yield Orchestrator(f"http://{LOOPBACK}:{ports.http}", session)


def write_record(path: Path, meeting: ChannelMeeting) -> None:
    """Keep *meeting* as JSON: every field, times in ISO 8601."""
    path.write_text(json.dumps(_plain(meeting), indent=1, ensure_ascii=False))


def _write_failure(path: Path, *, error: BaseException, **known: Any) -> None:
    """Keep what is known of a meeting that failed midway, and why it failed."""
    record = {**_plain(known), "error": f"{type(error).__name__}: {error}"}
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False))


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value
