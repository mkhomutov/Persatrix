"""The processes of a local deployment: what each runs, and starting and stopping them.

A deployment (:mod:`evaluators.exp001.deployment`) is one orchestrator process
and one process per adviser. They run this checkout's code: the orchestrator
binary built from it, and ``python -m agents.server`` with the checkout on the
import path. None of them inherits a ``PERSATRIX_`` or ``SECURITY_`` setting
from the shell that runs the harness, so a stray orchestrator or agent setting
there cannot change a deployment. The model client's own settings, such as
``ANTHROPIC_BASE_URL`` or a proxy, do pass through, as they reach arm A's calls
in the harness's own process. Every process listens on loopback only.

The orchestrator starts first, and the advisers once it answers; on the way
down the advisers stop first, since an adviser drains its memory summaries as
it stops and they still need the orchestrator's wallet.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from evaluators.exp001.deployment import DeploymentError, Layout
from evaluators.exp001.orchestrator import OrchestratorError

LOOPBACK = "127.0.0.1"
# The shell settings a process never inherits from the harness.
_OWN_PREFIXES = ("PERSATRIX_", "SECURITY_")


@dataclass(frozen=True)
class Ports:
    http: int
    grpc: int


def free_ports() -> Ports:
    """Two ports no process on this machine is listening on right now."""
    sockets = [socket.socket() for _ in range(2)]
    try:
        for s in sockets:
            s.bind((LOOPBACK, 0))
        http, grpc = (s.getsockname()[1] for s in sockets)
    finally:
        for s in sockets:
            s.close()
    return Ports(http=http, grpc=grpc)


@dataclass(frozen=True)
class Process:
    """One process of a deployment: what it runs, and what it adds to the environment."""

    name: str
    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: Path
    log: Path


def orchestrator_process(layout: Layout, ports: Ports, *, binary: Path) -> Process:
    argv = (
        str(binary),
        "--config", str(layout.config),
        "--port", str(ports.grpc),
        "--http-port", str(ports.http),
        "--http-bind", LOOPBACK,
        "--grpc-bind", LOOPBACK,
        "--workflows-dir", str(layout.workflows),
        "--env", "development",
        "--channels-db", str(layout.data / "channels.db"),
        "--accounts-db", str(layout.data / "accounts.db"),
    )
    env = {
        # The log buffer must start, or the orchestrator serves no wallet
        # and every adviser's model call is refused.
        "PERSATRIX_LOGBUFFER_DIR": str(layout.data / "logs"),
        "OBSERVABILITY_AUDIT_PATH": str(layout.data / "audit.jsonl"),
        "SECURITY_RATE_LIMIT_ENABLED": "false",
    }
    return Process("orchestrator", argv, env, layout.root, layout.log("orchestrator"))


def adviser_process(
    layout: Layout,
    adviser_id: str,
    ports: Ports,
    *,
    python: Path,
    repo: Path,
    env: Mapping[str, str],
) -> Process:
    """An adviser's process; *env* holds its meeting's clock and call-log settings."""
    argv = (
        str(python), "-m", "agents.server",
        "--agent", adviser_id,
        "--port", "0",  # the process picks a free one and registers it
        "--host", LOOPBACK,
        "--config", str(layout.config / "agents.yaml"),
        "--workspace", str(layout.workspace),
        "--orchestrator-url", f"http://{LOOPBACK}:{ports.http}",
        "--orchestrator-grpc", f"{LOOPBACK}:{ports.grpc}",
    )
    own = {
        "PYTHONPATH": str(repo),
        # Pinned, or the process reads the checkout's shipped config.
        "PERSATRIX_OPTIMIZATION_CONFIG": str(layout.config / "optimization.yaml"),
        **env,
    }
    return Process(adviser_id, argv, own, repo, layout.log(adviser_id))


def process_environment(process: Process, environ: Mapping[str, str]) -> dict[str, str]:
    """The environment *process* runs with: the harness's own, less its
    ``PERSATRIX_`` and ``SECURITY_`` settings, then the process's."""
    kept = {k: v for k, v in environ.items() if not k.startswith(_OWN_PREFIXES)}
    return {**kept, **process.env}


class Handle(Protocol):
    """A started process, as far as a deployment needs one."""

    returncode: int | None

    def poll(self) -> int | None: ...

    def send_signal(self, sig: int) -> None: ...

    def kill(self) -> None: ...


