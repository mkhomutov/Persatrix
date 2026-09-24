"""EXP-001 harness — a deployment's processes, started and stopped (PR 5a).

A deployment is one orchestrator process and one process per adviser, all
running this checkout's code on loopback. The orchestrator must answer
before any adviser starts, and every adviser must register before the
operator speaks. On the way down the advisers stop first: each drains its
memory summaries as it stops, and they still need the orchestrator's wallet.
No setting of the shell that runs the harness reaches a process.
"""

from __future__ import annotations

import signal
from pathlib import Path
from typing import Any

import pytest

from evaluators.exp001.deployment import DeploymentError, Layout
from evaluators.exp001.orchestrator import OrchestratorError
from evaluators.exp001.panel import load_panel
from evaluators.exp001.processes import (
    Deployment,
    Ports,
    Process,
    adviser_process,
    orchestrator_process,
    process_environment,
)

_REPO = Path(__file__).resolve().parents[3]
PANEL = load_panel(_REPO / "evaluators" / "experiments" / "EXP-001" / "panel.yaml")


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    return Layout(tmp_path / "deployment")


_PORTS = Ports(http=18080, grpc=19090)


class TestProcesses:
    def test_the_orchestrator_serves_this_deployment_only_on_loopback(
        self, layout: Layout,
    ) -> None:
        process = orchestrator_process(layout, _PORTS, binary=Path("/repo/bin/persatrix-server"))
        assert process.argv == (
            "/repo/bin/persatrix-server",
            "--config", str(layout.config),
            "--port", "19090",
            "--http-port", "18080",
            "--http-bind", "127.0.0.1",
            "--grpc-bind", "127.0.0.1",
            "--workflows-dir", str(layout.workflows),
            "--env", "development",
            "--channels-db", str(layout.data / "channels.db"),
            "--accounts-db", str(layout.data / "accounts.db"),
        )
        assert process.env == {
            "PERSATRIX_LOGBUFFER_DIR": str(layout.data / "logs"),
            "OBSERVABILITY_AUDIT_PATH": str(layout.data / "audit.jsonl"),
            "SECURITY_RATE_LIMIT_ENABLED": "false",
        }
        assert (process.name, process.cwd, process.log) == (
            "orchestrator", layout.root, layout.logs / "orchestrator.log",
        )

    def test_an_adviser_runs_this_checkouts_agents_on_this_deployments_config(
        self, layout: Layout,
    ) -> None:
        meeting_env = {"PERSATRIX_CALL_LOG": "/run/calls.jsonl", "PERSATRIX_CLOCK_START": "x"}
        process = adviser_process(
            layout, "ripple-kite", _PORTS,
            python=Path("/venv/bin/python"), repo=Path("/repo"), env=meeting_env,
        )
        assert process.argv == (
            "/venv/bin/python", "-m", "agents.server",
            "--agent", "ripple-kite",
            "--port", "0",
            "--host", "127.0.0.1",
            "--config", str(layout.config / "agents.yaml"),
            "--workspace", str(layout.workspace),
            "--orchestrator-url", "http://127.0.0.1:18080",
            "--orchestrator-grpc", "127.0.0.1:19090",
        )
        assert process.env == {
            "PYTHONPATH": "/repo",
            "PERSATRIX_OPTIMIZATION_CONFIG": str(layout.config / "optimization.yaml"),
            **meeting_env,
        }
        assert (process.cwd, process.log) == (Path("/repo"), layout.logs / "ripple-kite.log")

    def test_no_setting_of_the_harness_own_environment_reaches_a_process(
        self, layout: Layout,
    ) -> None:
        """A stray session, epoch or rate-limit setting in the shell that runs
        the harness would change a deployment; the key is passed through."""
        process = orchestrator_process(layout, _PORTS, binary=Path("/bin/server"))
        environ = {
            "PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-test",
            "PERSATRIX_EPOCH": "trial", "PERSATRIX_CLOCK_START": "2030-01-01T00:00:00+00:00",
            "SECURITY_RATE_LIMIT_ENABLED": "true", "SECURITY_RATE_LIMIT_CALLS": "5",
        }
        assert process_environment(process, environ) == {
            "PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-test", **process.env,
        }


class _Handle:
    """A process that answers signals as told: it exits on SIGTERM unless it
    is stubborn, and it may already have exited."""

    def __init__(self, name: str, world: _World, *, stubborn: bool, exit_code: int | None):
        self.name = name
        self.world = world
        self.stubborn = stubborn
        self.returncode = exit_code

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.world.signalled.append((self.name, sig))
        if not self.stubborn:
            self.returncode = 0

    def kill(self) -> None:
        self.world.signalled.append((self.name, signal.SIGKILL))
        self.returncode = -signal.SIGKILL


