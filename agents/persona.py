"""
Orchestr8 Persona Agent Interface (v0.2+).

Extends BaseAgent with async event handling, channel messaging,
sub-agent spawning, delegation, and autonomous behavior.

Includes the concrete ``_LLMPersonaAgent`` with LLM-powered ``on_event()``
decision loop, ``PersonaState`` dynamic state, behavioral dimension rendering,
and ``create_persona_agent()`` factory.
"""

from __future__ import annotations

import asyncio
import json
import logging
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
        """
        Core event handler. Override this in your persona agent.

        Receives events (messages, tasks, mentions, ticks), returns
        actions (send message, spawn sub-agent, delegate, complete task).
        The framework executes actions and delivers results as new events.
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
        """Deserialize from stored JSON. Unknown fields are ignored."""
        mood_str = data.get("mood", "neutral")
        try:
            mood = Mood(mood_str)
        except ValueError:
            logger.warning("Unknown mood %r, defaulting to NEUTRAL", mood_str)
            mood = Mood.NEUTRAL
        return cls(
            mood=mood,
            stress_level=float(data.get("stress_level", 0.0)),
            energy=float(data.get("energy", 1.0)),
            goal_progress=data.get("goal_progress", {}),
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
    a complete behavioral profile.
    """
    merged = {**_DIMENSION_DEFAULTS, **behavior}
    lines: list[str] = []
    for dimension, value in merged.items():
        desc = DIMENSION_DESCRIPTIONS.get(dimension, {}).get(value)
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
        """Execute tool calls, checking memory tools first then registry."""
        memory_tool_map = {td.name: td for td in self._memory_tools}
        results: list[LLMToolResult] = []

        for call in tool_calls:
            # Check memory tools first
            tool_def = memory_tool_map.get(call.name)
            if tool_def is None:
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

    def _parse_actions(self, response: LLMResponse) -> list[AgentAction]:
        """Parse LLM response text into AgentAction list.

        The LLM is expected to return a JSON array of actions. Falls back
        to a single COMPLETE_TASK with the raw text if parsing fails.
        """
        text = response.text or ""
        # Try to extract JSON action array from the response
        try:
            # Look for a JSON array in the response
            stripped = text.strip()
            if stripped.startswith("["):
                raw_actions = json.loads(stripped)
            elif "```json" in stripped:
                start = stripped.index("```json") + 7
                end = stripped.index("```", start)
                raw_actions = json.loads(stripped[start:end])
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
                actions.append(AgentAction(
                    action_type=action_type,
                    payload=raw.get("payload", {}),
                ))
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

    async def on_event(self, event: AgentEvent) -> list[AgentAction]:
        """LLM-powered event handler with multi-turn tool-use loop."""
        async with self._lock:
            return await self._on_event_inner(event)

    async def _on_event_inner(self, event: AgentEvent) -> list[AgentAction]:
        """Inner event handler — must be called under self._lock."""
        if self._llm_client is None:
            return [AgentAction(
                action_type=ActionType.COMPLETE_TASK,
                payload={"result": "LLM client not configured"},
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
            logger.warning("Failed to store episode", exc_info=True)

        # 7. Persist state
        await self._persist_persona_state()

        return actions

    async def on_tick(self) -> list[AgentAction]:
        """Autonomous tick — recovers energy, then decides on actions."""
        async with self._lock:
            self._state.recover_energy()
            event = AgentEvent(event_type=EventType.TICK)
            return await self._on_event_inner(event)

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
            logger.warning("Failed to persist persona state", exc_info=True)

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
            logger.warning("Failed to load persona state, using defaults", exc_info=True)
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
        """Close all memory tiers, awaiting in-flight operations."""
        async with self._lock:
            await self._persist_persona_state()
            await self._working_memory.close()
            await self._episodic_memory.close()
            await self._relationship_memory.close()


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
    working_memory = WorkingMemory(
        max_tokens=config.get("max_tokens", 100_000),
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
