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

The second half of the issue is the refusal text: a channel message for
an agent that has no dispatch target on this server (a task agent, say)
is refused as that, not as an overloaded receiver.
"""

from __future__ import annotations

import contextlib
import logging
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

from agents.channel_receive import NO_DISPATCH_TARGET, RECEIVER_OVERLOADED
from agents.dispatch import EventDispatcher
from agents.llm_client import LLMClient, LLMResponse
from agents.persona import PersonaMemoryError, create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.server import AgentServer
from agents.server_cli import start_or_exit
from agents.server_persona import initialize_persona_agents
from agents.server_servicers import AgentServiceServicer
from agents.tick import TickScheduler
from agents.tools.registry import clear_registry

from ._receive_channel_message_helpers import StubAgent, channel_event

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
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{server.port}")
        try:
            assert channel.get_state() is not grpc.ChannelConnectivity.READY
        finally:
            await channel.close()

    async def test_another_start_up_error_is_not_turned_into_this_exit(self) -> None:
        """Only the memory failure is this exit; anything else is raised as it is."""
        server = MagicMock(spec=AgentServer)
        server.start = AsyncMock(side_effect=RuntimeError("boom"))
        server.stop = AsyncMock()

        with pytest.raises(RuntimeError, match="boom"):
            await start_or_exit(server)

        server.stop.assert_not_awaited()

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


def test_the_real_process_exits_with_status_one(tmp_path: Path) -> None:
    """The whole path, in a real process: ``python -m agents.server``.

    Asserts the exit status a supervisor sees, and that the process ends
    by itself instead of waiting on a database thread.
    """
    store = tmp_path / "store"
    store.mkdir()
    config = tmp_path / "agents.yaml"
    config.write_text(yaml.safe_dump({
        "schema_version": "0.2",
        "agents": [{
            "id": AGENT, "type": "persona", "name": "Ember Owl", "role": "test persona",
            "model": "quality", "max_llm_calls": 3, "tools": [],
            "persona": {"name": "Ember Owl", "background": "ISSUE-0205 test persona."},
            "autonomy": {"level": "reactive", "timers": []},
            # A folder where the database file should be.
            "memory": {"db_path": str(store)},
        }],
    }), encoding="utf-8")

    done = subprocess.run(
        [
            sys.executable, "-m", "agents.server", "--agent", AGENT,
            "--config", str(config), "--workspace", str(tmp_path), "--port", "0",
            # Nothing listens here; the process must not get as far as calling it.
            "--orchestrator-url", "http://127.0.0.1:1", "--shutdown-grace", "1",
        ],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, check=False,
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


# ─── The refusal text ────────────────────────────────────────


@pytest.mark.asyncio
class TestRefusalNamesTheCause:
    @pytest.mark.parametrize(
        ("has_target", "enqueue_accepts", "want"),
        [
            (False, True, NO_DISPATCH_TARGET.format(agent_id=AGENT)),
            (True, False, RECEIVER_OVERLOADED),
            (True, True, ""),
        ],
        ids=["no dispatch target", "target with a full queue", "accepted"],
    )
    async def test_each_refusal_has_its_own_text(
        self, has_target: bool, enqueue_accepts: bool, want: str,
    ) -> None:
        agent = StubAgent(agent_id=AGENT, config={"model": "test"})
        dispatcher = MagicMock(spec=EventDispatcher)
        dispatcher.has_inbound_target = MagicMock(return_value=has_target)
        dispatcher.enqueue_inbound = MagicMock(return_value=enqueue_accepts)
        servicer = AgentServiceServicer({AGENT: agent}, dispatcher)

        ack = await servicer.ReceiveChannelMessage(
            channel_event(), MagicMock(spec=grpc.aio.ServicerContext),
        )

        assert ack.error_message == want
        assert ack.success is (want == "")
        assert dispatcher.enqueue_inbound.called is has_target

    @pytest.mark.parametrize(
        ("registered", "loop", "want"),
        [
            (False, None, False),
            (True, None, True),
            (False, "running", True),
            (False, "stopped", False),
            (True, "stopped", True),
        ],
        ids=[
            "nothing registered",
            "a registered agent",
            "a running event loop",
            "an event loop that is not running",
            "a registered agent whose loop is not running",
        ],
    )
    async def test_what_counts_as_a_dispatch_target(
        self, registered: bool, loop: str | None, want: bool,
    ) -> None:
        """The dispatcher's own rule: the two places an inbound event can go."""
        dispatcher = EventDispatcher()
        if registered:
            dispatcher.register_agent(AGENT, MagicMock(spec=_LLMPersonaAgent))
        if loop is not None:
            scheduler = MagicMock(spec=TickScheduler)
            scheduler.is_running = loop == "running"
            dispatcher.register_tick_scheduler(AGENT, scheduler)

        assert dispatcher.has_inbound_target(AGENT) is want
        assert dispatcher.has_inbound_target("someone-else") is False

    async def test_a_task_agent_is_not_an_overloaded_receiver(self) -> None:
        """The real dispatcher, hosting nothing: what a task agent's server has."""
        agent = StubAgent(agent_id="planner", config={"model": "test"})
        servicer = AgentServiceServicer({"planner": agent}, EventDispatcher())

        ack = await servicer.ReceiveChannelMessage(
            channel_event(channel_id="group:general"), MagicMock(spec=grpc.aio.ServicerContext),
        )

        assert ack.success is False
        assert ack.error_message == NO_DISPATCH_TARGET.format(agent_id="planner")
        assert "planner" in ack.error_message
        assert "overloaded" not in ack.error_message
        assert "queue" not in ack.error_message


def test_the_two_texts_cannot_be_mistaken_for_each_other() -> None:
    assert "overloaded" in RECEIVER_OVERLOADED
    assert "overloaded" not in NO_DISPATCH_TARGET
    assert "{agent_id}" in NO_DISPATCH_TARGET
