"""
Orchestr8 Base Agent Interface.

All agents implement BaseAgent. Task agents override handle().
Persona agents extend PersonaAgent (see persona.py) which adds
event-driven communication, sub-agent spawning, and autonomy.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskInput:
    """Input to an agent for task execution."""

    task_id: str
    workflow_id: str
    payload: str
    context: dict[str, str] = field(default_factory=dict)


@dataclass
class TaskOutput:
    """Result from an agent's task execution."""

    status: str  # "completed" | "failed"
    result: str
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseAgent(ABC):
    """
    Base class for all Orchestr8 agents.

    Task agents: override handle() for synchronous task execution.
    Persona agents: extend PersonaAgent instead (see persona.py).
    """

    def __init__(self, agent_id: str, config: dict[str, Any] | None = None):
        self.agent_id = agent_id
        self.config = config or {}

    @abstractmethod
    async def handle(self, task: TaskInput) -> TaskOutput:
        """
        Process a task and return a result.

        This is the primary interface for v0.1 task agents and
        backward-compatible entry point for persona agents.
        """
        ...

    @property
    @abstractmethod
    def capabilities(self) -> list[str]:
        """Declare what this agent can do (used for agent selection)."""
        ...

    @property
    def name(self) -> str:
        """Human-readable agent name."""
        return self.config.get("name", self.agent_id)

    @property
    def role(self) -> str:
        """Agent's role description."""
        return self.config.get("role", "")

    async def health_check(self) -> bool:
        """Returns True if the agent is healthy and ready to accept tasks."""
        return True

    async def shutdown(self) -> None:
        """Called during graceful shutdown. Override to clean up resources."""
        pass
