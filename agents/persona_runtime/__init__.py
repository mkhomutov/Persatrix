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
    "_LLMPersonaAgent",
    "_coerce_event_timeout",
    "_truncate_with_ellipsis",
]

import asyncio
import json
import logging
from typing import Any

from ..base import TaskInput
from ..llm_client import LLMClient
from ..memory.episodic import EpisodicMemory
from ..memory.relationship import RelationshipMemory
from ..memory.working import WorkingMemory
from ..persona import PersonaAgent
from ..persona_behavior import render_behavior
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
from .state_persistence import _StatePersistenceMixin

logger = logging.getLogger(__name__)


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


# ─── LLM-Powered Persona Agent ────────────────────────────


class _LLMPersonaAgent(
    _ActionLoopMixin,
    _MemoryContextMixin,
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
        self._state = PersonaState()
        self._lock = asyncio.Lock()

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

    # ─── System prompt assembly ────────────────────────

    def _build_system_prompt(self) -> str:
        """Assemble the full system prompt from persona config, behavior, and state."""
        persona_cfg = self.persona
        parts: list[str] = []

        # Identity
        parts.append(f"You are {self.name}.")
        if persona_cfg.get("title"):
            parts.append(f"Title: {persona_cfg['title']}")
        parts.append(f"Role: {self.role}")

        # Background
        if persona_cfg.get("background"):
            parts.append(f"\nBackground:\n{persona_cfg['background'].strip()}")

        # Behavioral dimensions
        behavior = persona_cfg.get("behavior", {})
        rendered = render_behavior(behavior)
        if rendered:
            parts.append(f"\nCommunication style:\n{rendered}")

        # Quirks
        quirks = persona_cfg.get("quirks", [])
        if quirks:
            parts.append("\nQuirks:")
            for q in quirks:
                parts.append(f"- {q}")

        # Goals
        goals = persona_cfg.get("goals", {})
        if goals:
            parts.append("\nGoals:")
            if goals.get("primary"):
                parts.append(f"- Primary: {goals['primary']}")
            for g in goals.get("secondary", []):
                parts.append(f"- Secondary: {g}")
            if goals.get("hidden"):
                parts.append(f"- Hidden motivation: {goals['hidden']}")

        # Dynamic state
        state_section = self._state.to_prompt_section()
        if state_section:
            parts.append(f"\nCurrent state:\n{state_section}")

        return "\n".join(parts)

    def _format_event(self, event: AgentEvent) -> str:
        """Format an event as a user message for the LLM."""
        match event.event_type:
            case EventType.TASK_ASSIGNED:
                task = event.payload.get("task")
                if isinstance(task, TaskInput):
                    return f"You have been assigned a task:\n\n{task.payload}"
                return f"You have been assigned a task:\n\n{event.payload}"
            case EventType.MESSAGE_RECEIVED:
                # SECURITY: sender_id and content originate from the
                # dispatcher today (trusted).  When external bridges
                # (Slack, Discord, email) are added in v0.2+, these
                # fields will carry untrusted user input — sanitize
                # and length-cap before injecting into the LLM prompt
                # to mitigate prompt injection risks.
                sender = event.sender_id or "unknown"
                content = event.payload.get("content", "")
                return f"Message from {sender}:\n\n{content}"
            case EventType.MENTION:
                sender = event.sender_id or "unknown"
                content = event.payload.get("content", "")
                return f"You were mentioned by {sender}:\n\n{content}"
            case EventType.SUB_AGENT_COMPLETED:
                result = event.payload.get("result", "")
                return f"A sub-agent completed its task:\n\n{result}"
            case EventType.TICK:
                return "Autonomous tick: review your goals and decide on next actions."
            case _:
                try:
                    payload_str = json.dumps(event.payload)
                except TypeError:
                    payload_str = str(event.payload)
                return f"Event ({event.event_type.value}): {payload_str}"

    # ─── Core event handler ────────────────────────────

    # Default event processing timeout (seconds). Prevents a slow LLM
    # provider combined with multiple tool rounds from holding the per-agent
    # lock indefinitely. Configurable via config["event_timeout"].
    _DEFAULT_EVENT_TIMEOUT: float = 300.0

    async def on_event(self, event: AgentEvent) -> list[AgentAction]:
        """LLM-powered event handler with per-event timeout.

        Wraps ``_on_event_inner()`` in ``asyncio.wait_for()`` to bound
        wall-clock time (PR #54 review: unbounded lock hold).
        """
        timeout = _coerce_event_timeout(
            self.config.get("event_timeout", self._DEFAULT_EVENT_TIMEOUT),
            self._DEFAULT_EVENT_TIMEOUT,
            self.agent_id,
        )
        async with self._lock:
            try:
                return await asyncio.wait_for(
                    self._on_event_inner(event), timeout=timeout,
                )
            except TimeoutError:
                logger.error(
                    "Agent %s event processing timed out after %.0fs",
                    self.agent_id,
                    timeout,
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
        """
        timeout = _coerce_event_timeout(
            self.config.get("event_timeout", self._DEFAULT_EVENT_TIMEOUT),
            self._DEFAULT_EVENT_TIMEOUT,
            self.agent_id,
        )
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
                # Do NOT recover energy on timeout — the tick produced no
                # meaningful work.  Recovering before _on_event_inner()
                # (the previous pattern) leaked +0.1 energy per timed-out
                # tick because drain_energy() never ran for actions.
                # (PR #55 review: energy leak on tick timeout.)
                return [AgentAction(ActionType.DO_NOTHING, {})]
            # Recover energy only after successful completion so timed-out
            # ticks don't accumulate free energy.
            self._state.recover_energy()
            return actions

    # handle() is inherited from PersonaAgent — no override needed.
    # PersonaAgent.handle() wraps tasks as TASK_ASSIGNED events and
    # calls self.on_event(), which dispatches to _on_event_inner()
    # via polymorphism.