class Registry(Protocol):
    """What a deployment asks the orchestrator while it starts."""

    async def healthy(self) -> bool: ...

    async def agents(self) -> set[str]: ...


def launch(process: Process, environ: Mapping[str, str]) -> Handle:
    """Start *process*, its output appended to its log."""
    process.log.parent.mkdir(parents=True, exist_ok=True)
    with process.log.open("ab") as log:
        return subprocess.Popen(
            process.argv,
            cwd=process.cwd,
            env=process_environment(process, environ),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # the harness's own Ctrl-C does not reach it
        )


_POLL = 0.2
_REAP = 5.0  # how long a killed process may take to be gone


class Deployment:
    """Starts and stops one deployment's processes: the orchestrator first,
    then the advisers; on the way down, the advisers first."""

    def __init__(
        self,
        orchestrator: Process,
        advisers: Sequence[Process],
        *,
        spawn: Callable[[Process, Mapping[str, str]], Handle] = launch,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._orchestrator = orchestrator
        self._advisers = tuple(advisers)
        self._spawn = spawn
        self._environ = dict(os.environ if environ is None else environ)
        self._clock = clock
        self._sleep = sleep
        self._handles: dict[str, Handle] = {}
        self._logs = {p.name: p.log for p in (orchestrator, *advisers)}

    async def start(self, registry: Registry, *, timeout: float = 120.0) -> None:
        """Start every process and wait until each adviser has registered.

        A process that exits meanwhile, or a start that takes longer than
        *timeout* seconds, is a :class:`DeploymentError`.
        """
        deadline = self._clock() + timeout
        self._handles["orchestrator"] = self._spawn(self._orchestrator, self._environ)
        while True:
            self._refuse_exited()
            if await registry.healthy():
                break
            await self._wait(deadline, f"the orchestrator is not healthy after {timeout:g} s")
        for process in self._advisers:
            self._handles[process.name] = self._spawn(process, self._environ)
        while True:
            self._refuse_exited()
            try:
                registered = await registry.agents()
            except OrchestratorError:  # it stopped answering; its exit, or the deadline, says why
                registered = set()
            missing = sorted({p.name for p in self._advisers} - registered)
            if not missing:
                return
            await self._wait(deadline, f"not registered after {timeout:g} s: {', '.join(missing)}")

    def _refuse_exited(self) -> None:
        for name, code in self.exited().items():
            raise DeploymentError(f"{name} exited ({code}) while starting; see {self._logs[name]}")

    async def _wait(self, deadline: float, timed_out: str) -> None:
        if self._clock() >= deadline:
            raise DeploymentError(timed_out)
        await self._sleep(_POLL)

    def exited(self) -> dict[str, int]:
        """The processes that have exited, with their exit codes."""
        codes = {name: handle.poll() for name, handle in self._handles.items()}
        return {name: code for name, code in codes.items() if code is not None}

    async def stop(self, *, grace: float = 90.0) -> dict[str, int]:
        """Stop the advisers, then the orchestrator; return every exit code.

        An adviser drains its memory summaries as it stops, and they still
        need the orchestrator's wallet. A process still running *grace*
        seconds after it was asked to stop is killed.
        """
        await self._stop_all([name for name in self._handles if name != "orchestrator"], grace)
        await self._stop_all([name for name in self._handles if name == "orchestrator"], grace)
        return self.exited()

    def kill(self) -> None:
        """Kill every process still running, at once: what a stop that is
        itself interrupted falls back on, so nothing is left running."""
        for handle in self._handles.values():
            if handle.poll() is None:
                handle.kill()

    async def _stop_all(self, names: Sequence[str], grace: float) -> None:
        running = [name for name in names if self._handles[name].poll() is None]
        for name in running:
            self._handles[name].send_signal(signal.SIGTERM)
        if await self._until_exited(running, grace):
            return
        for name in running:
            if self._handles[name].poll() is None:
                self._handles[name].kill()
        await self._until_exited(running, _REAP)

    async def _until_exited(self, names: Sequence[str], seconds: float) -> bool:
        deadline = self._clock() + seconds
        while any(self._handles[name].poll() is None for name in names):
            if self._clock() >= deadline:
                return False
            await self._sleep(_POLL)
        return True
