"""
Persatrix LLM-Powered Persona Agent Runtime.

Contains ``_LLMPersonaAgent``, the concrete ``PersonaAgent`` subclass with
LLM-powered ``on_event()`` decision loop, multi-turn tool use, memory
context injection, and state persistence.

Extracted from ``persona.py`` to bring the file under the 500-line code
file-size limit (see ``scripts/checks/file_size.py``).  ``persona.py``
retains the ``PersonaAgent`` ABC and the ``create_persona_agent()`` factory.

Type definitions live in ``persona_types``, behavioral dimension
rendering in ``persona_behavior``, event dispatch in ``dispatch``,
and the tick scheduler in ``tick``.

The runtime class is split across submodules for file-size hygiene:

- ``memory_context`` — memory injection + context-window assembly
- ``action_loop`` — multi-turn tool-use loop, prompt assembly, action parsing
- ``state_persistence`` — state serialisation and memory lifecycle
"""

from __future__ import annotations

__all__ = [
    "Linkable",
    "MemoryNamespace",
    "TICK_REASON_SCHEDULED",
    "TICK_REASON_WOKE_ON_EVENT",
    "_LLMPersonaAgent",
    "_coerce_event_timeout",
    "_truncate_with_ellipsis",
]

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Final, Protocol, runtime_checkable

from opentelemetry import trace
from opentelemetry.trace import Link, Status, StatusCode

from ..llm_client import LLMClient
from ..memory.episodic import EpisodicMemory
from ..memory.relationship import RelationshipMemory
from ..memory.working import WorkingMemory
from ..observability.metrics import (
    event_attrs,
    tick_attrs,
    try_get_instruments,
)
from ..observability.spans import PERSONA_EVENT_SPAN, PERSONA_TICK_SPAN
from ..persona import PersonaAgent
from ..persona_types import (
    ActionType,
    AgentAction,
    AgentEvent,
    EventType,
    PersonaState,
)
from ..tools.registry import ToolDefinition
from .action_loop import _ActionLoopMixin
from .memory_context import _MemoryContextMixin, _truncate_with_ellipsis  # noqa: F401
from .prompt_assembly import _PromptAssemblyMixin
from .state_persistence import _StatePersistenceMixin

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)


# ─── tick.reason vocabulary (single-source) ───────────────
#
# Pinned as ``Final`` so the emitter, the dispatcher, and the test suite all
# reference the same string literals.  Adding a third value requires a
# coordinated rename rather than free-form drift across modules.
TICK_REASON_SCHEDULED: Final[str] = "scheduled"
TICK_REASON_WOKE_ON_EVENT: Final[str] = "woke-on-event"


# ─── Span-link forwarder Protocol ─────────────────────────
#
# ``EventDispatcher.dispatch()`` forwards an event→tick causality
# :class:`opentelemetry.trace.Link` to the agent via
# ``getattr(agent, "add_pending_tick_link", None)``.  Declaring the
# contract as a runtime-checkable :class:`Protocol` lets mypy validate
# the dispatcher end-to-end and lets the dispatcher feature-detect with
# :func:`isinstance` instead of an attribute probe (PR #167 review
# nice-to-have).
@runtime_checkable
class Linkable(Protocol):
    """Agents that can absorb pending tick links from the dispatcher."""

    def add_pending_tick_link(self, link: Link) -> None: ...


# Cap on queued tick links.  PR #167 review *Should Fix*: an unbounded
# buffer combined with a slow / paused tick consumer accumulates links
# indefinitely (memory growth, plus every eventual tick span carries a
# pathological link list).  Oldest-drop semantics preserve the most
# recent causality, which is what operators usually want.
_PENDING_TICK_LINKS_CAP: Final[int] = 32


# ─── Helper Functions ──────────────────────────────────────