class _World:
    """Spawns fake processes, runs a fake clock and answers like an orchestrator."""

    def __init__(self, *, healthy_after: int = 2, stubborn: frozenset[str] = frozenset(),
                 exits: frozenset[str] = frozenset(), registers: bool = True,
                 dies_while_registering: bool = False) -> None:
        self.now = 0.0
        self.handles: dict[str, _Handle] = {}
        self.spawned: list[tuple[str, int]] = []  # each name, with the health checks so far
        self.signalled: list[tuple[str, int]] = []
        self.health_checks = 0
        self.healthy_after = healthy_after
        self.stubborn = stubborn
        self.exits = exits
        self.registers = registers
        self.dies_while_registering = dies_while_registering

    def spawn(self, process: Process, environ: Any) -> _Handle:
        self.spawned.append((process.name, self.health_checks))
        handle = _Handle(
            process.name, self, stubborn=process.name in self.stubborn,
            exit_code=1 if process.name in self.exits else None,
        )
        self.handles[process.name] = handle
        return handle

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds

    async def healthy(self) -> bool:
        self.health_checks += 1
        return self.health_checks >= self.healthy_after

    async def agents(self) -> set[str]:
        if self.dies_while_registering:  # it stops answering, and exits a moment later
            self.handles["orchestrator"].returncode = 2
            raise OrchestratorError("GET /api/v1/agents: Cannot connect to host")
        if not self.registers:
            return set()
        return {name for name in self.handles if name != "orchestrator"}


def _deployment(layout: Layout, world: _World) -> Deployment:
    advisers = [
        adviser_process(
            layout, a.id, _PORTS, python=Path("/py"), repo=Path("/repo"), env={},
        ) for a in PANEL.advisers
    ]
    return Deployment(
        orchestrator_process(layout, _PORTS, binary=Path("/bin/server")), advisers,
        spawn=world.spawn, environ={}, clock=world.clock, sleep=world.sleep,
    )


class TestStartAndStop:
    async def test_the_orchestrator_is_healthy_before_any_adviser_starts(
        self, layout: Layout,
    ) -> None:
        world = _World(healthy_after=3)
        await _deployment(layout, world).start(world, timeout=60)
        assert world.spawned == [("orchestrator", 0), *((a.id, 3) for a in PANEL.advisers)]

    async def test_a_process_that_exits_while_starting_is_named_with_its_log(
        self, layout: Layout,
    ) -> None:
        world = _World(exits=frozenset({"ripple-kite"}))
        with pytest.raises(DeploymentError, match=r"ripple-kite exited \(1\).*ripple-kite\.log"):
            await _deployment(layout, world).start(world, timeout=60)

    async def test_advisers_that_never_register_time_the_start_out(
        self, layout: Layout,
    ) -> None:
        world = _World(registers=False)
        with pytest.raises(DeploymentError, match="not registered after 60 s: crimson-crow, "):
            await _deployment(layout, world).start(world, timeout=60)

    async def test_the_advisers_stop_before_the_orchestrator(self, layout: Layout) -> None:
        """An adviser drains its memory summaries as it stops, and they still
        need the orchestrator's wallet."""
        world = _World()
        deployment = _deployment(layout, world)
        await deployment.start(world, timeout=60)
        codes = await deployment.stop(grace=30)
        assert codes == dict.fromkeys(["orchestrator", *(a.id for a in PANEL.advisers)], 0)
        assert world.signalled == [
            *((a.id, signal.SIGTERM) for a in PANEL.advisers), ("orchestrator", signal.SIGTERM),
        ]

    async def test_a_process_that_ignores_the_stop_is_killed_after_the_grace(
        self, layout: Layout,
    ) -> None:
        world = _World(stubborn=frozenset({"velvet-pika"}))
        deployment = _deployment(layout, world)
        await deployment.start(world, timeout=60)
        start = world.now
        codes = await deployment.stop(grace=30)
        assert codes["velvet-pika"] == -signal.SIGKILL
        assert world.now - start >= 30
        assert world.signalled[-2:] == [
            ("velvet-pika", signal.SIGKILL), ("orchestrator", signal.SIGTERM),
        ]

    async def test_an_orchestrator_that_goes_away_while_advisers_register_is_named(
        self, layout: Layout,
    ) -> None:
        world = _World(dies_while_registering=True)
        with pytest.raises(DeploymentError, match=r"orchestrator exited \(2\).*orchestrator\.log"):
            await _deployment(layout, world).start(world, timeout=60)

    async def test_kill_ends_every_process_still_running_at_once(self, layout: Layout) -> None:
        """What a stop that is itself interrupted falls back on: nothing is
        left running, grace or not."""
        world = _World(stubborn=frozenset({"velvet-pika", "orchestrator"}))
        deployment = _deployment(layout, world)
        await deployment.start(world, timeout=60)
        world.handles["ripple-kite"].returncode = 0
        deployment.kill()
        assert sorted(name for name, sig in world.signalled if sig == signal.SIGKILL) == sorted(
            name for name in world.handles if name != "ripple-kite"
        )
        assert all(handle.returncode is not None for handle in world.handles.values())

    async def test_stopping_twice_signals_nothing_more(self, layout: Layout) -> None:
        world = _World()
        deployment = _deployment(layout, world)
        await deployment.start(world, timeout=60)
        first = await deployment.stop(grace=30)
        sent = list(world.signalled)
        assert await deployment.stop(grace=30) == first
        assert world.signalled == sent
