"""
Persatrix Persona Type Definitions.

Dataclasses, enums, and protocols that form the persona agent type system.
Extracted from ``persona.py`` for modularity — no logic changes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)
__all__ = [
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
]
# ─── Events that a persona agent can receive ───────────────


class EventType(Enum):
    TASK_ASSIGNED = "task_assigned"
    MESSAGE_RECEIVED = "message_received"
    # RFC 0011 PR 4a: additive — the canonical channels event type. The hard
    # rename ``MESSAGE_RECEIVED`` → ``CHANNEL_MESSAGE`` lands atomically with
    # the chat-path migration in a follow-up PR (chat is the heavy producer
    # of the old name; renaming without migrating chat would leave ``main``
    # broken). Until then both members coexist: chat ingest emits the old
    # name, ``ReceiveChannelMessage`` emits the new one.
    CHANNEL_MESSAGE = "channel_message"
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
    # RFC 0011 §D additive extension: top-level thread parent id (None for
    # non-threaded events). Promoted from ``payload`` so the response gate
    # in PR 4b can branch on thread context without a payload lookup. Stays
    # additive — existing callers default to None and need no change.
    thread_id: str | None = None
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
    # RFC 0011 PR 4a: additive — the canonical channels send action. The
    # hard rename ``SEND_MESSAGE`` → ``SEND_CHANNEL_MESSAGE`` lands atomically
    # with the chat-path migration in a follow-up PR. The dispatch executor
    # for this action arrives in PR 4b; the enum member ships now so PR 4a's
    # response gate plumbing has the canonical symbol available.
    SEND_CHANNEL_MESSAGE = "send_channel_message"
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
                # Clamp to [0.0, 1.0]: a corrupted DB value like 2.5 would
                # otherwise produce "goal: 250%" in to_prompt_section(), which
                # is misleading both to operators and the LLM.
                # energy and stress_level are clamped the same way below.
                # (PR review F-60-R6: goal_progress not clamped to [0.0, 1.0].)
                goal_progress[k] = min(1.0, max(0.0, float(v)))
            except (TypeError, ValueError):
                logger.warning("Invalid goal_progress value for %r: %r, skipping", k, v)
        return cls(
            mood=mood,
            stress_level=min(1.0, max(0.0, float(data.get("stress_level", 0.0)))),
            energy=min(1.0, max(0.0, float(data.get("energy", 1.0)))),
            goal_progress=goal_progress,
        )
