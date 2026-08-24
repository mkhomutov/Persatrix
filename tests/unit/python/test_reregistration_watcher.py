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

from agents import server_reregister
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


def _landed() -> AsyncMock:
    """A re-register callback that reports success, as ``_self_register`` does."""
    return AsyncMock(return_value=True)


async def _drive(states: list[grpc.ChannelConnectivity]) -> AsyncMock:
    """Run a watcher over ``states`` to completion; return the re-register mock."""
    reregister = _landed()
    watcher = ReregistrationWatcher(_ScriptedChannel(states), reregister)
    watcher.start()
    await _finished(watcher)
    return reregister


@pytest.fixture
def _no_backoff(monkeypatch):
    """Collapse the retry waits so a retry test costs no wall time."""
    monkeypatch.setattr(server_reregister, "REREGISTER_BACKOFF_INITIAL_SEC", 0)
    monkeypatch.setattr(server_reregister, "REREGISTER_BACKOFF_CAP_SEC", 0)
    monkeypatch.setattr(server_reregister, "WATCH_RETRY_INITIAL_SEC", 0)
    monkeypatch.setattr(server_reregister, "WATCH_RETRY_CAP_SEC", 0)


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


class TestRetry:
    """One reconnect gets several tries, because one POST is not enough.

    The watcher wakes on the orchestrator's *gRPC* port, but registration is a
    REST POST to a different port that may still be binding — and
    ``_self_register`` reports that as a plain ``False``, never an exception,
    because it is best-effort by contract. A single attempt that lands in that
    window would leave the agent unregistered until a next reconnect that may
    never come: the mute fleet, reached by a new road.
    """

    async def test_a_failed_attempt_is_retried_until_it_lands(self, _no_backoff):
        reregister = AsyncMock(side_effect=[False, False, True])
        watcher = ReregistrationWatcher(_ScriptedChannel([READY, IDLE, READY]), reregister)
        watcher.start()
        await _finished(watcher)
        assert reregister.await_count == 3

    async def test_a_raising_attempt_is_retried_too(self, _no_backoff):
        """A refused connection raises; a 429 returns False. Both get another go."""
        reregister = AsyncMock(side_effect=[ConnectionRefusedError(), True])
        watcher = ReregistrationWatcher(_ScriptedChannel([READY, IDLE, READY]), reregister)
        watcher.start()
        await _finished(watcher)
        assert reregister.await_count == 2

    async def test_success_does_not_spend_the_rest_of_the_budget(self, _no_backoff):
        reregister = await _drive([READY, IDLE, READY])
        reregister.assert_awaited_once()

    async def test_giving_up_is_loud_and_bounded(self, _no_backoff, caplog):
        """Out of tries, the agent is unreachable — that must not be a whisper."""
        reregister = AsyncMock(return_value=False)
        watcher = ReregistrationWatcher(_ScriptedChannel([READY, IDLE, READY]), reregister)
        with caplog.at_level("ERROR", logger="Persatrix.agent.server_reregister"):
            watcher.start()
            await _finished(watcher)
        assert reregister.await_count == server_reregister.REREGISTER_ATTEMPTS
        assert any(r.levelname == "ERROR" for r in caplog.records)

    async def test_a_spent_budget_does_not_disarm_the_next_reconnect(self, _no_backoff):
        """Giving up on THIS reconnect must not give up on the watcher."""
        reregister = AsyncMock(return_value=False)
        watcher = ReregistrationWatcher(
            _ScriptedChannel([READY, IDLE, READY, IDLE, READY]), reregister
        )
        watcher.start()
        await _finished(watcher)
        assert reregister.await_count == 2 * server_reregister.REREGISTER_ATTEMPTS


class _ParkedChannel:
    """READY and never changing again — a live, healthy channel."""

    def get_state(self, try_to_connect: bool = False) -> grpc.ChannelConnectivity:
        return READY

    async def wait_for_state_change(self, last_observed_state) -> None:
        await asyncio.Event().wait()