def _coerce_event_timeout(
    raw_value: object,
    default: float,
    agent_id: str,
) -> float:
    """Coerce a config-sourced timeout to ``float``.

    YAML configs can supply a string for a numeric key (e.g.
    ``event_timeout: "300"``).  ``asyncio.wait_for(timeout=...)``
    requires a real float; a non-numeric value raises TypeError
    that silently escapes lock acquisition with no useful log.

    Returns *default* with a warning when coercion fails.

    Extracted from ``on_event()`` / ``on_tick()`` where the same
    ``try: float(raw)`` guard was duplicated.
    (PR #60 review: timeout coercion duplicated between on_event/on_tick.)
    """
    try:
        return float(raw_value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning(
            "Agent %s: invalid event_timeout %r, using default %.0fs",
            agent_id, raw_value, default,
        )
        return default


# ─── Memory namespace ─────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MemoryNamespace:
    """Lightweight namespace exposing memory tiers for external callers.

    ``server_servicers.py`` accesses ``agent.memory.relationship`` to record
    chat interactions.  ``_LLMPersonaAgent`` stores the tiers as private
    attributes; this frozen dataclass provides a stable public interface
    without leaking internals.
    """

    episodic: EpisodicMemory
    relationship: RelationshipMemory
    working: WorkingMemory


# Deprecated alias — kept for backward compatibility with external code
# that imported the previously underscored name.  Prefer ``MemoryNamespace``.
_MemoryNamespace = MemoryNamespace


# ─── LLM-Powered Persona Agent ────────────────────────────


class _LLMPersonaAgent(
    _ActionLoopMixin,
    _MemoryContextMixin,
    _PromptAssemblyMixin,
    _StatePersistenceMixin,
    PersonaAgent,
):
    """Concrete PersonaAgent with LLM-powered decision loop.

    Created via ``create_persona_agent()``. Not intended for direct instantiation.
    """

    def __init__(
        self,
        agent_id: str,
        config: dict[str, Any],
        *,
        llm_client: LLMClient,
        episodic_memory: EpisodicMemory,
        relationship_memory: RelationshipMemory,
        working_memory: WorkingMemory,
        memory_tools: list[ToolDefinition],
    ) -> None:
        super().__init__(agent_id, config)
        self._llm_client = llm_client
        self._episodic_memory = episodic_memory
        self._relationship_memory = relationship_memory
        self._working_memory = working_memory
        self._memory_tools = memory_tools
        self._memory_ns = MemoryNamespace(
            episodic=episodic_memory,
            relationship=relationship_memory,
            working=working_memory,
        )
        self._state = PersonaState()
        self._lock = asyncio.Lock()
        # Pending Span Links to attach to the next on_tick() span (RFC 0019
        # § I).  Populated by ``EventDispatcher.dispatch()`` when an event
        # wakes the tick scheduler so the resulting tick can record
        # "event triggered me" causality across the asyncio task boundary.
        # Drained on every on_tick() invocation.
        self._pending_tick_links: list[Link] = []
        # Wall-clock of the previous ``on_tick()`` invocation for the
        # ``agent.persona.tick.interval`` histogram (RFC 0019 § F).
        # ``None`` until the first tick; first sample is then recorded
        # against the second tick so the interval is meaningful.
        self._last_tick_monotonic: float | None = None

    @property
    def memory(self) -> MemoryNamespace:
        """Public access to memory tiers for external callers."""
        return self._memory_ns

    @property
    def persona_state(self) -> dict[str, Any]:
        """Current dynamic state as dict (backward-compatible)."""
        return self._state.to_dict()

    @property
    def state(self) -> PersonaState:
        """Typed access to persona state."""
        return self._state

    def exclusive(self) -> asyncio.Lock:
        """Return the per-agent concurrency lock.

        Public accessor for the internal ``_lock`` so that same-module
        components (``TickScheduler``) can serialize without reaching
        into private attributes.  Called as ``async with agent.exclusive():``.
        (PR #55 review: TickScheduler should use public API for agent lock.)
        """
        return self._lock

    def recover_idle_energy(self) -> None:
        """Recover energy during an idle tick.  Must be called under lock.

        Public API for ``TickScheduler`` so it does not need to reach
        into the private ``_state`` attribute.  Mirrors the internal
        ``self._state.recover_energy()`` call used by ``on_tick()``.
        (PR #55 review: TickScheduler accesses private agent._state.)
        """
        self._state.recover_energy()

    def add_pending_tick_link(self, link: Link) -> None:
        """Queue a Span Link for the next ``on_tick()`` to consume.

        Called by ``EventDispatcher.dispatch()`` after waking the tick
        scheduler so the next tick records ``Link(link.kind="trigger")``
        back to the event that woke it (RFC 0019 § I).  Multiple wakes
        between ticks accumulate up to ``_PENDING_TICK_LINKS_CAP``;
        ``on_tick()`` drains them all.  Oldest entries are dropped once
        the cap is reached so a paused tick consumer cannot leak memory
        (PR #167 review *Should Fix*).
        """
        self._pending_tick_links.append(link)
        if len(self._pending_tick_links) > _PENDING_TICK_LINKS_CAP:
            # Drop the oldest entry.  ``list.pop(0)`` is O(n) but the cap
            # is small (32) and this branch only fires under sustained
            # event-without-tick saturation.
            self._pending_tick_links.pop(0)

    def _consume_pending_tick_links(self) -> list[Link]:
        """Drain queued tick links.  Called once per ``on_tick()``."""
        links = self._pending_tick_links
        self._pending_tick_links = []
        return links

    def _has_active_goal_payload(self) -> bool:
        """Return True if the agent has active goal progress tracked.

        Used by the RFC 0017 §F empty-context TICK short-circuit in
        ``_ActionLoopMixin._on_event_inner``.  See ``docs/rfcs/0017-pr-plan.md``
        §PR 5 for the canonical TICK-handler-pin record.
        """
        return bool(self._state.goal_progress)

    def _has_pending_turn(self) -> bool:
        """Return True if there is recent conversation context pending.

        Used by the RFC 0017 §F empty-context TICK short-circuit in
        ``_on_event_inner()``.  Returns True when ``recent_context`` is
        non-empty, meaning an ongoing conversation exists whose context
        the LLM should still consider on the next tick.  No new
        persisted state — ``recent_context`` is already on
        ``PersonaState`` (and is intentionally NOT persisted to disk).
        """
        return bool(self._state.recent_context)

    # ─── Core event handler ────────────────────────────

    # Default event processing timeout (seconds). Prevents a slow LLM
    # provider combined with multiple tool rounds from holding the per-agent
    # lock indefinitely. Configurable via config["event_timeout"].
    _DEFAULT_EVENT_TIMEOUT: float = 300.0

    async def on_event(self, event: AgentEvent) -> list[AgentAction]:
        """LLM-powered event handler with per-event timeout.

        Wraps ``_on_event_inner()`` in ``asyncio.wait_for()`` to bound
        wall-clock time (PR #54 review: unbounded lock hold).

        Wraps the entire dispatch in an ``agent.persona.event`` OTEL span
        per RFC 0019 § D.  Sub-millisecond phases (received → queued →
        handled → completed) are recorded as **span events** on this single
        span rather than nested spans, keeping the trace tree navigable.
        """
        timeout = _coerce_event_timeout(
            self.config.get("event_timeout", self._DEFAULT_EVENT_TIMEOUT),
            self._DEFAULT_EVENT_TIMEOUT,
            self.agent_id,
        )
        _inst = try_get_instruments()
        if _inst is not None:
            _inst.event_dispatched.add(
                1,
                attributes=event_attrs(
                    agent_id=self.agent_id,
                    event_type=event.event_type.value,
                ),
            )
        with _tracer.start_as_current_span(
            PERSONA_EVENT_SPAN,
            attributes={
                "agent.id": self.agent_id,
                "event.type": event.event_type.value,
            },
        ) as span:
            # Only set ``event.id`` when the event actually carries a
            # message_id — emitting an empty-string attribute pollutes
            # span backends and makes filtering by id harder.
            if event.message_id:
                span.set_attribute("event.id", event.message_id)
            span.add_event("received")
            async with self._lock:
                span.add_event("queued")
                try:
                    actions = await asyncio.wait_for(
                        self._on_event_inner(event), timeout=timeout,
                    )
                    span.add_event(
                        "handled",
                        attributes={"actions.count": len(actions)},
                    )
                    span.add_event("completed")
                    return actions
                except TimeoutError:
                    logger.error(
                        "Agent %s event processing timed out after %.0fs",
                        self.agent_id,
                        timeout,
                    )
                    span.set_status(
                        Status(StatusCode.ERROR, "event timeout"),
                    )
                    span.add_event(
                        "completed",
                        attributes={"event.outcome": "timeout"},
                    )
                    return [AgentAction(
                        action_type=ActionType.COMPLETE_TASK,
                        payload={
                            "result": f"Event processing timed out after {timeout:.0f}s",
                        },
                    )]

    async def on_tick(self) -> list[AgentAction]:
        """Autonomous tick — recovers energy, then decides on actions.

        Wraps ``_on_event_inner()`` in ``asyncio.wait_for()`` with the same
        configurable timeout used by ``on_event()``.  Without this guard a
        slow LLM provider could hold the per-agent lock indefinitely,
        blocking all event processing for the agent.
        (Review finding F-5a-1, resolved in PR 5b.)

        Wrapped in an ``agent.persona.tick`` OTEL span (RFC 0019 § D).
        ``tick.reason`` is derived from the pending link list at tick start:
        ``"woke-on-event"`` when the dispatcher queued at least one trigger
        Link since the last tick, otherwise ``"scheduled"``.  See
        :data:`TICK_REASON_SCHEDULED` / :data:`TICK_REASON_WOKE_ON_EVENT`.
        """
        timeout = _coerce_event_timeout(
            self.config.get("event_timeout", self._DEFAULT_EVENT_TIMEOUT),
            self._DEFAULT_EVENT_TIMEOUT,
            self.agent_id,
        )
        # Drain pending links once so both the link list and the derived
        # ``tick.reason`` attribute see the same snapshot.
        tick_links = self._consume_pending_tick_links()
        with _tracer.start_as_current_span(
            PERSONA_TICK_SPAN,
            links=tick_links,
            attributes={
                "agent.id": self.agent_id,
                # Derive ``tick.reason`` from the link list so the attribute
                # tracks the actual cause without duplicating dispatcher
                # state into the runtime (PR #167 review nice-to-have).
                "tick.reason": (
                    TICK_REASON_WOKE_ON_EVENT if tick_links else TICK_REASON_SCHEDULED
                ),
            },
        ) as span:
            now_monotonic = time.monotonic()
            _inst = try_get_instruments()
            if _inst is not None and self._last_tick_monotonic is not None:
                _inst.persona_tick_interval.record(
                    (now_monotonic - self._last_tick_monotonic) * 1000.0,
                    attributes=tick_attrs(agent_id=self.agent_id),
                )
            self._last_tick_monotonic = now_monotonic
            async with self._lock:
                event = AgentEvent(event_type=EventType.TICK)
                try:
                    actions = await asyncio.wait_for(
                        self._on_event_inner(event), timeout=timeout,
                    )
                except TimeoutError:
                    logger.error(
                        "Agent %s tick timed out after %.0fs",
                        self.agent_id,
                        timeout,
                    )
                    span.set_status(
                        Status(StatusCode.ERROR, "tick timeout"),
                    )
                    # Do NOT recover energy on timeout — the tick produced no
                    # meaningful work.  Recovering before _on_event_inner()
                    # (the previous pattern) leaked +0.1 energy per timed-out
                    # tick because drain_energy() never ran for actions.
                    # (PR #55 review: energy leak on tick timeout.)
                    return [AgentAction(ActionType.DO_NOTHING, {})]
                # Recover energy only after successful completion so timed-out
                # ticks don't accumulate free energy.
                # NEW-L-2 (PR #149 re-review): on a suppressed tick (RFC 0017 §F
                # empty-context short-circuit), _on_event_inner early-returns
                # before _persist_persona_state(), so this energy increment is
                # in-memory only until the next state-mutating event persists.
                # recover_energy() is idempotent, so replay across restart
                # converges to the same value — benign, but worth noting when
                # comparing per-tick DB write rates pre/post RFC 0017 PR 5.
                self._state.recover_energy()
                span.set_attribute("actions.count", len(actions))
                return actions

    # handle() is inherited from PersonaAgent — no override needed.
    # PersonaAgent.handle() wraps tasks as TASK_ASSIGNED events and
    # calls self.on_event(), which dispatches to _on_event_inner()
    # via polymorphism.
