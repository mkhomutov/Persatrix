"""
Orchestr8 Persona Agent Interface (v0.2+).

Extends BaseAgent with async event handling, sub-agent spawning,
and autonomous behavior.

Contains the ``PersonaAgent`` ABC and the ``create_persona_agent()``
factory.  The concrete ``_LLMPersonaAgent`` with LLM-powered
``on_event()`` decision loop lives in ``persona_runtime``.

Type definitions live in ``persona_types``, behavioral dimension
rendering in ``persona_behavior``, event dispatch in ``dispatch``,
and the tick scheduler in ``tick``.
"""

from __future__ import annotations

# Public API of this module.  Includes the two locally-defined public
# symbols plus all symbols re-exported from extracted submodules (their
# __all__ lists).  Keeps persona.py consistent with the submodules
# that already define __all__.
# (F-64-DR5-06: no __all__ defined — inconsistent with extracted modules.)
__all__ = [
    # Local public symbols
    "PersonaAgent",
    "create_persona_agent",
    # Re-exported from persona_types
    "ActionType",
    "AgentAction",
    "AgentEvent",
    "EventType",
    "Mood",
    "OrchestratorClient",
    "PersonaState",
    "SubAgentRequest",
    "SubAgentResult",
    "SubAgentStatus",
    # Re-exported from persona_behavior
    "DIMENSION_DESCRIPTIONS",
    "render_behavior",
    # Re-exported from dispatch
    "ActionExecutor",
    "EventDispatcher",
    # Re-exported from tick
    "TickScheduler",
]

import logging
from abc import abstractmethod
from typing import Any

from .base import BaseAgent, TaskInput, TaskOutput, TaskStatus

# Re-export everything that was previously importable from this module
# so that existing ``from agents.persona import X`` statements continue
# to work without modification.  New code should import from the
# specific submodule directly.
# TODO(v0.3): deprecate these re-exports — new code should import from
# specific submodules (persona_types, persona_behavior, dispatch, tick,
# persona_runtime).  Once all internal consumers have migrated, emit
# DeprecationWarning and eventually remove the re-export block.
# (PR #64 review: should fix.)
from .dispatch import ActionExecutor, EventDispatcher  # noqa: F401
from .llm_client import LLMClient
from .memory.episodic import EpisodicMemory
from .memory.relationship import RelationshipMemory
from .memory.working import WorkingMemory
from .persona_behavior import (
    DIMENSION_DESCRIPTIONS,  # noqa: F401
    render_behavior,  # noqa: F401
)
from .persona_types import (
    ActionType,
    AgentAction,
    AgentEvent,
    EventType,
    Mood,  # noqa: F401 — re-exported for backward compatibility
    OrchestratorClient,
    PersonaState,
    SubAgentRequest,
    SubAgentResult,
    SubAgentStatus,  # noqa: F401 — re-exported for backward compatibility (F-64-01)
)
from .tick import TickScheduler  # noqa: F401
from .tools.builtin import create_memory_tools
from .tools.permissions import PermissionGate

logger = logging.getLogger(__name__)


# ─── Persona Agent Base Class ──────────────────────────────