class _ExplodingChannel:
    """A channel that raises from ``wait_for_state_change`` a fixed number of
    times, the way a closed ``grpc.aio`` channel does, then shuts down."""

    def __init__(self, failures: int):
        self._failures = failures

    def get_state(self, try_to_connect: bool = False) -> grpc.ChannelConnectivity:
        return READY if self._failures else SHUTDOWN

    async def wait_for_state_change(self, last_observed_state) -> None:
        if self._failures:
            self._failures -= 1
            raise grpc.aio.UsageError("Channel is closed.")


class TestResilience:
    async def test_a_channel_error_does_not_disarm_the_watcher(self, _no_backoff):
        """The loop is supervised: an unexpected channel error is retried.

        An unguarded loop is one stray exception away from ending re-registration
        for the life of the process, with nothing in the log to say so — the mute
        fleet again, reached from the inside.
        """
        watcher = ReregistrationWatcher(_ExplodingChannel(failures=3), _landed())
        watcher.start()
        await _finished(watcher)
        task = watcher.task
        assert task is not None and task.exception() is None

    async def test_stop_swallows_a_task_that_ended_in_failure(self):
        """Shutdown must not inherit the watcher's failure.

        ``stop()`` is the FIRST step of ``AgentServer.stop()``, and awaiting a
        task re-raises whatever it ended with — so letting that through would
        skip de-registration, the memory flush and the log-shipper drain, losing
        the very logs that would explain the failure. The supervised loop above
        makes this hard to reach on purpose; the guard is the backstop for when
        it is reached anyway, so it is pinned directly.
        """
        watcher = ReregistrationWatcher(_ParkedChannel(), _landed())

        async def _ended_badly() -> None:
            raise grpc.aio.UsageError("Channel is closed.")

        watcher._task = asyncio.create_task(_ended_badly())
        await asyncio.sleep(0)  # let it fail

        await asyncio.wait_for(watcher.stop(), timeout=5)
        assert watcher.task is None

    async def test_stop_cancels_a_watcher_parked_on_the_channel(self):
        """Shutdown does not hang waiting for a state change that never comes.

        ``_ParkedChannel`` blocks inside ``wait_for_state_change`` forever, which
        is what a live, healthy channel does — so this is a watcher genuinely
        parked, not one that was about to exit anyway.
        """
        watcher = ReregistrationWatcher(_ParkedChannel(), _landed())
        watcher.start()
        await asyncio.sleep(0)  # let the loop reach the park
        await asyncio.wait_for(watcher.stop(), timeout=5)
        assert watcher.task is None

    async def test_start_re_arms_a_loop_that_already_finished(self):
        """A completed task must not hold the door shut against a fresh one."""
        watcher = ReregistrationWatcher(_ScriptedChannel([READY]), _landed())
        watcher.start()
        first = watcher.task
        await _finished(watcher)
        watcher.start()
        assert watcher.task is not first
        await watcher.stop()

    async def test_stop_before_start_is_a_noop(self):
        await ReregistrationWatcher(_ScriptedChannel([READY]), _landed()).stop()


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

    async def test_stop_finishes_its_teardown_even_if_disarming_fails(self):
        """A watcher that cannot be disarmed must not abort the whole shutdown.

        Disarming is the FIRST step of ``AgentServer.stop()`` and was the only
        one not isolated, so an exception escaping it would skip everything
        after: de-registration, the persona memory flush, the shared session
        close and the log-shipper drain.
        """
        server = self._server()
        watcher = ReregistrationWatcher(_ParkedChannel(), _landed())
        server._reregister_watcher = watcher

        with (
            patch.object(watcher, "stop", new=AsyncMock(side_effect=RuntimeError("wedged"))),
            patch.object(server, "_self_deregister", new=AsyncMock()) as deregister,
        ):
            await server.stop()

        deregister.assert_awaited_once()
        assert server._reregister_watcher is None


@pytest.mark.parametrize("state", [IDLE, CONNECTING, TRANSIENT_FAILURE])
async def test_any_departure_state_arms_the_reconnect(state):
    """Every non-READY, non-SHUTDOWN state counts as a departure."""
    reregister = await _drive([READY, state, READY])
    reregister.assert_awaited_once()
