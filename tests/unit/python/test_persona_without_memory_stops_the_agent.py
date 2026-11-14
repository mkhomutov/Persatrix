"""A persona that cannot open its memory stops its agent process (ISSUE-0205).

A persona's memory is one database file.  When it failed to open, the
agent server logged the error, left the persona out of the event
dispatcher and carried on: it still registered the persona with the
orchestrator, which marks an agent healthy at registration and never
checks again.  The fleet looked healthy, the persona never replied, and
every message sent to it was refused as ``receiver overloaded: agent
event-loop queue full``, which names the wrong cause.

The process now stops with a non-zero exit status, before it registers
anything.  An agent process hosts one agent, so a persona without its
memory leaves a process that serves nothing, and an exit is the one
signal every supervisor already reads.

The second half of the issue is the refusal text, which
``test_channel_refusal_names_its_cause.py`` covers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import grpc
import pytest
import yaml

from agents.dispatch import EventDispatcher
from agents.llm_client import LLMClient, LLMResponse
from agents.memory.shared_pool import SharedMemoryPool, SharedPoolConfig, SharedPoolRegistry
from agents.persona import PersonaMemoryError, create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.server import AgentServer
from agents.server_cli import start_or_exit
from agents.server_persona import initialize_persona_agents
from agents.server_pools import start_shared_pools
from agents.tick import TickScheduler
from agents.tools.registry import clear_registry

from ._receive_channel_message_helpers import StubAgent

AGENT = "ember-owl"
REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _client() -> LLMClient:
    provider = AsyncMock()
    provider.create_message = AsyncMock(return_value=LLMResponse(text="ok"))
    provider.format_tool_definitions = MagicMock(return_value=[])
    return LLMClient(provider)


def _persona(db_path: str, *, autonomy: str = "reactive") -> _LLMPersonaAgent:
    config: dict[str, Any] = {
        "id": AGENT, "type": "persona", "name": "Ember Owl", "role": "test persona",
        "model": "test-model", "max_llm_calls": 3, "max_tokens": 4096, "tools": [],
        "persona": {"name": "Ember Owl", "background": "ISSUE-0205 test persona."},
        "autonomy": {"level": autonomy, "timers": []},
        "memory": {"db_path": db_path, "working": {"max_tokens": 50000}},
        "relationships": [],
    }
    return create_persona_agent(agent_id=AGENT, config=config, llm_client=_client())


# ─── Ways a memory database fails to open ────────────────────


def _a_folder(tmp_path: Path) -> str:
    """The path names a folder, as a wrong volume mount leaves it."""
    return str(tmp_path)


def _missing_parent(tmp_path: Path) -> str:
    """The folder the file should be in does not exist."""
    return str(tmp_path / "no-such-folder" / "memory.db")


def _not_a_database(tmp_path: Path) -> str:
    """The file is there and is not a database: a corrupt store."""
    path = tmp_path / "memory.db"
    path.write_bytes(b"this is not a sqlite file, " * 64)
    return str(path)


BROKEN_STORES: list[Callable[[Path], str]] = [_a_folder, _missing_parent, _not_a_database]


@pytest.mark.asyncio
class TestInitializePersonaAgents:
    @pytest.mark.parametrize("broken", BROKEN_STORES, ids=lambda f: f.__name__.lstrip("_"))
    async def test_a_persona_whose_memory_does_not_open_stops_the_startup(
        self, tmp_path: Path, broken: Callable[[Path], str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        agent = _persona(broken(tmp_path), autonomy="semi-autonomous")
        dispatcher = EventDispatcher()
        schedulers: dict[str, TickScheduler] = {}
        try:
            with caplog.at_level(logging.ERROR), pytest.raises(PersonaMemoryError) as raised:
                await initialize_persona_agents({AGENT: agent}, dispatcher, schedulers)
        finally:
            await agent.close_memory()

        assert raised.value.agent_id == AGENT
        assert AGENT in str(raised.value)
        assert raised.value.__cause__ is not None, "the store's own error is kept as the cause"
        assert dispatcher.get_agent(AGENT) is None, "it takes no events"
        assert schedulers == {}, "and no tick loop was started for it"
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert [(r.name, r.getMessage().split(" — ")[0]) for r in errors] == [
            ("Persatrix.agent.server_persona", f"Failed to initialize memory for agent {AGENT}"),
        ]
        assert errors[0].exc_info is not None, "with the store's traceback"

    async def test_a_persona_whose_memory_opens_is_served(self, tmp_path: Path) -> None:
        agent = _persona(str(tmp_path / "memory.db"))
        dispatcher = EventDispatcher()
        try:
            await initialize_persona_agents({AGENT: agent}, dispatcher, {})

            assert dispatcher.get_agent(AGENT) is agent
        finally:
            await agent.close_memory()

    async def test_a_task_agent_has_no_memory_to_fail_here(self) -> None:
        """Only a persona needs its memory to serve; a task agent is passed over."""
        task_agent = StubAgent(agent_id="planner", config={"model": "test"})
        dispatcher = EventDispatcher()

        await initialize_persona_agents({"planner": task_agent}, dispatcher, {})

        assert dispatcher.get_agent("planner") is None


def _non_daemon_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.is_alive() and not t.daemon}


@pytest.fixture
async def new_server() -> AsyncIterator[Callable[[], AgentServer]]:
    """Build servers that are stopped when the test ends, pass or fail.

    A test here that fails can leave a server running with a database
    connection open.  That connection's thread is not a daemon, so the
    test process itself would then never exit: the failure this file is
    about, in the test runner.
    """
    servers: list[AgentServer] = []

    def build() -> AgentServer:
        server = AgentServer(port=0, orchestrator_url="http://127.0.0.1:1", shutdown_grace=1)
        servers.append(server)
        return server

    yield build
    for server in servers:
        with contextlib.suppress(Exception):
            await server.stop()


async def _nothing_listens_on(port: int) -> bool:
    """True when a gRPC channel to *port* does not become ready."""
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        await asyncio.wait_for(channel.channel_ready(), timeout=1.0)
    except TimeoutError:
        return True
    finally:
        await channel.close()
    return False


def _saved_state(db_path: str) -> str:
    """The persona's state row, read with a connection of the test's own."""
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT persona_state_json FROM agent_state WHERE agent_id = ?", (AGENT,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None, "no state was ever saved"
    return str(row[0])


@pytest.mark.asyncio
class TestASharedPoolThatFailsToOpen:
    """A shared pool keeps its rows in the persona's own database file and
    opens it first, through a connection of its own."""

    async def test_it_is_closed_before_it_is_dropped(self, tmp_path: Path) -> None:
        before = _non_daemon_threads()
        pool = SharedMemoryPool(
            SharedPoolConfig(
                name="team-knowledge", readers=frozenset({AGENT}), writers=frozenset({AGENT}),
            ),
            # The file opens; the first statement on it fails.
            db_path=_not_a_database(tmp_path),
        )
        registry = SharedPoolRegistry({"team-knowledge": pool})

        await start_shared_pools(registry)

        assert registry.names() == ()
        assert pool._episodic._db is None, "the connection that was made is closed"
        assert _non_daemon_threads() <= before, "and its thread has ended"

    async def test_a_listening_server_is_what_the_probe_below_tells_apart(
        self, new_server: Callable[[], AgentServer],
    ) -> None:
        """The control for ``_nothing_listens_on``: a started server answers."""
        server = new_server()
        server._self_register = AsyncMock(return_value=False)  # type: ignore[method-assign]
        await server.start()

        assert not await _nothing_listens_on(server.port)


@pytest.mark.asyncio
class TestStartOrExit:
    @pytest.mark.parametrize("broken", BROKEN_STORES, ids=lambda f: f.__name__.lstrip("_"))
    async def test_the_process_exits_before_it_registers_anything(
        self, tmp_path: Path, broken: Callable[[Path], str],
        new_server: Callable[[], AgentServer],
    ) -> None:
        server = new_server()
        server.register_agent(_persona(broken(tmp_path)))
        register = AsyncMock(return_value=True)
        server._self_register = register  # type: ignore[method-assign]

        with pytest.raises(SystemExit) as exit_:
            await start_or_exit(server)

        # A string exit code is printed to stderr and exits with status 1.
        assert isinstance(exit_.value.code, str)
        assert AGENT in exit_.value.code
        assert "cannot open its memory" in exit_.value.code
        register.assert_not_awaited()
        assert server._session is None, "no call to the orchestrator was set up"

    async def test_what_did_open_is_closed_so_the_process_can_end(
        self, tmp_path: Path, new_server: Callable[[], AgentServer],
    ) -> None:
        """A database connection is a thread that is not a daemon.

        The episodic tier opens first.  When a later tier fails, that
        connection is open, and a process cannot exit while such a thread
        is alive.  The server is stopped before the exit, which closes it.
        """
        before = _non_daemon_threads()
        agent = _persona(str(tmp_path / "memory.db"))
        agent._relationship_memory.initialize = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("migration 19 failed"),
        )
        server = new_server()
        server.register_agent(agent)
        port_before_start = server.port

        with pytest.raises(SystemExit):
            await start_or_exit(server)

        assert agent._episodic_memory._db is None, "the tier that opened was closed"
        assert _non_daemon_threads() <= before, "no database thread is left to hold the exit"
        assert server.port != port_before_start, "the server had started, and was stopped"
        assert await _nothing_listens_on(server.port)

    async def test_a_failed_start_leaves_the_saved_state_as_it_was(
        self, tmp_path: Path, new_server: Callable[[], AgentServer],
    ) -> None:
        """Stopping the server writes each persona's state to its store.

        The state is read from the store on the last step of opening the
        memory.  A start that fails before that step holds the defaults a
        new persona begins with, and must not write them over what an
        earlier run saved.
        """
        db_path = str(tmp_path / "memory.db")
        earlier = _persona(db_path)
        await earlier.initialize_memory()
        earlier.state.stress_level = 0.8
        earlier.state.energy = 0.2
        earlier.state.goal_progress = {"ship v0.4.0": 0.7}
        await earlier.close_memory()
        saved = _saved_state(db_path)
        assert '"stress_level": 0.8' in saved, saved

        agent = _persona(db_path)
        agent._relationship_memory.initialize = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("migration 19 failed"),
        )
        server = new_server()
        server.register_agent(agent)
        with pytest.raises(SystemExit):
            await start_or_exit(server)

        assert _saved_state(db_path) == saved

    async def test_a_persona_that_started_still_saves_its_state_on_stop(
        self, tmp_path: Path, new_server: Callable[[], AgentServer],
    ) -> None:
        db_path = str(tmp_path / "memory.db")
        agent = _persona(db_path)
        server = new_server()
        server.register_agent(agent)
        server._self_register = AsyncMock(return_value=False)  # type: ignore[method-assign]
        await start_or_exit(server)
        agent.state.stress_level = 0.6

        await server.stop()

        assert '"stress_level": 0.6' in _saved_state(db_path)

    @pytest.mark.parametrize(
        "error",
        [
            RuntimeError("boom"), OSError("boom"), ValueError("boom"),
            sqlite3.OperationalError("boom"),
        ],
        ids=lambda e: type(e).__name__,
    )
    async def test_another_start_up_error_stops_the_server_and_is_raised_as_it_is(
        self, error: Exception,
    ) -> None:
        """Only the memory failure gets this exit's message.  Any other
        failure to start is raised as it is, after the server is stopped:
        left running, it would hold its port and its database threads, and
        the process would neither serve nor end."""
        server = MagicMock(spec=AgentServer)
        server.start = AsyncMock(side_effect=error)
        server.stop = AsyncMock()

        with pytest.raises(type(error), match="boom") as raised:
            await start_or_exit(server)

        assert raised.value is error
        server.stop.assert_awaited_once()

    async def test_a_start_that_fails_after_the_memory_opened_leaves_nothing_running(
        self, tmp_path: Path, new_server: Callable[[], AgentServer],
    ) -> None:
        before = _non_daemon_threads()
        agent = _persona(str(tmp_path / "memory.db"))
        server = new_server()
        server.register_agent(agent)
        server._self_register = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("the start-up tail failed"),
        )

        with pytest.raises(RuntimeError, match="the start-up tail failed"):
            await start_or_exit(server)

        assert agent._episodic_memory._db is None, "the memory that opened was closed"
        assert _non_daemon_threads() <= before, "no database thread is left to hold the exit"
        assert await _nothing_listens_on(server.port)

    async def test_a_server_whose_persona_has_its_memory_starts(
        self, tmp_path: Path, new_server: Callable[[], AgentServer],
    ) -> None:
        agent = _persona(str(tmp_path / "memory.db"))
        server = new_server()
        server.register_agent(agent)
        # The start-up tail talks to an orchestrator; none is here.
        server._self_register = AsyncMock(return_value=False)  # type: ignore[method-assign]

        await start_or_exit(server)

        assert server._dispatcher.get_agent(AGENT) is agent


def _store_is_a_folder(tmp_path: Path) -> str:
    store = tmp_path / "store"
    store.mkdir()
    return str(store)


def _store_is_not_a_database(tmp_path: Path) -> str:
    return _not_a_database(tmp_path)


@pytest.mark.parametrize(
    "broken", [_store_is_a_folder, _store_is_not_a_database],
    ids=["a folder where the file should be", "a file that is not a database"],
)
@pytest.mark.parametrize(
    "shared_pool", [False, True], ids=["no shared pool", "the shipped shared pool"],
)
def test_the_real_process_exits_with_status_one(
    tmp_path: Path, broken: Callable[[Path], str], shared_pool: bool,
) -> None:
    """The whole path, in a real process: ``python -m agents.server``.

    Asserts the exit status a supervisor sees, and that the process ends
    by itself instead of waiting on a database thread.  The second is what
    the two parameters are for.  A connection that cannot be made leaves no
    thread (the folder), one that is made and then fails does (the file
    that is not a database).  And a shared pool keeps its rows in the
    persona's own file, through a connection of its own that is opened
    first: ``config/agents.yaml`` ships one.
    """
    document: dict[str, Any] = {
        "schema_version": "0.2",
        "agents": [{
            "id": AGENT, "type": "persona", "name": "Ember Owl", "role": "test persona",
            "model": "quality", "max_llm_calls": 3, "tools": [],
            "persona": {"name": "Ember Owl", "background": "ISSUE-0205 test persona."},
            "autonomy": {"level": "reactive", "timers": []},
            "memory": {"db_path": broken(tmp_path)},
        }],
    }
    if shared_pool:
        shipped = yaml.safe_load((REPO_ROOT / "config" / "agents.yaml").read_text(encoding="utf-8"))
        document["shared_memory_pools"] = shipped["shared_memory_pools"]
        assert document["shared_memory_pools"], "the shipped config declares a shared pool"
    config = tmp_path / "agents.yaml"
    config.write_text(yaml.safe_dump(document), encoding="utf-8")

    done = subprocess.run(
        [
            sys.executable, "-m", "agents.server", "--agent", AGENT,
            "--config", str(config), "--workspace", str(tmp_path), "--port", "0",
            # Nothing listens here; the process must not get as far as calling it.
            "--orchestrator-url", "http://127.0.0.1:1", "--shutdown-grace", "1",
        ],
        # A process that does not end is killed here, and the test fails.
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=30, check=False,
        env={
            "PATH": "/usr/bin:/bin",
            # The offline demo config: a mock provider, so no key is needed.
            "PERSATRIX_OPTIMIZATION_CONFIG": str(
                REPO_ROOT / "config" / "demo" / "offline" / "optimization.yaml"),
            "OTEL_SDK_DISABLED": "true",
        },
    )

    assert done.returncode == 1, done.stderr[-2000:]
    assert f"Agent {AGENT} cannot open its memory" in done.stderr
    assert "Could not reach orchestrator" not in done.stderr, "it never tried to register"
