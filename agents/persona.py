"""
Orchestr8 Persona Agent Interface (v0.2+).

Extends BaseAgent with async event handling, channel messaging,
sub-agent spawning, delegation, and autonomous behavior.
"""

from abc import abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .base import BaseAgent, TaskInput, TaskOutput


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
    timestamp: float = 0.0


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


@dataclass
class SubAgentResult:
    """Result from an ephemeral sub-agent."""

    status: str  # "completed" | "failed" | "timeout"
    result: Any = None
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


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
        self._orchestrator_client: Any = None  # injected by framework

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
                    status="completed",
                    result=action.payload.get("result", ""),
                    metadata=action.payload.get("metadata", {}),
                )

        return TaskOutput(status="failed", result="No completion action taken")

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
        return self.config.get("persona", {})

    @property
    def relationships(self) -> list[dict[str, Any]]:
        """Relationship definitions with other agents."""
        return self.config.get("relationships", [])

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
