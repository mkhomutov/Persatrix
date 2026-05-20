"""Task dataclasses shared across the agent runtime.

Leaf module — imports nothing from :mod:`agents.base` or any other
project module — so the dataclasses (and ``CONTEXT_PACKAGE_KEY``) can be
imported anywhere without inducing a circular import. They are
re-exported from :mod:`agents.base` to preserve the historical
``from agents.base import TaskInput`` / ``TaskOutput`` / ``TaskStatus`` /
``TaskInputConfig`` import paths used across the codebase. Same split
pattern as :mod:`agents.llm_types`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "CONTEXT_PACKAGE_KEY",
    "TaskInput",
    "TaskInputConfig",
    "TaskOutput",
    "TaskStatus",
]

CONTEXT_PACKAGE_KEY = "_context_package"
"""Reserved TaskInput.context key for the orchestrator's RFC 0008 _context_package
JSON payload (mirrors `internal/scheduler/context_package.go::ContextPackageKey`)."""


class TaskStatus(Enum):
    """Status of a completed task. Prevents stringly-typed bugs across agents."""

    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TaskInputConfig:
    """Per-task configuration overrides from TaskConfig proto message."""

    max_llm_calls: int = 0  # 0 means "use agent default"
    max_tokens: int = 0  # 0 means "use agent default"
    # PR-review B2: carry allowed_tools from proto even though enforcement
    # is deferred to v0.2, so the field is available to wire up later.
    allowed_tools: list[str] = field(default_factory=list)  # TODO(v0.2): enforce
    # RFC 0023 PR 5 — sub-agent invocation marker. When non-empty, the
    # child's leased LLM call is tagged ``CAUSE_SUB_AGENT`` and the
    # lease is acquired against this parent ``agent_id`` so per-persona
    # cost dashboards bill the originating persona for delegated work.
    # Set by ``SubAgentSpawner.dispatch``; never carried over the
    # ``TaskConfig`` proto (sub-agent dispatch is in-process today).
    sub_agent_parent_id: str = ""


@dataclass
class TaskInput:
    """Input to an agent for task execution."""

    task_id: str
    workflow_id: str
    payload: str
    context: dict[str, str] = field(default_factory=dict)
    config: TaskInputConfig = field(default_factory=TaskInputConfig)


@dataclass
class TaskOutput:
    """Result from an agent's task execution."""

    status: TaskStatus
    result: str
    metadata: dict[str, Any] = field(default_factory=dict)
