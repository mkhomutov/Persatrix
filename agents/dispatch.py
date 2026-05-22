"""Persatrix Event Dispatcher.

Routes events to persona agents with cascade depth limiting.  The
:class:`ActionExecutor` lives in :mod:`agents.action_executor`; it is
re-exported here so existing callers (``from agents.dispatch import
ActionExecutor``) and the persona facade (:mod:`agents.persona`) keep
working unchanged.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.trace import Link

from .action_executor import ActionExecutor
from .cascade_depth_defaults import DEFAULT_MAX_CASCADE_DEPTH
from .channel_publisher import ChannelPublisher
from .event_loop import InboundEventWake, SyncDispatchHandle
from .persona_types import AgentAction, AgentEvent

if TYPE_CHECKING:
    from .persona_runtime import _LLMPersonaAgent
    from .tick import TickScheduler

logger = logging.getLogger(__name__)

# Cap on in-flight no-running-loop fire-and-forget inbound tasks
# (:meth:`EventDispatcher.enqueue_inbound`). Reactive personas have no
# per-agent ``EventLoop`` to bound channel-message dispatch, so without
# this cap a chatty/abusive producer on the cleartext gRPC port could
# grow ``_inbound_fallback_tasks`` without bound — the same slow-burn DoS
# the RFC 0011 servicer's ``_MAX_PENDING_DISPATCHES`` cap (PR #248 deep
# review Low) defended against for *every* channel dispatch before RFC
# 0024 Phase 4 moved the running-loop path onto the EventLoop's bounded
# queue. Kept at the same value for behavioural parity.
_MAX_INBOUND_FALLBACK_TASKS: int = 1000

# ``DEFAULT_MAX_CASCADE_DEPTH`` is sourced from
# :mod:`agents.cascade_depth_defaults` so the publish-path leaf modules
# (``action_executor``, ``channel_publisher``) can depend on it without
# a circular import through this module. Re-exported here so callers
# can keep using the historical ``from agents.dispatch import …``
# surface.
__all__ = [
    "DEFAULT_MAX_CASCADE_DEPTH",
    "ActionExecutor",
    "ChannelPublisher",
    "EventDispatcher",
]


class EventDispatcher:
    """Routes events to persona agents with cascade depth limiting.

    Prevents infinite event loops by tracking cascade depth in
    ``event.metadata["cascade_depth"]``. Events beyond ``max_cascade_depth``
    are logged and dropped.
    """

    def __init__(
        self,
        agents: dict[str, _LLMPersonaAgent] | None = None,
        max_cascade_depth: int = DEFAULT_MAX_CASCADE_DEPTH,
        channel_publisher: ChannelPublisher | None = None,
    ) -> None:
        self._agents: dict[str, _LLMPersonaAgent] = agents or {}
        self._max_cascade_depth = max_cascade_depth
        self._tick_schedulers: dict[str, TickScheduler] = {}
        self._executor: ActionExecutor = ActionExecutor(
            dispatcher=self, channel_publisher=channel_publisher,
        )
        # Strong-ref anchor for the no-running-loop fire-and-forget
        # fallback in :meth:`enqueue_inbound` — Python 3.11+ GCs
        # weakly-held tasks mid-flight. Tasks add themselves on creation
        # and a done-callback discards them on completion. Bounded by
        # :data:`_MAX_INBOUND_FALLBACK_TASKS` so the set cannot grow
        # without bound on the path the EventLoop queue does not cover.
        self._inbound_fallback_tasks: set[asyncio.Task[None]] = set()

    def set_channel_publisher(self, publisher: ChannelPublisher | None) -> None:
        """Inject the REST publisher post-construction.

        Wired by :meth:`AgentServer.start` once the shared aiohttp session
        is open; keeps ``__init__`` callable from session-less test fixtures.
        """
        self._executor.set_channel_publisher(publisher)

    def register_agent(self, agent_id: str, agent: _LLMPersonaAgent) -> None:
        """Register a persona agent for event dispatch."""
        self._agents[agent_id] = agent

    def register_tick_scheduler(self, agent_id: str, scheduler: TickScheduler) -> None:
        """Register a tick scheduler to wake on incoming events."""
        self._tick_schedulers[agent_id] = scheduler

    def has_tick_scheduler(self, agent_id: str) -> bool:
        """Whether ``agent_id`` has a tick scheduler registered.

        Public counterpart to :meth:`register_tick_scheduler` so callers
        (and the partial-init wiring tests) can query registration without
        reaching into the private ``_tick_schedulers`` dict.
        (PR 2 review (7): test-coupling cleanup.)
        """
        return agent_id in self._tick_schedulers

    @property
    def max_cascade_depth(self) -> int:
        """The configured cascade-depth ceiling.

        ``dispatch()`` and the no-running-loop fallback already read the
        backing ``self._max_cascade_depth``; surfacing it lets the
        running-loop inbound path
        (:meth:`agents.tick.TickScheduler._handle_inbound_event`, reached
        via the executor it holds) honour the same configured ceiling
        instead of hardcoding :data:`DEFAULT_MAX_CASCADE_DEPTH` — one
        source of truth across all three inbound paths.
        (PR 4 review (1): cascade-depth ceiling drift.)
        """
        return self._max_cascade_depth

    @property
    def executor(self) -> ActionExecutor:
        """Public access to the action executor.

        Avoids callers needing to reach into the private ``_executor``
        attribute.  (Review finding: private attribute coupling.)
        """
        return self._executor

    async def dispatch(
        self,
        target_id: str,
        event: AgentEvent,
        *,
        execute_actions: bool = True,
    ) -> list[AgentAction]:
        """Dispatch an event to a target agent, execute resulting actions.

        Creates a deep copy of the event with incremented cascade depth so
        the caller's event object is not mutated. Returns the agent's
        decided actions; action-execution results are handled internally.
        (F-64-DR2-01: clarify return semantics — pre-execution objects.)

        Args:
            execute_actions: When ``False`` the agent's decided actions are
                returned without being passed to ``ActionExecutor.execute()``.
                Used by ``SendChatMessage`` to extract the reply text before
                firing side-effects so the reply is never lost if a
                downstream action raises. (OQ 7)

        .. note::

           **Lock acquisition intentionally at agent level, not dispatcher level.**
           ``on_event()`` already acquires the per-agent lock internally;
           acquiring it here would deadlock because ``asyncio.Lock`` is not
           reentrant. Acceptable for MVP: only ``_LLMPersonaAgent`` exists
           and it always acquires the lock in ``on_event()``.
           (PR #55 review: dispatcher does not acquire per-agent lock.)
        """
        depth = event.metadata.get("cascade_depth", 0)
        if depth >= self._max_cascade_depth:
            logger.warning(
                "Cascade depth %d reached for agent %s, dropping event %s",
                depth, target_id, event.event_type.value,
            )
            return []

        agent = self._agents.get(target_id)
        if agent is None:
            logger.warning(
                "Event dispatch target %s not found (event: %s)",
                target_id, event.event_type.value,
            )
            return []

        # Deep-copy payload + metadata so nested mutable structures are not
        # shared between dispatch targets or with the caller.
        # (F-64-DR2-02: metadata not deep-copied, inconsistent with payload.)
        event = AgentEvent(
            event_type=event.event_type,
            payload=copy.deepcopy(event.payload),
            channel_id=event.channel_id,
            sender_id=event.sender_id,
            message_id=event.message_id,
            thread_id=event.thread_id,
            timestamp=event.timestamp,
            metadata={**copy.deepcopy(event.metadata), "cascade_depth": depth + 1},
        )

        scheduler = self._tick_schedulers.get(target_id)
        if scheduler is not None:
            self._capture_pending_tick_link(agent)

        # RFC 0024 Phase 1: when a TickScheduler is registered *and started*
        # the agent owns a per-agent ``EventLoop``; route through
        # ``event_loop.enqueue`` so inbound events drain the same FIFO as
        # scheduled timer wakes.  The ``SyncDispatchHandle`` preserves the
        # three v0.3.2 contracts: return value (``list[AgentAction]`` for
        # SendChatMessage), await serialisation (``ActionExecutor.dispatch``
        # wraps this in ``asyncio.wait_for``), and queue-mediated ordering.
        #
        # Two fallback cases keep using the direct ``on_event`` call:
        # 1. No scheduler registered — non-autonomous agents under the
        #    v0.3.3 transitional shape.
        # 2. Scheduler registered but not started — test fixtures that
        #    construct a scheduler purely to verify the idle-reset
        #    semantics without booting the event loop.  Calling
        #    ``scheduler.wake()`` preserves the v0.3.2 idle-counter reset
        #    that those tests assert on.
        if scheduler is not None and scheduler.is_running:
            handle = SyncDispatchHandle()
            # RFC 0024 Decided §1 — discard, not block.  ``enqueue`` returns
            # ``False`` when the queue is full; the wake is dropped and the
            # supervisor never resolves the handle.  Awaiting it would hang
            # until the caller's external ``wait_for`` deadline fires
            # (SendChatMessage: clamped timeout; in-process cascade: 60 s
            # default).  Return ``[]`` synchronously so the discard contract
            # holds for chat-style callers too — same shape as the cascade-
            # depth-exceeded / target-not-found drops above.  (PR 1 review
            # finding #1.)
            if not scheduler.event_loop.enqueue(
                InboundEventWake(event=event, handle=handle),
            ):
                # DEBUG, not WARNING: ``EventLoop.enqueue`` already emits a
                # rate-limited queue-full WARNING (agent_id + cumulative
                # dropped_total). A per-drop WARNING here would re-flood the
                # logs PR 5 throttled. (PR 1 review finding #2.)
                logger.debug(
                    "Dispatch dropped (event loop queue full): "
                    "agent=%s event=%s dropped_total=%d",
                    target_id, event.event_type.value,
                    scheduler.event_loop.dropped_count,
                )
                return []
            actions = await handle
        else:
            if scheduler is not None:
                scheduler.wake()
            actions = await agent.on_event(event)

        # Propagate cascade depth into action execution so that
        # SEND_CHANNEL_MESSAGE child dispatches inherit the current depth.
        if execute_actions:
            await self._executor.execute(
                target_id, actions, cascade_depth=depth + 1,
            )

        return actions

    def _capture_pending_tick_link(self, agent: _LLMPersonaAgent | None) -> None:
        """Record event→tick causality as a pending Span Link (RFC 0019 §I).

        Captured at the dispatch / enqueue call site because that is the
        only place running inside the active inbound-event span; the
        agent's next ``on_tick`` consumes the link. Shared by
        :meth:`dispatch` and :meth:`enqueue_inbound`.
        """
        if agent is None:
            return
        current_span = trace.get_current_span()
        ctx = current_span.get_span_context()
        if not ctx.is_valid:
            return
        # Lazy import keeps this module free of a hard runtime dep on the
        # persona subpackage (PR #167 review nice-to-have).
        from .persona_runtime import Linkable

        if isinstance(agent, Linkable):
            agent.add_pending_tick_link(
                Link(ctx, attributes={"link.kind": "trigger"}),
            )

    def enqueue_inbound(self, target_id: str, event: AgentEvent) -> bool:
        """Fire-and-forget inbound dispatch (RFC 0024 Phase 4).

        Channel messages enqueue an :class:`InboundEventWake` *without* a
        :class:`SyncDispatchHandle` directly onto the target agent's
        running :class:`~agents.event_loop.EventLoop`; the loop owns
        processing (decide → execute → recover) via its ``on_inbound``
        callback. The gRPC ``ReceiveChannelMessage`` handler returns its
        ``TaskAck`` as soon as this returns — the agent processes when the
        loop drains its queue. No ``SyncDispatchHandle`` is created on the
        channel path: the handler's only consumer of a return value (the
        ``TaskAck``) does not need the agent's actions (RFC 0024 §E).

        Returns ``True`` if the event was accepted (enqueued, or scheduled
        on the no-running-loop fallback), ``False`` if it was dropped —
        the target agent is unknown, the loop's queue is full
        (discard-not-block, RFC 0024 Decided §1), or — on the no-loop
        fallback — the in-flight task set is at :data:`_MAX_INBOUND_FALLBACK_TASKS`.
        """
        agent = self._agents.get(target_id)
        scheduler = self._tick_schedulers.get(target_id)
        if scheduler is not None and scheduler.is_running:
            self._capture_pending_tick_link(agent)
            accepted = scheduler.event_loop.enqueue(InboundEventWake(event=event))
            if not accepted:
                # DEBUG, not WARNING: the rate-limited queue-full WARNING in
                # ``EventLoop.enqueue`` is the canonical operator signal —
                # channel-message drops are a steady state under RFC 0024
                # Phase 4 backpressure. (PR 1 review finding #2.)
                logger.debug(
                    "Inbound dispatch dropped (event loop queue full): "
                    "agent=%s event=%s dropped_total=%d",
                    target_id, event.event_type.value,
                    scheduler.event_loop.dropped_count,
                )
            return accepted
        # No running loop (non-autonomous agent / session-less fixture):
        # there is no supervisor to drain a fire-and-forget wake, so
        # process inline as a detached task. The gRPC caller still returns
        # immediately; the strong-ref anchor keeps the task alive to
        # completion (Python 3.11+ GCs weakly-held tasks).
        if agent is None:
            logger.warning(
                "Inbound dispatch target %s not found (event: %s)",
                target_id, event.event_type.value,
            )
            return False
        # Discard-not-block, symmetric with the EventLoop queue-full path:
        # the running-loop branch above is bounded by the loop's queue, but
        # this fallback (reactive personas: no running loop) has no such
        # backstop, so cap the in-flight task set here. Checked after agent
        # resolution so cheap rejects run first (PR #248 ordering).
        if len(self._inbound_fallback_tasks) >= _MAX_INBOUND_FALLBACK_TASKS:
            logger.warning(
                "Inbound dispatch dropped (no-loop fallback at capacity): "
                "agent=%s event=%s in_flight=%d",
                target_id, event.event_type.value,
                len(self._inbound_fallback_tasks),
            )
            return False
        task = asyncio.create_task(
            self._process_inbound_no_loop(agent, event),
            name=f"inbound-no-loop:{target_id}",
        )
        self._inbound_fallback_tasks.add(task)
        task.add_done_callback(self._inbound_fallback_tasks.discard)
        return True

    async def _process_inbound_no_loop(
        self, agent: _LLMPersonaAgent, event: AgentEvent,
    ) -> None:
        """No-running-loop fallback body for :meth:`enqueue_inbound`.

        Reuses the same processing coroutine the loop's ``on_inbound``
        callback runs, so the ISSUE-0065/0066 chat-error recovery and the
        cascade-depth guard behave identically whether or not a live
        ``EventLoop`` is draining.
        """
        from .chat_reply import process_inbound_channel_event

        await process_inbound_channel_event(
            agent=agent,
            executor=self._executor,
            event=event,
            max_cascade_depth=self._max_cascade_depth,
        )
