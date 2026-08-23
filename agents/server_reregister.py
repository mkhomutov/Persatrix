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

Registration itself is best-effort by contract, so a failed re-register is logged
and the watcher stays armed for the next reconnect — the orchestrator is by
definition unsteady at this exact moment, and a watcher that dies on the first
failure leaves the fleet mute for good.
"""

from __future__ import annotations

import asyncio
import contextlib
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
# only ever awaits a channel state change, so cancellation is immediate; the
# timeout exists so agent shutdown cannot be held up by a wedged channel.
STOP_TIMEOUT_SEC = 5.0


class ReregistrationWatcher:
    """Calls ``reregister`` whenever the watched channel reconnects.

    Args:
        channel: the agent's channel to the orchestrator (in production, the
            ``grpc.aio.Channel`` opened in ``AgentServer.start()``). Note this
            is the *gRPC* endpoint while registration itself is a REST POST to a
            different address — the channel is the restart **signal**, not the
            transport the re-registration travels on.
        reregister: awaited on each reconnect. This is ``_self_register`` and
            must stay that: see the module docstring on catch-up replay.
    """

    def __init__(
        self,
        channel: ConnectivitySource,
        reregister: Callable[[], Awaitable[None]],
    ) -> None:
        self._channel = channel
        self._reregister = reregister
        self._task: asyncio.Task[None] | None = None

    @property
    def task(self) -> asyncio.Task[None] | None:
        """The watch loop, or ``None`` before start / after stop."""
        return self._task

    def start(self) -> None:
        """Spawn the watch loop. Idempotent; call from a running event loop.

        Synchronous because there is nothing to await — the channel is already
        open (its owner is ``AgentServer``) and all this does is detach the loop.
        """
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="agent-reregister")

    async def stop(self) -> None:
        """Cancel the watch loop and wait for it to unwind. Idempotent.

        Called *before* de-registration during agent shutdown, so a channel that
        flaps on the way down cannot re-register an agent that is leaving.
        """
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(task, timeout=STOP_TIMEOUT_SEC)

    async def _run(self) -> None:
        """Watch the channel; re-register on every return to ``READY``.

        ``left_ready`` is what keeps a clean boot quiet: the channel connects
        lazily, so its first arrival at ``READY`` happens *after* the agent's own
        startup registration and is not a reconnect.
        """
        state = self._channel.get_state()
        left_ready = False
        seen_ready = False

        while True:
            if state is grpc.ChannelConnectivity.SHUTDOWN:
                logger.debug("Orchestrator channel shut down; re-registration watcher stopping")
                return
            if state is grpc.ChannelConnectivity.READY:
                if left_ready:
                    left_ready = False
                    await self._fire()
                seen_ready = True
            elif seen_ready:
                # Any non-READY state counts. Which one it is says something
                # about *how* the orchestrator went away, not whether it did.
                left_ready = True
                logger.info(
                    "Orchestrator channel left READY (%s); will re-register when it returns",
                    state,
                )

            await self._channel.wait_for_state_change(state)
            state = self._channel.get_state()

    async def _fire(self) -> None:
        """Run one re-registration, swallowing its failure."""
        logger.info("Orchestrator channel reconnected; re-registering")
        try:
            await self._reregister()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Best-effort, like the startup registration it repeats. The next
            # reconnect tries again; disarming here would be the mute fleet.
            logger.warning(
                "Re-registration after orchestrator reconnect failed; "
                "will retry on the next reconnect",
                exc_info=True,
            )
