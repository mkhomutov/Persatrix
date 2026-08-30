"""Re-register the agent when the orchestrator comes back (ISSUE-0125).

The orchestrator's agent registry lives only in memory, and every agent calls
``_self_register()`` exactly once — in its *own* startup path. So when the
**orchestrator** is the process that restarts, the registry comes back empty and
nobody ever tells it who is out there again. The deployment looks perfectly
healthy: ``/healthz`` answers, the containers are up, publishing a message still
returns ``201``. The personas simply never reply, because every delivery is being
dropped against an empty registry, and the only trace is one warning per dropped
message. On a paid live run that reads as "the persona had nothing to say".

The agent cannot see the dropped deliveries — it is the *target* of a dispatch,
not the caller — but it can see the restart itself, because it already holds a
long-lived gRPC channel to the orchestrator (opened at startup and shared by the
log shipper and the wallet client). When the orchestrator goes away that channel
notices, and when it comes back the channel reconnects. That transition is a
signal sitting unused on a connection that already exists, and this module turns
it into a re-registration.

Two details that decide whether this works at all:

**The trigger is any departure from ``READY`` and a return to it** — deliberately
not the ``READY → TRANSIENT_FAILURE → READY`` cycle the issue's own fix sketch
named. ``TRANSIENT_FAILURE`` is reached only when a connection *attempt* fails.
On a clean orchestrator restart the log shipper's stream ends without an error
and then backs off with no call outstanding, and the wallet client only makes
calls during an LLM turn — so the channel is usually idle when the connection
drops, and an idle channel goes to ``IDLE`` instead. A watcher written to the
literal cycle passes a unit test that injects ``TRANSIENT_FAILURE`` and never
fires in production, which is precisely the failure it was meant to remove.

**Coming back means re-register and nothing else.** In ``AgentServer.start()``
the registration call sits immediately next to the catch-up replay, so the
tempting shape — "re-run the startup tail" — would re-read the missed channel
history on every reconnect. Catch-up has no watermark (RFC 0011 open question 8),
so that is unbounded re-derivation, the exact growth curve ISSUE-0130 closed.
This watcher therefore takes one callback and calls one callback.

It is event-driven rather than a periodic re-register on purpose: a fleet-wide
timer is the polling shape RFC 0024 removed, and it would cost every idle agent a
wake-up forever to catch an event that happens once a month.

**A reconnect gets retries, not one shot.** The watcher wakes on the *gRPC*
channel but registration is a REST POST to a different port, and the
orchestrator brings those two up separately — it is serving gRPC (which is what
woke us) while its REST listener may still be binding. One POST into that window
returns "connection refused", and if that were the end of it the agent would sit
unregistered until the next reconnect, which may never come: the mute fleet,
reached by a new road. So each reconnect retries with a short backoff, and only
a run of failures gives up — loudly, at ERROR.

Nothing here may die quietly. The watch loop is supervised, so an unexpected
error from the channel is logged and retried instead of ending re-registration
for the life of the process, and ``stop()`` never lets a watcher that already
failed abort the agent's shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

import grpc

logger = logging.getLogger("Persatrix.agent.server_reregister")

__all__ = ["ConnectivitySource", "ReregistrationWatcher"]


class ConnectivitySource(Protocol):
    """The two-method slice of ``grpc.aio.Channel`` this watcher reads.

    Declared structurally rather than importing the concrete channel type so the
    watcher depends on the signal it needs and nothing else — and so a test can
    drive it with a scripted state sequence instead of a live connection.
    """

    def get_state(self, try_to_connect: bool = ...) -> grpc.ChannelConnectivity: ...

    async def wait_for_state_change(
        self, last_observed_state: grpc.ChannelConnectivity
    ) -> None: ...

# How long stop() waits for the loop to unwind before giving up on it. The loop
# only ever awaits a channel state change or a short sleep, so cancellation is
# immediate; the timeout exists so agent shutdown cannot be held up by a wedged
# channel.
STOP_TIMEOUT_SEC = 5.0

# How hard one reconnect tries to re-register before giving up on that reconnect.
# The orchestrator is mid-boot at this exact moment — its gRPC port answers (that
# is what woke us) while its REST port may still be binding — so the first
# attempt is the one most likely to hit a closed socket. Roughly 7.5s of total
# wait across five tries: long enough to outlast a listener coming up, short
# enough that the watcher is back to observing the channel well before any
# plausible next flap.
REREGISTER_ATTEMPTS = 5
REREGISTER_BACKOFF_INITIAL_SEC = 0.5
REREGISTER_BACKOFF_CAP_SEC = 4.0

# Backoff for the supervised watch loop itself, when the channel raises something
# unexpected. Mirrors the log shipper's reconnect contract on the same channel
# (agents/observability/log_shipper.py): keep trying, but never hot-loop.
WATCH_RETRY_INITIAL_SEC = 1.0
WATCH_RETRY_CAP_SEC = 30.0


class ReregistrationWatcher:
    """Calls ``reregister`` whenever the watched channel reconnects.

    Args:
        channel: the agent's channel to the orchestrator (in production, the
            ``grpc.aio.Channel`` opened in ``AgentServer.start()``). Note this
            is the *gRPC* endpoint while registration itself is a REST POST to a
            different address — the channel is the restart **signal**, not the
            transport the re-registration travels on.
        reregister: awaited on each reconnect. This is ``_self_register`` and
            must stay that: see the module docstring on catch-up replay. It
            returns True when every hosted agent is registered; False means the
            attempt did not land and is worth retrying.
    """

    def __init__(
        self,
        channel: ConnectivitySource,
        reregister: Callable[[], Awaitable[bool]],
    ) -> None:
        self._channel = channel
        self._reregister = reregister
        self._task: asyncio.Task[None] | None = None
        # Carried on the instance rather than inside the loop so a supervised
        # restart (see _run) resumes with what it already knew. A restart that
        # forgot `_left_ready` would drop the pending reconnect on the floor —
        # a missed re-registration at the exact moment one is owed.
        self._left_ready = False
        self._seen_ready = False

    @property
    def task(self) -> asyncio.Task[None] | None:
        """The watch loop, or ``None`` before start / after stop."""
        return self._task

    def start(self) -> None:
        """Spawn the watch loop. Idempotent; call from a running event loop.

        Synchronous because there is nothing to await — the channel is already
        open (its owner is ``AgentServer``) and all this does is detach the loop.

        A loop that has already finished does not block a fresh one: holding a
        completed task here would mean a watcher could be re-armed by no one.
        """
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="agent-reregister")

    async def stop(self) -> None:
        """Cancel the watch loop and wait for it to unwind. Idempotent.

        Called *before* de-registration during agent shutdown, so a channel that
        flaps on the way down cannot re-register an agent that is leaving.

        Never raises on the watcher's behalf. Awaiting a task re-raises whatever
        it ended with, and this is the FIRST step of ``AgentServer.stop()`` — so
        a watcher that had already failed would otherwise abort the shutdown
        before de-registration, the memory flush and the log-shipper drain, and
        take the evidence of its own failure down with it.
        """
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=STOP_TIMEOUT_SEC)
        except (asyncio.CancelledError, TimeoutError):
            pass  # the expected paths: our cancel landing, or a wedged channel
        except Exception:
            logger.warning(
                "Re-registration watcher had already stopped on an error",
                exc_info=True,
            )

    async def _run(self) -> None:
        """Supervise :meth:`_watch` so an unexpected channel error is not fatal.

        Without this the watcher is one stray exception from being disarmed for
        the life of the process, with nothing in the log to say so — the mute
        fleet again, reached from the inside. Retrying with a backoff keeps a
        genuinely broken channel to one wake-up every 30s instead of a hot loop,
        the same contract the log shipper holds on this very channel.
        """
        backoff = WATCH_RETRY_INITIAL_SEC
        while True:
            try:
                await self._watch()
                return  # clean exit: the channel shut down
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Orchestrator channel watch failed; retrying in %.1fs",
                    backoff,
                    exc_info=True,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, WATCH_RETRY_CAP_SEC)

    async def _watch(self) -> None:
        """Watch the channel; re-register on every return to ``READY``.

        ``_left_ready`` is what keeps a clean boot quiet: the channel connects
        lazily, so its first arrival at ``READY`` happens *after* the agent's own
        startup registration and is not a reconnect.
        """
        state = self._channel.get_state()

        while True:
            if state is grpc.ChannelConnectivity.SHUTDOWN:
                logger.debug("Orchestrator channel shut down; re-registration watcher stopping")
                return
            if state is grpc.ChannelConnectivity.READY:
                if self._left_ready:
                    self._left_ready = False
                    await self._fire()
                self._seen_ready = True
            elif self._seen_ready:
                # Any non-READY state counts. Which one it is says something
                # about *how* the orchestrator went away, not whether it did.
                self._left_ready = True
                logger.info(
                    "Orchestrator channel left READY (%s); will re-register when it returns",
                    state,
                )

            await self._channel.wait_for_state_change(state)
            state = self._channel.get_state()

    async def _fire(self) -> None:
        """Re-register, retrying a few times before giving up on this reconnect.

        One attempt is not enough. The channel that woke us is the orchestrator's
        gRPC port, but registration is a REST POST to a different port that may
        still be binding — and ``_self_register`` reports that as a plain False
        rather than an exception, because it is best-effort by contract and must
        never take the agent down. Giving up after one try would leave the agent
        unregistered until a next reconnect that may never come.
        """
        logger.info("Orchestrator channel reconnected; re-registering")
        backoff = REREGISTER_BACKOFF_INITIAL_SEC
        for attempt in range(1, REREGISTER_ATTEMPTS + 1):
            try:
                if await self._reregister():
                    return
                logger.warning(
                    "Re-registration attempt %d/%d did not land",
                    attempt, REREGISTER_ATTEMPTS,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Re-registration attempt %d/%d raised",
                    attempt, REREGISTER_ATTEMPTS, exc_info=True,
                )
            if attempt < REREGISTER_ATTEMPTS:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, REREGISTER_BACKOFF_CAP_SEC)

        # Out of tries. Say so at ERROR: this agent is now unreachable to every
        # dispatch until the channel flaps again, and that is the condition
        # ISSUE-0125 exists to make impossible to miss.
        logger.error(
            "Re-registration failed %d times after the orchestrator reconnected; "
            "this agent stays unregistered until the channel reconnects again",
            REREGISTER_ATTEMPTS,
        )
