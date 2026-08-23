"""ISSUE-0125 — the agent re-registers itself when the orchestrator comes back.

The orchestrator's agent registry is in-memory, so a restart empties it, and
agents call ``_self_register()`` exactly once in their own startup path. Nothing
re-registers when the *orchestrator* is the process that restarted, so the fleet
stays silently unreachable: ``/healthz`` is green, publishes return ``201``, and
every dispatch is dropped with one WARN while the personas never reply.

:class:`agents.server_reregister.ReregistrationWatcher` closes that by watching a
signal the agent already holds — the connectivity state of the long-lived gRPC
channel to the orchestrator (shared by the RFC 0018 log shipper and the RFC 0023
wallet client).

Two properties these tests exist to pin, both of which a plausible-looking
implementation gets wrong:

* **The trigger is any departure from ``READY`` and return** — *not* the literal
  ``READY → TRANSIENT_FAILURE → READY`` cycle. ``TRANSIENT_FAILURE`` is only
  entered when a connection *attempt* fails; on a clean orchestrator restart the
  shipper's stream EOFs without an exception and backs off with no RPC pending,
  so the idle channel goes to ``IDLE``. A watcher written to the literal cycle
  passes an injected unit test and never fires in production.
* **Reconnecting must re-register and nothing else.** ``_self_register()`` and
  ``replay_for_persona_agents()`` sit adjacent in ``AgentServer.start()``, so the
  obvious "re-run the startup tail" shape would re-ingest the catch-up window on
  every orchestrator blip — and catch-up has no watermark (RFC 0011 OQ #8), which
  makes that unbounded re-derivation.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import grpc
import pytest

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.server import AgentServer
from agents.server_reregister import ReregistrationWatcher

READY = grpc.ChannelConnectivity.READY
IDLE = grpc.ChannelConnectivity.IDLE
CONNECTING = grpc.ChannelConnectivity.CONNECTING
TRANSIENT_FAILURE = grpc.ChannelConnectivity.TRANSIENT_FAILURE
SHUTDOWN = grpc.ChannelConnectivity.SHUTDOWN


class _ScriptedChannel:
    """A ``grpc.aio.Channel`` stand-in that walks a fixed state sequence.

    ``SHUTDOWN`` is appended to every script so the watcher's loop terminates on
    its own and the test can simply await the task — no sleeps, no polling.
    """

    def __init__(self, states: list[grpc.ChannelConnectivity]):
        self._states = [*states, SHUTDOWN]
        self._idx = 0

    def get_state(self, try_to_connect: bool = False) -> grpc.ChannelConnectivity:
        return self._states[self._idx]

    async def wait_for_state_change(self, last_observed_state) -> None:
        if self._idx + 1 < len(self._states):
            self._idx += 1
            return
        await asyncio.Event().wait()  # park forever; the script is exhausted


async def _drive(states: list[grpc.ChannelConnectivity]) -> AsyncMock:
    """Run a watcher over ``states`` to completion; return the re-register mock."""
    reregister = AsyncMock()
    watcher = ReregistrationWatcher(_ScriptedChannel(states), reregister)
    watcher.start()
    await _finished(watcher)
    return reregister


async def _finished(watcher: ReregistrationWatcher) -> None:
    """Await the watch loop, which ends when the scripted channel SHUTs DOWN."""
    task = watcher.task
    assert task is not None
    await asyncio.wait_for(task, timeout=5)


class TestTrigger:
    """What does and does not count as "the orchestrator came back"."""

    async def test_idle_cycle_reregisters(self):
        """The production shape: an idle channel drops to IDLE, then reconnects.

        This is the case the literal ``TRANSIENT_FAILURE`` cycle misses.
        """
        reregister = await _drive([READY, IDLE, CONNECTING, READY])
        reregister.assert_awaited_once()

    async def test_transient_failure_cycle_reregisters(self):
        """A real failed connection attempt is still a departure and return."""
        reregister = await _drive([READY, TRANSIENT_FAILURE, CONNECTING, READY])
        reregister.assert_awaited_once()

    async def test_first_ready_does_not_reregister(self):
        """Reaching READY for the first time is boot, not a reconnect.

        The channel is opened in ``AgentServer.start()`` and connects lazily, so
        the agent's own startup ``_self_register()`` has already run (or is about
        to). Firing here would re-register on every clean boot.
        """
        reregister = await _drive([IDLE, CONNECTING, READY])
        reregister.assert_not_awaited()

    async def test_departure_without_return_does_not_reregister(self):
        """A fleet still waiting for the orchestrator has nothing to register with."""
        reregister = await _drive([READY, IDLE, CONNECTING])
        reregister.assert_not_awaited()

    async def test_each_outage_reregisters_once(self):
        """Two outages, two re-registrations — and no extra for the steady state."""
        reregister = await _drive([READY, IDLE, READY, READY, IDLE, CONNECTING, READY])
        assert reregister.await_count == 2

    async def test_shutdown_ends_the_watcher(self):
        """A closed channel ends the loop rather than spinning on a dead handle."""
        watcher = ReregistrationWatcher(_ScriptedChannel([READY]), AsyncMock())
        watcher.start()
        await _finished(watcher)


class TestResilience:
    async def test_reregister_failure_does_not_kill_the_watcher(self):
        """A blip during a re-register must not disarm the next reconnect.

        The orchestrator is by definition flaky at exactly this moment; a watcher
        that dies on the first failed POST leaves the fleet mute for good, which
        is the defect it was built to remove.
        """
        reregister = AsyncMock(side_effect=[RuntimeError("orchestrator still booting"), None])
        watcher = ReregistrationWatcher(
            _ScriptedChannel([READY, IDLE, READY, IDLE, CONNECTING, READY]), reregister
        )
        watcher.start()
        await _finished(watcher)
        assert reregister.await_count == 2

    async def test_stop_cancels_a_watcher_parked_on_the_channel(self):
        """Shutdown does not hang waiting for a state change that never comes."""
        watcher = ReregistrationWatcher(_ScriptedChannel([READY]), AsyncMock())
        watcher.start()
        await asyncio.wait_for(watcher.stop(), timeout=5)
        assert watcher.task is None

    async def test_stop_before_start_is_a_noop(self):
        await ReregistrationWatcher(_ScriptedChannel([READY]), AsyncMock()).stop()


class _StubAgent(BaseAgent):
    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="stub")


class TestAgentServerWiring:
    """The watcher as ``AgentServer`` actually wires it."""

    def _server(self) -> AgentServer:
        server = AgentServer(host="127.0.0.1", port=0, shutdown_grace=1)
        server.register_agent(_StubAgent(agent_id="test-agent", config={}))
        return server

    async def test_reconnect_reregisters_but_never_replays_catchup(self):
        """The no-replay corollary, pinned as behaviour rather than as a comment.

        ``_self_register()`` and ``replay_for_persona_agents()`` are adjacent in
        the startup path. Re-running the tail on reconnect would re-ingest the
        catch-up window on every orchestrator blip, and catch-up has no watermark
        — the unbounded re-derivation ISSUE-0130 shape (a) just bounded, reopened
        through a different door.
        """
        server = self._server()
        server._orchestrator_channel = _ScriptedChannel([READY, IDLE, CONNECTING, READY])

        with (
            patch.object(server, "_self_register", new=AsyncMock()) as self_register,
            patch("agents.server.replay_for_persona_agents", new=AsyncMock()) as replay,
        ):
            server._start_reregistration_watcher()
            assert server._reregister_watcher is not None
            await _finished(server._reregister_watcher)

        self_register.assert_awaited_once()
        replay.assert_not_awaited()

    async def test_no_channel_means_no_watcher(self):
        """Nothing to watch is not an error — the agent still serves."""
        server = self._server()
        server._orchestrator_channel = None
        server._start_reregistration_watcher()
        assert server._reregister_watcher is None


@pytest.mark.parametrize("state", [IDLE, CONNECTING, TRANSIENT_FAILURE])
async def test_any_departure_state_arms_the_reconnect(state):
    """Every non-READY, non-SHUTDOWN state counts as a departure."""
    reregister = await _drive([READY, state, READY])
    reregister.assert_awaited_once()
