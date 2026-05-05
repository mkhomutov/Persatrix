"""Persatrix Event Dispatcher.

Routes events to persona agents with cascade depth limiting.  The
:class:`ActionExecutor` lives in :mod:`agents.action_executor`; it is
re-exported here so existing callers (``from agents.dispatch import
ActionExecutor``) and the persona facade (:mod:`agents.persona`) keep
working unchanged.
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.trace import Link

from .action_executor import ActionExecutor
from .channel_publisher import ChannelPublisher
from .persona_types import AgentAction, AgentEvent

if TYPE_CHECKING:
    from .persona_runtime import _LLMPersonaAgent
    from .tick import TickScheduler

logger = logging.getLogger(__name__)

__all__ = ["ActionExecutor", "ChannelPublisher", "EventDispatcher"]


class EventDispatcher:
    """Routes events to persona agents with cascade depth limiting.

    Prevents infinite event loops by tracking cascade depth in
    ``event.metadata["cascade_depth"]``. Events beyond ``max_cascade_depth``
    are logged and dropped.
    """

    def __init__(
        self,
        agents: dict[str, _LLMPersonaAgent] | None = None,
        max_cascade_depth: int = 5,
        channel_publisher: ChannelPublisher | None = None,
    ) -> None:
        self._agents: dict[str, _LLMPersonaAgent] = agents or {}
        self._max_cascade_depth = max_cascade_depth
        self._tick_schedulers: dict[str, TickScheduler] = {}
        self._executor: ActionExecutor = ActionExecutor(
            dispatcher=self, channel_publisher=channel_publisher,
        )

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
            scheduler.wake()
            # RFC 0019 § I: record event→tick causality as a Span Link the
            # next on_tick() consumes. Captured here because the dispatcher
            # is the only call site that runs inside the active event span.
            current_span = trace.get_current_span()
            ctx = current_span.get_span_context()
            if ctx.is_valid:
                # Lazy import keeps this module free of a hard runtime dep
                # on the persona subpackage (PR #167 review nice-to-have).
                from .persona_runtime import Linkable

                if isinstance(agent, Linkable):
                    agent.add_pending_tick_link(
                        Link(ctx, attributes={"link.kind": "trigger"}),
                    )

        actions = await agent.on_event(event)

        # Propagate cascade depth into action execution so that
        # SEND_CHANNEL_MESSAGE child dispatches inherit the current depth.
        if execute_actions:
            await self._executor.execute(
                target_id, actions, cascade_depth=depth + 1,
            )

        return actions
