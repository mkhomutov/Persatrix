"""
Persatrix Event Dispatcher and Action Executor.

Routes events to persona agents and executes agent actions.
Extracted from ``persona.py`` for modularity — no logic changes.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.trace import Link

from .observability.spans import SUBAGENT_SPAWN_SPAN
from .persona_types import (
    ActionType,
    AgentAction,
    AgentEvent,
    EventType,
)

if TYPE_CHECKING:
    from .persona_runtime import _LLMPersonaAgent
    from .tick import TickScheduler

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)

__all__ = ["ActionExecutor", "EventDispatcher"]

# Maximum mentions per SEND_MESSAGE action to prevent resource exhaustion
# from LLM-generated payloads.  Each mention triggers a synchronous dispatch
# (per-agent lock + LLM call); with cascade fan-out worst case is N^D.
# (PR #55 review: unbounded mentions list → resource exhaustion.)
_MAX_MENTIONS_PER_ACTION = 10

# Default per-dispatch timeout (seconds) for SEND_MESSAGE cascades.
# Prevents a hung target agent from blocking the sender indefinitely.
# Separate from _DEFAULT_EVENT_TIMEOUT: bounds a single hop, not full event.
# TODO(v0.3): make configurable via config["dispatch_timeout"].
# Hard-coded here as a partial fix for F-5b-4 (PR #55 review: no per-dispatch
# timeout in _handle_send_message()); making it configurable requires the v0.3
# dispatch config schema and is tracked as a deferred item in the PR 7b section
# of docs/rfcs/0005-pr-plan.md.
# (PR #60 review: hard-coded 60s dispatch timeout.)
_DEFAULT_DISPATCH_TIMEOUT: float = 60.0


# ─── Action Executor ──────────────────────────────────────


class ActionExecutor:
    """Executes ``AgentAction`` lists produced by persona agents.

    Handles each action type exhaustively. ``SEND_MESSAGE`` dispatches
    through the ``EventDispatcher`` (if provided) to the target agent.
    ``DELEGATE`` and ``SPAWN_SUB_AGENT`` are TODO stubs for future RFCs.
    """

    def __init__(
        self,
        dispatcher: EventDispatcher | None = None,
    ) -> None:
        self._dispatcher = dispatcher

    async def execute(
        self,
        agent_id: str,
        actions: list[AgentAction],
        *,
        cascade_depth: int = 0,
    ) -> list[dict[str, Any]]:
        """Execute actions and return results.

        Returns a list of dicts, one per action, with ``action_type`` and
        ``status`` fields. Non-fatal failures are logged but do not propagate.

        Args:
            cascade_depth: Current cascade depth from the parent dispatch.
                Propagated to child dispatches via SEND_MESSAGE so the
                cascade depth limit is enforced across the full event chain.
                (PR #55 review: SEND_MESSAGE child events bypassed cascade limit.)
        """
        results: list[dict[str, Any]] = []
        for action in actions:
            result = await self._execute_one(agent_id, action, cascade_depth=cascade_depth)
            results.append(result)
        return results

    async def _execute_one(
        self,
        agent_id: str,
        action: AgentAction,
        *,
        cascade_depth: int = 0,
    ) -> dict[str, Any]:
        """Execute a single agent action and return a status dict.

        Every returned dict contains ``action_type`` (str) and ``status``
        (str).  The status contract:

        * ``"completed"`` — COMPLETE_TASK executed successfully.
        * ``"dispatched"`` — SEND_MESSAGE routed to at least one target.
        * ``"failed"`` — SEND_MESSAGE attempted but all dispatches failed.
        * ``"no_targets"`` — SEND_MESSAGE had no mentioned targets (no-op).
        * ``"no_dispatcher"`` — SEND_MESSAGE with no EventDispatcher configured.
        * ``"skipped"`` — USE_TOOL appeared as a final action (should not happen).
        * ``"ok"`` — DO_NOTHING.
        * ``"not_implemented"`` — DELEGATE, SPAWN_SUB_AGENT, or approval actions.
        * ``"unhandled"`` — Unknown ActionType (defensive catch-all).

        SEND_MESSAGE dicts also include ``dispatched_to`` (int).
        (PR #60 review: document status contract for downstream consumers.)
        """
        match action.action_type:
            case ActionType.COMPLETE_TASK:
                return {
                    "action_type": "complete_task",
                    "status": "completed",
                    "result": action.payload.get("result", ""),
                }
            case ActionType.SEND_MESSAGE:
                return await self._handle_send_message(
                    agent_id, action, cascade_depth=cascade_depth,
                )
            case ActionType.USE_TOOL:
                # Tool execution happens inside _on_event_inner() via
                # _execute_tools(). If USE_TOOL appears as a returned
                # action, it means the LLM wants to use a tool outside
                # the multi-turn loop — log and skip.
                logger.warning(
                    "Agent %s returned USE_TOOL as a final action — "
                    "tool calls should happen inside on_event() loop",
                    agent_id,
                )
                return {
                    "action_type": "use_tool",
                    "status": "skipped",
                }
            case ActionType.DO_NOTHING:
                return {"action_type": "do_nothing", "status": "ok"}
            case ActionType.DELEGATE:
                # TODO(v0.2+): route delegation through orchestrator
                logger.info(
                    "Agent %s requested delegation to %s (not yet implemented)",
                    agent_id,
                    action.payload.get("agent_id", "unknown"),
                )
                return {"action_type": "delegate", "status": "not_implemented"}
            case ActionType.SPAWN_SUB_AGENT:
                # TODO(v0.2+): spawn ephemeral sub-agent
                # The ``agent.subagent.spawn`` span ships now (RFC 0019 § D)
                # so the span name and attribute keys are pinned before the
                # real spawner lands in RFC 0009.  When the spawner ships, the
                # sub-agent's root span will emit a ``Link(link.kind="spawn")``
                # back to the SpanContext captured here.
                _spawn_attrs: dict[str, str] = {
                    "agent.id": agent_id,
                    "subagent.status": "not_implemented",
                }
                # Skip the ``subagent.role`` attribute when unset — emitting
                # an empty string pollutes span backends and makes attribute
                # filters noisier (PR #167 review nice-to-have).
                _role_raw = action.payload.get("role", "")
                _role = str(_role_raw) if _role_raw else ""
                if _role:
                    _spawn_attrs["subagent.role"] = _role
                with _tracer.start_as_current_span(
                    SUBAGENT_SPAWN_SPAN,
                    attributes=_spawn_attrs,
                ):
                    logger.info(
                        "Agent %s requested sub-agent spawn (not yet implemented)",
                        agent_id,
                    )
                return {"action_type": "spawn_sub_agent", "status": "not_implemented"}
            case ActionType.REQUEST_APPROVAL:
                logger.info(
                    "Agent %s requested approval (not yet implemented)",
                    agent_id,
                )
                return {"action_type": "request_approval", "status": "not_implemented"}
            case ActionType.GRANT_APPROVAL:
                logger.info(
                    "Agent %s granted approval (not yet implemented)",
                    agent_id,
                )
                return {"action_type": "grant_approval", "status": "not_implemented"}
            case ActionType.DENY_APPROVAL:
                logger.info(
                    "Agent %s denied approval (not yet implemented)",
                    agent_id,
                )
                return {"action_type": "deny_approval", "status": "not_implemented"}
            case _:
                # Defensive catch-all: Python match is not exhaustive at
                # the type level.  If ActionType gains a new variant without
                # updating this match, the function would implicitly return
                # None — breaking the -> dict[str, Any] contract and causing
                # a TypeError in execute()'s results.append(result).
                # (Review finding: missing catch-all branch.)
                logger.warning(
                    "Agent %s: unhandled action type %s",
                    agent_id, action.action_type.value,
                )
                return {"action_type": action.action_type.value, "status": "unhandled"}

    async def _handle_send_message(
        self,
        sender_id: str,
        action: AgentAction,
        *,
        cascade_depth: int = 0,
    ) -> dict[str, Any]:
        """Route SEND_MESSAGE to the EventDispatcher as a MESSAGE_RECEIVED event."""
        if self._dispatcher is None:
            logger.warning(
                "Agent %s sent message but no dispatcher configured",
                sender_id,
            )
            return {"action_type": "send_message", "status": "no_dispatcher"}

        target_channel = action.payload.get("channel_id", "")
        content = action.payload.get("content", "")
        mentions = action.payload.get("mentions", [])

        # Cap mentions list to prevent resource exhaustion from LLM-generated
        # payloads with many targets.  Each mention triggers a synchronous
        # dispatch (acquiring a per-agent lock + LLM call), and with cascade
        # fan-out the worst case is N^D dispatches where N=mentions and
        # D=max_cascade_depth.
        # (PR #55 review: unbounded mentions list → resource exhaustion.)
        if len(mentions) > _MAX_MENTIONS_PER_ACTION:
            logger.warning(
                "Agent %s SEND_MESSAGE mentions list truncated from %d to %d",
                sender_id,
                len(mentions),
                _MAX_MENTIONS_PER_ACTION,
            )
            mentions = mentions[:_MAX_MENTIONS_PER_ACTION]

        # Route to mentioned agents as MESSAGE_RECEIVED events.
        # Log at WARNING when channel_id is present but mentions is empty —
        # this almost certainly means the LLM intended to route to a channel
        # (not yet implemented), so the message is silently lost.  WARNING
        # makes the drop visible to operators, reducing confusion and wasted
        # LLM budget on undeliverable messages.
        # (PR #55 review: silent message drop when channel_id set without mentions.)
        # Empty mentions is a no-op, not a failure.  Return "no_targets"
        # instead of falling through to the dispatch loop where dispatched=0
        # would produce a misleading "failed" status.  A channel-only message
        # with no mentions is an intentional routing choice (future feature),
        # not an error.  (F-60-R2-2: distinguish no-op from all-failed.)
        if not mentions:
            if target_channel:
                logger.warning(
                    "Agent %s SEND_MESSAGE to channel %s has no mentions — "
                    "message not routed (channel routing not yet implemented)",
                    sender_id,
                    target_channel,
                )
            else:
                logger.debug(
                    "Agent %s SEND_MESSAGE has no mentions, message not routed",
                    sender_id,
                )
            return {
                "action_type": "send_message",
                "status": "no_targets",
                "dispatched_to": 0,
            }
        dispatched = 0
        for target_id in mentions:
            try:
                # Propagate cascade_depth so that cross-agent message
                # chains are bounded by the dispatcher's max_cascade_depth.
                # Without this, each SEND_MESSAGE would restart at depth 0,
                # bypassing the cascade limit entirely.
                # (PR #55 review: cascade depth not propagated through SEND_MESSAGE.)
                event = AgentEvent(
                    event_type=EventType.MESSAGE_RECEIVED,
                    payload={
                        "content": content,
                        "channel_id": target_channel,
                    },
                    channel_id=target_channel,
                    sender_id=sender_id,
                    metadata={"cascade_depth": cascade_depth},
                )
                await asyncio.wait_for(
                    self._dispatcher.dispatch(target_id, event),
                    timeout=_DEFAULT_DISPATCH_TIMEOUT,
                )
                dispatched += 1
            except TimeoutError:
                # Per-dispatch timeout prevents a hung target agent from
                # blocking the sender indefinitely.
                # (F-5b-4: per-dispatch timeout in _handle_send_message.)
                logger.warning(
                    "Dispatch from %s to %s timed out after %.0fs",
                    sender_id, target_id, _DEFAULT_DISPATCH_TIMEOUT,
                )
            except Exception:
                # execute() promises "Non-fatal failures are logged but
                # do not propagate."  Without this guard a single failed
                # dispatch would skip remaining mentions and propagate
                # the exception up to the executor loop.
                # (Review finding: _handle_send_message exception propagation.)
                logger.warning(
                    "Failed to dispatch message from %s to %s",
                    sender_id, target_id, exc_info=True,
                )

        # Use "failed" status when all dispatches timed out or errored,
        # so callers don't see a successful-looking result with dispatched_to=0.
        # (F-60-6: status "dispatched" with dispatched_to=0 is misleading.)
        status = "dispatched" if dispatched > 0 else "failed"
        return {
            "action_type": "send_message",
            "status": status,
            "dispatched_to": dispatched,
        }


# ─── Event Dispatcher ─────────────────────────────────────


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
    ) -> None:
        self._agents: dict[str, _LLMPersonaAgent] = agents or {}
        self._max_cascade_depth = max_cascade_depth
        self._tick_schedulers: dict[str, TickScheduler] = {}
        self._executor: ActionExecutor = ActionExecutor(dispatcher=self)

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

        Creates a shallow copy of the event with incremented cascade depth
        to avoid mutating the caller's event object.  Returns the agent's
        decided actions.  Action execution results (e.g. dispatch
        success/failure) are handled internally and not reflected in
        the return value.
        (F-64-DR2-01: clarify return semantics — pre-execution objects.)

        Args:
            execute_actions: When ``False`` the agent's decided actions are
                returned without being passed to ``ActionExecutor.execute()``.
                This is used by ``SendChatMessage`` to extract the reply text
                before firing side-effects so that the reply is never lost if
                a downstream action raises. (OQ 7)

        .. note::

           **Lock acquisition intentionally at agent level, not dispatcher level.**
           RFC 0005 spec (L382–401) shows ``async with agent.exclusive()`` inside
           ``dispatch()``.  However, ``on_event()`` already acquires the per-agent
           lock internally.  Acquiring it here would deadlock because
           ``asyncio.Lock`` is not reentrant (dispatch → lock → on_event → lock).
           This is acceptable for MVP: only ``_LLMPersonaAgent`` exists, and it
           always acquires the lock in ``on_event()``.  If ``PersonaAgent`` is
           subclassed without internal locking, the dispatcher should be revisited
           to acquire the lock externally — or use a reentrant lock.
           (PR #55 review: dispatcher does not acquire per-agent lock.)

        .. note::

           Memory context (episodic recall, relationship summaries, recent
           notes) is injected into the agent's working memory at the start
           of ``_on_event_inner()`` via ``_inject_memory_context()``.
           (F-5b-1: implemented in PR 7b.)
        """
        depth = event.metadata.get("cascade_depth", 0)
        if depth >= self._max_cascade_depth:
            logger.warning(
                "Cascade depth %d reached for agent %s, dropping event %s",
                depth,
                target_id,
                event.event_type.value,
            )
            return []

        agent = self._agents.get(target_id)
        if agent is None:
            logger.warning(
                "Event dispatch target %s not found (event: %s)",
                target_id,
                event.event_type.value,
            )
            return []

        # Create a shallow copy of event with incremented cascade depth
        # to avoid mutating the caller's event object — prevents incorrect
        # depth tracking if the same event were dispatched to multiple
        # targets or reused.  (Review finding: in-place metadata mutation.)
        # Deep-copy payload to fully isolate nested mutable structures
        # (lists, dicts inside payload values) between dispatch targets.
        # Shallow {**event.payload} only copies top-level keys.
        # (Review finding: shallow copy depth for event payload.)
        # Deep-copy metadata for the same reason: if metadata gains nested
        # mutable structures beyond cascade_depth (e.g. tracing context
        # dicts), shallow spread would share them between caller and copy.
        # (F-64-DR2-02: metadata not deep-copied, inconsistent with payload.)
        event = AgentEvent(
            event_type=event.event_type,
            payload=copy.deepcopy(event.payload),
            channel_id=event.channel_id,
            sender_id=event.sender_id,
            message_id=event.message_id,
            timestamp=event.timestamp,
            metadata={**copy.deepcopy(event.metadata), "cascade_depth": depth + 1},
        )

        # Wake tick scheduler if idle
        scheduler = self._tick_schedulers.get(target_id)
        if scheduler is not None:
            scheduler.wake()
            # RFC 0019 § I: record event→tick causality as a Span Link the
            # next on_tick() consumes.  Captured here (rather than in
            # scheduler.wake()) because the dispatcher is the only call site
            # that runs inside the active event span; the tick loop that
            # later invokes on_tick() runs in a separate asyncio task whose
            # context lacks this span.
            current_span = trace.get_current_span()
            ctx = current_span.get_span_context()
            if ctx.is_valid:
                # ``Linkable`` is a runtime-checkable Protocol declared in
                # :mod:`agents.persona_runtime`; importing lazily keeps the
                # dispatch module free of a hard runtime dep on the persona
                # subpackage (PR #167 review nice-to-have).
                from .persona_runtime import Linkable

                if isinstance(agent, Linkable):
                    agent.add_pending_tick_link(
                        Link(ctx, attributes={"link.kind": "trigger"}),
                    )

        # Deliver event
        actions = await agent.on_event(event)

        # Execute resulting actions, propagating cascade depth so that
        # SEND_MESSAGE actions inherit the current depth for child dispatches.
        # Skipped when execute_actions=False so callers (e.g. SendChatMessage
        # servicer) can inspect actions before firing side-effects. (OQ 7)
        if execute_actions:
            await self._executor.execute(
                target_id, actions, cascade_depth=depth + 1,
            )

        return actions
