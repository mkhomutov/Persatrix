"""
Orchestr8 Persona Agent Interface (v0.2+).

Extends BaseAgent with async event handling, sub-agent spawning,
and autonomous behavior.

Includes the concrete ``_LLMPersonaAgent`` with LLM-powered ``on_event()``
decision loop, ``PersonaState`` dynamic state, behavioral dimension rendering,
``create_persona_agent()`` factory, ``ActionExecutor``, ``EventDispatcher``,
and ``TickScheduler`` for autonomous operation.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
from abc import abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from .base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from .llm_client import LLMClient, LLMResponse, LLMToolResult, StopReason, ToolCall
from .memory.episodic import EpisodicMemory
from .memory.relationship import RelationshipMemory
from .memory.working import WorkingMemory
from .tools.builtin import create_memory_tools
from .tools.permissions import PermissionGate
from .tools.registry import ToolDefinition, get_tool, list_tools

logger = logging.getLogger(__name__)

# ─── Events that a persona agent can receive ───────────────

class EventType(Enum):
    TASK_ASSIGNED = "task_assigned"
    MESSAGE_RECEIVED = "message_received"
    MENTION = "mention"
    SUB_AGENT_COMPLETED = "sub_agent_completed"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESPONSE = "approval_response"
    TICK = "tick"  # autonomous loop heartbeat
    AGENT_JOINED = "agent_joined"
    AGENT_LEFT = "agent_left"


@dataclass
class AgentEvent:
    """An event delivered to a persona agent."""

    event_type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    channel_id: str | None = None
    sender_id: str | None = None
    message_id: str | None = None
    # default_factory=time.time ensures each event gets the current timestamp
    # rather than a sentinel 0.0 that callers might forget to override.
    timestamp: float = field(default_factory=time.time)
    # Extensible metadata for cross-cutting concerns (e.g. cascade_depth
    # tracking from Q4 decision, tracing correlation IDs). Using a dict
    # avoids adding a new field for every framework-level concern.
    metadata: dict[str, Any] = field(default_factory=dict)


# ─── Actions that a persona agent can take ─────────────────

class ActionType(Enum):
    SEND_MESSAGE = "send_message"
    COMPLETE_TASK = "complete_task"
    DELEGATE = "delegate"
    SPAWN_SUB_AGENT = "spawn_sub_agent"
    USE_TOOL = "use_tool"
    REQUEST_APPROVAL = "request_approval"
    GRANT_APPROVAL = "grant_approval"
    DENY_APPROVAL = "deny_approval"
    DO_NOTHING = "do_nothing"


@dataclass
class AgentAction:
    """An action a persona agent wants to execute."""

    action_type: ActionType
    payload: dict[str, Any] = field(default_factory=dict)


# ─── Sub-Agent Types ───────────────────────────────────────

@dataclass
class SubAgentRequest:
    """Request to spawn an ephemeral sub-agent."""

    role: str
    task: str
    tools: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    output_schema: dict | None = None
    model: str = "claude-sonnet-4-20250514"
    temperature: float = 0.2
    max_llm_calls: int = 10
    max_tokens: int = 50000
    timeout_seconds: int = 120
    inherit_permissions: bool = True
    restricted_permissions: list[str] = field(default_factory=list)


class SubAgentStatus(Enum):
    """Status of a sub-agent execution."""

    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class SubAgentResult:
    """Result from an ephemeral sub-agent."""

    status: SubAgentStatus
    result: Any = None
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# ─── Orchestrator Client Protocol ──────────────────────────

@runtime_checkable
class OrchestratorClient(Protocol):
    """
    Protocol defining the interface for the orchestrator client injected
    into PersonaAgent. Using a Protocol (structural typing) instead of Any
    enables type checking and makes the expected interface self-documenting
    for testing.

    PR review: _orchestrator_client was typed as Any, providing zero type
    safety and making it impossible to verify mocks implement the contract.
    """

    async def spawn_sub_agent(
        self, *, parent_id: str, request: SubAgentRequest
    ) -> SubAgentResult: ...


# ─── Persona Agent Base Class ──────────────────────────────

class PersonaAgent(BaseAgent):
    """
    Event-driven agent with persona, memory, and social capabilities.

    Subclass this for persona agents. Override on_event() to define behavior.
    The framework calls on_event() for each incoming event; the agent returns
    one or more actions to execute.
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