class PersonaAgent(BaseAgent):
    """
    Event-driven agent with persona, memory, and social capabilities.

    Subclass this for persona agents. Override on_event() to define behavior.
    The framework calls on_event() for each incoming event; the agent returns
    one or more actions to execute.

    **``llm_client`` forwarding**: ``PersonaAgent.__init__`` does NOT accept
    ``llm_client``. The concrete subclass ``_LLMPersonaAgent`` receives it
    via its own ``__init__`` and stores it on ``self._llm_client`` directly.
    Subclasses that need LLM access should follow the same pattern or use
    ``create_persona_agent()`` which wires everything.
    (F-5a-3: documented override contract.)
    """

    def __init__(self, agent_id: str, config: dict[str, Any] | None = None):
        super().__init__(agent_id, config)
        self._persona_state: dict[str, Any] = {}
        self._orchestrator_client: OrchestratorClient | None = None  # injected by framework

    # ─── BaseAgent compatibility ───────────────────────

    async def handle(self, task: TaskInput) -> TaskOutput:
        """Backward-compatible: wraps task as a TASK_ASSIGNED event."""
        event = AgentEvent(
            event_type=EventType.TASK_ASSIGNED,
            payload={"task": task},
        )
        actions = await self.on_event(event)

        for action in actions:
            if action.action_type == ActionType.COMPLETE_TASK:
                return TaskOutput(
                    status=TaskStatus.COMPLETED,
                    result=action.payload.get("result", ""),
                    metadata=action.payload.get("metadata", {}),
                )

        action_types = [a.action_type.value for a in actions]
        return TaskOutput(
            status=TaskStatus.FAILED,
            result=f"No COMPLETE_TASK action taken; got actions: {action_types}",
        )

    # ─── Core event handler ────────────────────────────

    @abstractmethod
    async def on_event(self, event: AgentEvent) -> list[AgentAction]:
        """Core event handler. Override this in your persona agent.

        Receives events (messages, tasks, mentions, ticks), returns
        actions (send message, spawn sub-agent, delegate, complete task).
        The framework executes actions and delivers results as new events.

        **Lock contract**: Implementations MUST acquire ``self._lock``
        internally (e.g. ``async with self._lock:``) to serialize event
        processing.  The ``EventDispatcher`` does NOT acquire the lock
        at the dispatch level because ``asyncio.Lock`` is not reentrant
        — acquiring at both layers would deadlock.  If a subclass
        overrides ``on_event()`` without internal locking, concurrent
        dispatches to the same agent will race unserialized.
        (PR #55 review: lock contract fragility for future subclasses.)
        """
        # Using @abstractmethod (consistent with BaseAgent.handle) so missing
        # implementations are caught at instantiation time, not first event.
        ...

    async def on_tick(self) -> list[AgentAction]:
        """
        Called periodically for autonomous agents.
        Default: do nothing. Override for goal-driven behavior.
        """
        return [AgentAction(ActionType.DO_NOTHING, {})]

    # ─── State & Memory ────────────────────────────────

    @property
    def persona_state(self) -> dict[str, Any]:
        """Current dynamic state (mood, stress, goal progress)."""
        return self._persona_state

    @property
    def persona(self) -> dict[str, Any]:
        """Static persona config (background, personality, goals)."""
        result: dict[str, Any] = self.config.get("persona", {})
        return result

    @property
    def relationships(self) -> list[dict[str, Any]]:
        """Relationship definitions with other agents."""
        result: list[dict[str, Any]] = self.config.get("relationships", [])
        return result

    # ─── Sub-Agent Spawning ────────────────────────────

    async def spawn_sub_agent(self, request: SubAgentRequest) -> SubAgentResult:
        """
        Spawn an ephemeral sub-agent for atomic task execution.

        The framework handles:
        - Permission validation (child ≤ parent)
        - Budget deduction from parent's pool
        - Depth/concurrency limit enforcement
        - Process lifecycle (spawn → execute → destroy)
        """
        if self._orchestrator_client is None:
            raise RuntimeError("Orchestrator client not initialized")

        return await self._orchestrator_client.spawn_sub_agent(
            parent_id=self.agent_id,
            request=request,
        )

    # ─── Convenience Methods ───────────────────────────

    def message(
        self,
        channel_id: str,
        content: str,
        message_type: str = "TEXT",
        mentions: list[str] | None = None,
    ) -> AgentAction:
        """Create a SEND_MESSAGE action."""
        return AgentAction(
            action_type=ActionType.SEND_MESSAGE,
            payload={
                "channel_id": channel_id,
                "content": content,
                "type": message_type,
                "mentions": mentions or [],
            },
        )

    def complete(self, result: str, **metadata: Any) -> AgentAction:
        """Create a COMPLETE_TASK action."""
        return AgentAction(
            action_type=ActionType.COMPLETE_TASK,
            payload={"result": result, "metadata": metadata},
        )

    def delegate_to(self, agent_id: str, task: str) -> AgentAction:
        """Create a DELEGATE action."""
        return AgentAction(
            action_type=ActionType.DELEGATE,
            payload={"agent_id": agent_id, "task": task},
        )


# Late import: _LLMPersonaAgent lives in persona_runtime.py which imports
# PersonaAgent from this module.  The import must come AFTER PersonaAgent
# is defined to break the circular dependency.  When Python loads
# persona_runtime.py, it finds PersonaAgent already in this module's
# namespace.  The helpers (_truncate_with_ellipsis, _coerce_event_timeout)
# are also re-exported for backward-compatible imports.
from .persona_runtime import (  # noqa: E402, I001
    _LLMPersonaAgent,
    _coerce_event_timeout,  # noqa: F401 — re-exported for backward-compatible imports
    _truncate_with_ellipsis,  # noqa: F401 — re-exported for backward-compatible imports
)


# ─── Factory ───────────────────────────────────────────────


def create_persona_agent(
    agent_id: str,
    config: dict[str, Any],
    *,
    llm_client: LLMClient,
) -> _LLMPersonaAgent:
    """Factory that creates a concrete PersonaAgent with LLM-powered decision loop.

    Wires up all memory tiers, memory tools, and behavioral dimensions.
    Caller must call ``await agent.initialize_memory()`` before use.
    """
    memory_config = config.get("memory", {})
    db_path = memory_config.get("db_path", "data/memory.db")

    episodic_memory = EpisodicMemory(agent_id=agent_id, db_path=db_path)
    relationship_memory = RelationshipMemory(agent_id=agent_id, db_path=db_path)
    # F-5a-1: Read working memory budget from memory config, not the agent's
    # LLM completion limit (config["max_tokens"]).  These are distinct concerns:
    # config["max_tokens"] caps LLM output tokens (e.g. 4096), while working
    # memory needs the full context-window budget (typically 100k+).
    working_config = memory_config.get("working", {})
    working_memory = WorkingMemory(
        max_tokens=working_config.get("max_tokens", 100_000),
    )

    # Create memory tools with permission gate from agent config
    permissions = config.get("permissions", {})
    gate = PermissionGate(permissions)
    notes_config = memory_config.get("notes", {})
    memory_tools = create_memory_tools(
        episodic_memory,
        gate,
        max_notes=notes_config.get("max_notes", 500),
        auto_reflect_after=notes_config.get("auto_reflect_after", 0),
    )

    return _LLMPersonaAgent(
        agent_id=agent_id,
        config=config,
        llm_client=llm_client,
        episodic_memory=episodic_memory,
        relationship_memory=relationship_memory,
        working_memory=working_memory,
        memory_tools=memory_tools,
    )