# ─── Dynamic Persona State ────────────────────────────────


class Mood(Enum):
    """Constrained mood states. Each maps to known prompt behavior."""

    NEUTRAL = "neutral"
    FOCUSED = "focused"
    FRUSTRATED = "frustrated"
    ENERGIZED = "energized"
    UNCERTAIN = "uncertain"
    SATISFIED = "satisfied"


# Energy constants (MVP — hardcoded, config-driven in follow-up).
_ENERGY_COST_PER_ACTION = 0.05
_ENERGY_RECOVERY_PER_TICK = 0.1

# Maximum mentions per SEND_MESSAGE action to prevent resource exhaustion
# from LLM-generated payloads.  Each mention triggers a synchronous dispatch
# (per-agent lock + LLM call); with cascade fan-out worst case is N^D.
# (PR #55 review: unbounded mentions list → resource exhaustion.)
_MAX_MENTIONS_PER_ACTION = 10


@dataclass
class PersonaState:
    """Mutable runtime state for a persona agent."""

    mood: Mood = Mood.NEUTRAL
    stress_level: float = 0.0
    energy: float = 1.0
    recent_context: list[str] = field(default_factory=list)
    goal_progress: dict[str, float] = field(default_factory=dict)

    def to_prompt_section(self) -> str:
        """Format state for injection into system prompt."""
        lines = [f"Current mood: {self.mood.value}"]
        if self.stress_level > 0.3:
            lines.append(f"Stress level: {self.stress_level:.1f}/1.0")
        if self.energy < 0.5:
            lines.append(
                f"Energy level: {self.energy:.1f}/1.0 — conserve effort, prefer delegation"
            )
        if self.recent_context:
            lines.append("Recent context:")
            for ctx in self.recent_context[-5:]:
                lines.append(f"  - {ctx}")
        if self.goal_progress:
            lines.append("Goal progress:")
            for goal, progress in self.goal_progress.items():
                lines.append(f"  - {goal}: {progress:.0%}")
        return "\n".join(lines)

    def drain_energy(self) -> None:
        """Drain energy after an action. Clamped to [0.0, 1.0]."""
        self.energy = max(0.0, self.energy - _ENERGY_COST_PER_ACTION)

    def recover_energy(self) -> None:
        """Recover energy on idle tick. Clamped to [0.0, 1.0]."""
        self.energy = min(1.0, self.energy + _ENERGY_RECOVERY_PER_TICK)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for persistence. ``recent_context`` is NOT persisted."""
        return {
            "mood": self.mood.value,
            "stress_level": self.stress_level,
            "energy": self.energy,
            "goal_progress": self.goal_progress,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersonaState:
        """Deserialize from stored JSON. Unknown fields are ignored.

        Clamps ``energy`` and ``stress_level`` to [0.0, 1.0] to guard
        against corrupted or manually edited DB data (review finding:
        out-of-range values would produce broken prompt sections).
        """
        mood_str = data.get("mood", "neutral")
        try:
            mood = Mood(mood_str)
        except ValueError:
            logger.warning("Unknown mood %r, defaulting to NEUTRAL", mood_str)
            mood = Mood.NEUTRAL
        raw_goals = data.get("goal_progress", {})
        goal_progress: dict[str, float] = {}
        for k, v in raw_goals.items():
            try:
                goal_progress[k] = float(v)
            except (TypeError, ValueError):
                logger.warning("Invalid goal_progress value for %r: %r, skipping", k, v)
        return cls(
            mood=mood,
            stress_level=min(1.0, max(0.0, float(data.get("stress_level", 0.0)))),
            energy=min(1.0, max(0.0, float(data.get("energy", 1.0)))),
            goal_progress=goal_progress,
        )


# ─── Behavioral Dimensions ────────────────────────────────


DIMENSION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "directness": {
        "indirect": (
            "Diplomatic and tactful. Softens criticism, asks questions"
            " instead of stating objections directly."
        ),
        "balanced": (
            "Balances directness with tact. States positions clearly"
            " but frames feedback constructively."
        ),
        "direct": (
            "Says exactly what they think."
            " Doesn't sugarcoat feedback or hedge opinions."
        ),
    },
    "detail_focus": {
        "big-picture": (
            "Focuses on high-level patterns and architecture."
            " Skips minutiae to keep discussions strategic."
        ),
        "balanced": (
            "Addresses both high-level concerns and"
            " specific details as needed."
        ),
        "detail-focused": (
            "Thorough and meticulous. Flags edge cases,"
            " checks specifics, prefers exhaustive analysis."
        ),
    },
    "formality": {
        "casual": (
            "Informal and approachable. Uses humor,"
            " contractions, and conversational language."
        ),
        "professional": (
            "Clear and structured. Uses professional"
            " language without being stiff."
        ),
        "formal": (
            "Precise and formal. Uses structured reports,"
            " proper titles, and measured language."
        ),
    },
    "risk_tolerance": {
        "cautious": (
            "Wants thorough analysis before decisions."
            " Asks for more data. Flags risks others might overlook."
        ),
        "moderate": (
            "Balances speed with diligence."
            " Comfortable with reasonable assumptions."
        ),
        "bold": (
            "Willing to make calls with incomplete information"
            " and course-correct. Bias toward action."
        ),
    },
    "expressiveness": {
        "reserved": (
            "Keeps emotions out of professional communication."
            " Focuses on facts and logic."
        ),
        "moderate": (
            "Acknowledges emotions when relevant"
            " but keeps focus on substance."
        ),
        "expressive": (
            "Openly shares reactions and feelings. Communication"
            " is warm, enthusiastic, or frustrated as the"
            " situation warrants."
        ),
    },
}

# Default middle value for each dimension when not specified.
_DIMENSION_DEFAULTS: dict[str, str] = {
    "directness": "balanced",
    "detail_focus": "balanced",
    "formality": "professional",
    "risk_tolerance": "moderate",
    "expressiveness": "moderate",
}


def render_behavior(behavior: dict[str, str]) -> str:
    """Convert structured behavior dimensions into natural language for LLM prompt.

    Applies defaults for omitted dimensions so the persona always has
    a complete behavioral profile.  Unknown dimension keys from ``behavior``
    are logged as warnings to aid config debugging.
    """
    merged = {**_DIMENSION_DEFAULTS, **behavior}
    lines: list[str] = []
    for dimension, value in merged.items():
        if dimension not in DIMENSION_DESCRIPTIONS:
            logger.warning(
                "Unknown behavior dimension %r (value=%r) — ignored",
                dimension,
                value,
            )
            continue
        desc = DIMENSION_DESCRIPTIONS[dimension].get(value)
        if desc:
            lines.append(f"- {desc}")
    return "\n".join(lines)


# ─── LLM-Powered Persona Agent ────────────────────────────


class _LLMPersonaAgent(PersonaAgent):
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

    def _build_tool_definitions(self) -> list[dict[str, Any]]:
        """Build tool definitions including memory tools.

        Uses a dict keyed by tool name so memory tools take precedence
        over registry tools with the same name (review finding #4).
        """
        # Start with agent-configured tools from the global registry
        allowed = set(self.config.get("tools", []))
        defs_by_name: dict[str, dict[str, Any]] = {}

        for td in list_tools():
            if td.name in allowed:
                defs_by_name[td.name] = {
                    "name": td.name,
                    "description": td.description,
                    "parameters": td.parameters,
                }

        # Memory tools override registry tools with the same name,
        # consistent with _execute_tools() which checks memory tools first.
        for td in self._memory_tools:
            defs_by_name[td.name] = {
                "name": td.name,
                "description": td.description,
                "parameters": td.parameters,
            }

        return list(defs_by_name.values())

    async def _execute_tools(self, tool_calls: list[ToolCall]) -> list[LLMToolResult]:
        """Execute tool calls, checking memory tools first then registry.

        Registry lookups are restricted to tools in ``config["tools"]``
        (F-5a-2: defense-in-depth against LLM hallucinating tool names
        that exist in the global registry but weren't offered to this agent).
        """
        memory_tool_map = {td.name: td for td in self._memory_tools}
        allowed_tools = set(self.config.get("tools", []))
        results: list[LLMToolResult] = []

        for call in tool_calls:
            # Check memory tools first (always allowed)
            tool_def = memory_tool_map.get(call.name)
            if tool_def is None and call.name in allowed_tools:
                tool_def = get_tool(call.name)

            if tool_def is None or tool_def.func is None:
                results.append(LLMToolResult(
                    tool_call_id=call.id,
                    content=f"Unknown tool: {call.name}",
                    is_error=True,
                ))
                continue

            try:
                result = await tool_def.func(**call.input)
                if result.success:
                    content = (
                        json.dumps(result.data)
                        if isinstance(result.data, (dict, list))
                        else str(result.data)
                    )
                else:
                    error_msg = result.error or "Tool failed"
                    if result.error_type:
                        content = f"Tool error ({result.error_type}): {error_msg}"
                    else:
                        content = error_msg
                results.append(LLMToolResult(
                    tool_call_id=call.id,
                    content=content,
                    is_error=not result.success,
                ))
            except Exception as exc:
                logger.warning("Unexpected error in tool %s: %s", call.name, exc)
                results.append(LLMToolResult(
                    tool_call_id=call.id,
                    content="Internal tool error",
                    is_error=True,
                ))

        return results

    # Agent ID format shared with server.py — cross-component contract.
    _AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")

    def _validate_action_payload(self, action: AgentAction) -> AgentAction:
        """Validate LLM-generated action payloads, replacing invalid ones with DO_NOTHING.

        Enforces required fields per action type to prevent malformed LLM output
        from reaching downstream execution (PR #54 review: unvalidated payloads).
        """
        p = action.payload
        match action.action_type:
            case ActionType.DELEGATE:
                agent_id = p.get("agent_id")
                if not isinstance(agent_id, str) or not self._AGENT_ID_RE.match(agent_id):
                    logger.warning(
                        "DELEGATE action has invalid agent_id %r, replacing with DO_NOTHING",
                        agent_id,
                    )
                    return AgentAction(ActionType.DO_NOTHING, {})
                if not isinstance(p.get("task"), str) or not p["task"].strip():
                    logger.warning(
                        "DELEGATE action missing non-empty 'task', replacing with DO_NOTHING",
                    )
                    return AgentAction(ActionType.DO_NOTHING, {})
            case ActionType.SEND_MESSAGE:
                if not isinstance(p.get("channel_id"), str) or not p["channel_id"].strip():
                    logger.warning(
                        "SEND_MESSAGE missing non-empty 'channel_id',"
                        " replacing with DO_NOTHING",
                    )
                    return AgentAction(ActionType.DO_NOTHING, {})
                if not isinstance(p.get("content"), str) or not p["content"].strip():
                    logger.warning(
                        "SEND_MESSAGE missing non-empty 'content',"
                        " replacing with DO_NOTHING",
                    )
                    return AgentAction(ActionType.DO_NOTHING, {})
            case ActionType.SPAWN_SUB_AGENT:
                if not isinstance(p.get("role"), str) or not p["role"].strip():
                    logger.warning(
                        "SPAWN_SUB_AGENT missing non-empty 'role',"
                        " replacing with DO_NOTHING",
                    )
                    return AgentAction(ActionType.DO_NOTHING, {})
                if not isinstance(p.get("task"), str) or not p["task"].strip():
                    logger.warning(
                        "SPAWN_SUB_AGENT missing non-empty 'task',"
                        " replacing with DO_NOTHING",
                    )
                    return AgentAction(ActionType.DO_NOTHING, {})
            case _:
                pass  # COMPLETE_TASK, DO_NOTHING, approvals — no payload constraints
        return action

    def _parse_actions(self, response: LLMResponse) -> list[AgentAction]:
        """Parse LLM response text into AgentAction list.

        The LLM is expected to return a JSON array of actions. Falls back
        to a single COMPLETE_TASK with the raw text if parsing fails.
        Parsed actions are validated per action type before returning.
        """
        text = response.text or ""
        # Try to extract JSON action array from the response
        try:
            # Look for a JSON array in the response
            stripped = text.strip()
            if stripped.startswith("["):
                raw_actions = json.loads(stripped)
            elif "```json" in stripped:
                # Use regex to extract the first JSON code block — more robust
                # than str.index() against nested fences (review finding P-1).
                # Newline anchors (not \s*) to avoid polynomial backtracking on
                # pathological input with many backtick sequences (PR #54 review).
                m = re.search(r"```json\n(.*?)\n```", stripped, re.DOTALL)
                if m is None:
                    return [AgentAction(
                        action_type=ActionType.COMPLETE_TASK,
                        payload={"result": text},
                    )]
                raw_actions = json.loads(m.group(1))
            else:
                # Treat the whole response as a COMPLETE_TASK result
                return [AgentAction(
                    action_type=ActionType.COMPLETE_TASK,
                    payload={"result": text},
                )]

            actions: list[AgentAction] = []
            for raw in raw_actions:
                try:
                    action_type = ActionType(raw.get("action_type", "do_nothing"))
                except ValueError:
                    logger.warning("Unknown action_type %r, skipping", raw.get("action_type"))
                    continue
                # Validate payload per action type (PR #54 review: unvalidated
                # LLM output). Full ActionExecutor validation deferred to PR 5b;
                # this enforces required-field constraints at parse time.
                validated = self._validate_action_payload(AgentAction(
                    action_type=action_type,
                    payload=raw.get("payload", {}),
                ))
                actions.append(validated)
            return actions if actions else [AgentAction(
                action_type=ActionType.COMPLETE_TASK,
                payload={"result": text},
            )]

        except (json.JSONDecodeError, ValueError):
            return [AgentAction(
                action_type=ActionType.COMPLETE_TASK,
                payload={"result": text},
            )]

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
        timeout = self.config.get("event_timeout", self._DEFAULT_EVENT_TIMEOUT)
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

    async def _on_event_inner(self, event: AgentEvent) -> list[AgentAction]:
        """Inner event handler — must be called under self._lock."""
        if self._llm_client is None:
            return [AgentAction(
                action_type=ActionType.COMPLETE_TASK,
                payload={"result": "LLM client not configured"},
            )]

        # Fail-fast for missing model config — a bare KeyError from
        # self.config["model"] deep inside the LLM call produces an
        # unclear traceback.  Matches BaseAgent._run_llm_loop() SF2 pattern.
        if "model" not in self.config:
            return [AgentAction(
                action_type=ActionType.COMPLETE_TASK,
                payload={"result": "Agent config missing required 'model' field"},
            )]

        # 1. Build system prompt
        system_prompt = self._build_system_prompt()

        # 2. Format the event as a user message
        user_message = self._format_event(event)

        # 3. Multi-turn tool-use loop
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_message},
        ]
        tool_defs = self._llm_client.format_tool_definitions(
            self._build_tool_definitions()
        )

        max_llm_calls = self.config.get("max_llm_calls", 10)
        max_tokens = self.config.get("max_tokens", 4096)

        response: LLMResponse | None = None
        for _ in range(max_llm_calls):
            try:
                response = await self._llm_client.create_message(
                    model=self.config["model"],
                    messages=messages,
                    system=system_prompt,
                    tools=tool_defs,
                    max_tokens=max_tokens,
                    temperature=self.config.get("temperature", 0.7),
                )
            except Exception as exc:
                logger.error("LLM provider error in agent %s: %s", self.agent_id, exc)
                return [AgentAction(
                    action_type=ActionType.COMPLETE_TASK,
                    payload={"result": "LLM provider error"},
                )]

            if response.stop_reason == StopReason.TOOL_USE:
                tool_results = await self._execute_tools(response.tool_calls)
                messages = self._llm_client.append_tool_round(
                    messages, response, tool_results
                )
                continue

            # END_TURN or MAX_TOKENS — break out
            break

        if response is None:
            return [AgentAction(
                action_type=ActionType.COMPLETE_TASK,
                payload={"result": "No LLM response"},
            )]

        # 3a. Handle MAX_TOKENS — the LLM truncated its response before
        # completing.  Parsing truncated text as actions would produce
        # malformed JSON that silently falls back to COMPLETE_TASK with
        # garbage content.  Return a descriptive action instead, consistent
        # with BaseAgent._run_llm_loop() which returns FAILED for MAX_TOKENS.
        if response.stop_reason == StopReason.MAX_TOKENS:
            logger.warning(
                "Agent %s response truncated (max_tokens)", self.agent_id,
            )
            return [AgentAction(
                action_type=ActionType.COMPLETE_TASK,
                payload={"result": "Response truncated: max_tokens limit reached"},
            )]

        # 3b. Detect max_llm_calls exhaustion: if the loop ended while
        # the LLM was still requesting tool use, the budget was hit
        # without a natural stop. Log a warning and set a descriptive
        # fallback so callers can distinguish this from a normal empty
        # completion (review finding: silent budget exhaustion).
        if response.stop_reason == StopReason.TOOL_USE:
            logger.warning(
                "Agent %s exhausted max_llm_calls=%d without natural stop",
                self.agent_id,
                max_llm_calls,
            )
            response = LLMResponse(
                text=f"Max LLM call budget exhausted after {max_llm_calls} iterations",
                stop_reason=StopReason.END_TURN,
                usage=response.usage,
            )

        # 4. Parse actions
        actions = self._parse_actions(response)

        # 5. Drain energy per action
        for action in actions:
            if action.action_type != ActionType.DO_NOTHING:
                self._state.drain_energy()

        # 6. Store episode
        try:
            await self._episodic_memory.store_episode(
                summary=(
                    f"Event: {event.event_type.value} → "
                    f"Actions: {[a.action_type.value for a in actions]}"
                ),
                context={"event": event.payload, "sender": event.sender_id},
            )
        except Exception:
            logger.warning("Failed to store episode for agent %s", self.agent_id, exc_info=True)

        # 7. Persist state
        await self._persist_persona_state()

        return actions

    async def on_tick(self) -> list[AgentAction]:
        """Autonomous tick — recovers energy, then decides on actions.

        Wraps ``_on_event_inner()`` in ``asyncio.wait_for()`` with the same
        configurable timeout used by ``on_event()``.  Without this guard a
        slow LLM provider could hold the per-agent lock indefinitely,
        blocking all event processing for the agent.
        (Review finding F-5a-1, resolved in PR 5b.)
        """
        timeout = self.config.get("event_timeout", self._DEFAULT_EVENT_TIMEOUT)
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

    # ─── State persistence ─────────────────────────────

    async def _persist_persona_state(self) -> None:
        """Serialize persona state to the agent_state table.

        Uses EpisodicMemory's public ``persist_agent_state()`` API rather
        than reaching into its private DB handle (review finding #3).
        """
        try:
            state_json = json.dumps(self._state.to_dict())
            await self._episodic_memory.persist_agent_state(
                self.agent_id, state_json,
            )
        except Exception:
            logger.warning(
                "Failed to persist persona state for agent %s",
                self.agent_id,
                exc_info=True,
            )

    async def _load_persona_state(self) -> PersonaState:
        """Load persona state from the agent_state table, or return defaults.

        Uses EpisodicMemory's public ``load_agent_state()`` API.
        """
        try:
            state_json = await self._episodic_memory.load_agent_state(
                self.agent_id,
            )
            if state_json:
                return PersonaState.from_dict(json.loads(state_json))
        except Exception:
            logger.warning(
                "Failed to load persona state for agent %s, using defaults",
                self.agent_id,
                exc_info=True,
            )
        return PersonaState()

    # ─── Memory lifecycle ──────────────────────────────

    async def initialize_memory(self) -> None:
        """Initialize all memory tiers and load persisted state."""
        await self._episodic_memory.initialize()
        await self._relationship_memory.initialize(
            config_relationships=self.config.get("relationships"),
        )
        await self._working_memory.initialize()
        self._state = await self._load_persona_state()

    async def close_memory(self) -> None:
        """Close all memory tiers, awaiting in-flight operations.

        Each tier is closed in its own try/except so that a failure in one
        tier (e.g. disk-full on SQLite) does not prevent the remaining
        tiers from releasing their resources (PR #54 review).
        """
        async with self._lock:
            await self._persist_persona_state()
            errors: list[Exception] = []
            # Close order: working (flush compression) → episodic (DB) → relationship (DB)
            for tier in (self._working_memory, self._episodic_memory, self._relationship_memory):
                try:
                    await tier.close()
                except Exception as exc:
                    errors.append(exc)
                    logger.warning("Failed to close memory tier: %s", exc)
            if errors:
                logger.error(
                    "Memory close for agent %s completed with %d error(s)",
                    self.agent_id,
                    len(errors),
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
                await self._dispatcher.dispatch(target_id, event)
                dispatched += 1
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

        return {
            "action_type": "send_message",
            "status": "dispatched",
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
    ) -> list[AgentAction]:
        """Dispatch an event to a target agent, execute resulting actions.

        Creates a shallow copy of the event with incremented cascade depth
        to avoid mutating the caller's event object.  Returns the agent's
        actions (post-execution).

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

           TODO(v0.2): Inject memory context (episodic recall, relationship
           summaries, recent notes) into the agent's working memory before
           event handling — see RFC 0005 Phase 5b ``_inject_memory_context()``.
           Deferred from this PR; persona agents currently rely on explicit
           memory tool calls for context retrieval.
           Tracked as F-5b-1 in PR plan for PR 7 follow-up.
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
        event = AgentEvent(
            event_type=event.event_type,
            payload=copy.deepcopy(event.payload),
            channel_id=event.channel_id,
            sender_id=event.sender_id,
            message_id=event.message_id,
            timestamp=event.timestamp,
            metadata={**event.metadata, "cascade_depth": depth + 1},
        )

        # Wake tick scheduler if idle
        scheduler = self._tick_schedulers.get(target_id)
        if scheduler is not None:
            scheduler.wake()

        # Deliver event
        actions = await agent.on_event(event)

        # Execute resulting actions, propagating cascade depth so that
        # SEND_MESSAGE actions inherit the current depth for child dispatches.
        await self._executor.execute(
            target_id, actions, cascade_depth=depth + 1,
        )

        return actions


# ─── Tick Scheduler ────────────────────────────────────────


class TickScheduler:
    """Autonomous tick loop for persona agents.

    Fires ``on_tick()`` at configurable intervals. Tracks idle ticks
    (consecutive ``DO_NOTHING`` actions) and skips LLM calls when idle.
    Supports ``wake()`` to reset idle state on incoming events.
    """

    # Minimum tick interval to prevent accidental busy loops from
    # zero or negative configuration values.
    _MIN_INTERVAL: float = 0.01

    def __init__(
        self,
        agent: _LLMPersonaAgent,
        *,
        interval: float = 60.0,
        max_actions_per_tick: int = 3,
        idle_after_ticks: int = 10,
        executor: ActionExecutor | None = None,
    ) -> None:
        self._agent = agent
        if interval < self._MIN_INTERVAL:
            logger.warning(
                "Tick interval %.2fs below minimum, clamping to %.1fs",
                interval,
                self._MIN_INTERVAL,
            )
            interval = self._MIN_INTERVAL
        self._interval = interval
        self._max_actions_per_tick = max_actions_per_tick
        self._idle_after_ticks = idle_after_ticks
        self._executor = executor
        self._idle_count = 0
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._wake_event = asyncio.Event()

    @property
    def idle_count(self) -> int:
        """Number of consecutive idle ticks."""
        return self._idle_count

    @property
    def is_idle(self) -> bool:
        """Whether the scheduler has exceeded idle_after_ticks threshold."""
        return self._idle_count >= self._idle_after_ticks

    @property
    def is_running(self) -> bool:
        """Whether the tick loop task is active."""
        return self._task is not None and not self._task.done()

    def wake(self) -> None:
        """Reset idle state and wake the tick loop.

        Called by EventDispatcher when an event arrives for this agent.
        """
        self._idle_count = 0
        self._wake_event.set()

    def start(self) -> None:
        """Start the tick loop as an asyncio task."""
        if self._task is not None and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name=f"tick-{self._agent.agent_id}")

    async def stop(self, timeout: float = 10.0) -> None:
        """Stop the tick loop, waiting for in-flight operations.

        Args:
            timeout: Maximum seconds to wait for the current tick to complete.
        """
        self._stopped.set()
        self._wake_event.set()  # Unblock any wait
        if self._task is not None and not self._task.done():
            try:
                # shield() prevents wait_for's cancellation from killing
                # the task — _stopped + _wake_event already signal _run()
                # to exit cleanly.  If it doesn't exit within `timeout`,
                # we cancel the task explicitly in the TimeoutError branch.
                await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
            except TimeoutError:
                logger.warning(
                    "Tick scheduler for %s did not stop within %.0fs, cancelling",
                    self._agent.agent_id,
                    timeout,
                )
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run(self) -> None:
        """Main tick loop."""
        logger.info(
            "Tick scheduler started for %s (interval=%.0fs, idle_after=%d)",
            self._agent.agent_id,
            self._interval,
            self._idle_after_ticks,
        )
        while not self._stopped.is_set():
            # Wait for interval or wake signal
            self._wake_event.clear()
            try:
                await asyncio.wait_for(
                    self._wait_for_stop_or_wake(),
                    timeout=self._interval,
                )
                # _stopped or _wake_event was set
                if self._stopped.is_set():
                    break
                # Woken up — fall through to tick immediately
            except TimeoutError:
                # Normal interval elapsed
                pass

            if self._stopped.is_set():
                break

            # Skip LLM calls when idle, but still recover energy so
            # woken agents aren't energy-depleted after long idle periods.
            # Brief lock acquire ensures consistency with concurrent
            # on_event() which may drain_energy().
            # (Review finding: idle energy starvation.)
            if self.is_idle:
                logger.debug(
                    "Agent %s idle (%d ticks), skipping LLM tick",
                    self._agent.agent_id,
                    self._idle_count,
                )
                # Use public API instead of reaching into private
                # attributes (PR #55 review: TickScheduler should use
                # public API for agent lock and state).
                async with self._agent.exclusive():
                    self._agent.recover_idle_energy()
                continue

            try:
                actions = await self._agent.on_tick()
                # Limit actions per tick
                actions = actions[: self._max_actions_per_tick]

                # Track idle state
                all_do_nothing = all(
                    a.action_type == ActionType.DO_NOTHING for a in actions
                )
                if all_do_nothing:
                    self._idle_count += 1
                else:
                    self._idle_count = 0

                # Execute actions
                if self._executor is not None:
                    await self._executor.execute(self._agent.agent_id, actions)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Tick error for agent %s", self._agent.agent_id,
                )

        logger.info("Tick scheduler stopped for %s", self._agent.agent_id)

    async def _wait_for_stop_or_wake(self) -> None:
        """Wait until either ``_stopped`` or ``_wake_event`` is set.

        Creates two ``asyncio.Task`` objects — one for each event — and
        uses ``asyncio.wait(return_when=FIRST_COMPLETED)`` to unblock as
        soon as either fires.  The losing task is cancelled in the
        ``finally`` block to avoid leaked coroutines.

        This dual-event pattern avoids race conditions that would arise
        from sequential ``await`` calls (missing the other signal while
        waiting on the first).
        """
        stop_task = asyncio.create_task(self._stopped.wait())
        wake_task = asyncio.create_task(self._wake_event.wait())
        try:
            await asyncio.wait(
                {stop_task, wake_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (stop_task, wake_task):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
