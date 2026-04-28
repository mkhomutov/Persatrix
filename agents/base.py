"""
Persatrix Base Agent Interface.

All agents implement BaseAgent. Task agents override handle().
Persona agents extend PersonaAgent (see persona.py) which adds
event-driven communication, sub-agent spawning, and autonomy.
"""

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .defaults import DEFAULT_MAX_LLM_CALLS, DEFAULT_MAX_TOKENS
from .llm_client import (
    LLMClient,
    LLMResponse,
    LLMToolResult,
    StopReason,
    ToolCall,
)
from .memory import MemoryFacade, budget_to_limit
from .tools.registry import get_tool, list_tools

logger = logging.getLogger(__name__)

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


class BaseAgent(ABC):
    """
    Base class for all Persatrix agents.

    Task agents: override handle() for synchronous task execution.
    Persona agents: extend PersonaAgent instead (see persona.py).
    """

    def __init__(
        self,
        agent_id: str,
        config: dict[str, Any] | None = None,
        llm_client: LLMClient | None = None,
    ):
        self.agent_id = agent_id
        self.config = config or {}
        self._llm_client = llm_client
        self._memory: Any = None

    @property
    def memory(self) -> Any:
        """Access the agent's memory surface, or ``None`` when disabled.

        For task agents this is a :class:`MemoryFacade` opened by
        :meth:`initialize_memory` when ``memory.enabled`` is set in
        ``config/agents.yaml`` (deny-by-default; RFC 0008 PR plan PR 2).
        Persona-runtime subclasses override this property to expose a
        ``MemoryNamespace`` over the three persona-memory tiers.

        Read-only — internal code mutates ``self._memory``; tests that
        need to inject a mock should do the same.
        """
        return self._memory

    def _memory_enabled(self) -> bool:
        memory_cfg = self.config.get("memory") or {}
        return bool(memory_cfg.get("enabled", False))

    @abstractmethod
    async def handle(self, task: TaskInput) -> TaskOutput:
        """
        Process a task and return a result.

        This is the primary interface for v0.1 task agents and
        backward-compatible entry point for persona agents.
        """
        ...

    # -- Participant Protocol properties (RFC 0016) -----------------------

    @property
    def participant_id(self) -> str:
        """Participant identity — delegates to ``agent_id``."""
        return self.agent_id

    @property
    def participant_type(self) -> str:
        """Participant kind — always ``"agent"`` for BaseAgent subclasses."""
        return "agent"

    @property
    def display_name(self) -> str:
        """Human-readable display name — delegates to ``name``."""
        return self.name

    # -- Agent metadata properties ----------------------------------------

    @property
    def capabilities(self) -> list[str]:
        """Declare what this agent can do (config-driven, deep-review d5)."""
        result: list[str] = self.config.get("capabilities", [])
        return result

    @property
    def name(self) -> str:
        """Human-readable agent name."""
        result: str = self.config.get("name", self.agent_id)
        return result

    @property
    def role(self) -> str:
        """Agent's role description."""
        result: str = self.config.get("role", "")
        return result

    async def health_check(self) -> bool:
        """Returns True if the agent is healthy and ready to accept tasks."""
        return True

    async def shutdown(self) -> None:
        """Called during graceful shutdown. Override to clean up resources."""
        pass

    # ─── Memory lifecycle (RFC 0008 PR plan PR 2) ───────────────

    async def initialize_memory(self) -> None:
        """Create and open the agent's :class:`MemoryFacade` if enabled.

        No-op when ``memory.enabled`` is false (the deny-by-default config
        path).  Idempotent — a second call after a successful first call
        is a no-op.  The agent server calls this from its startup pass for
        every registered task agent; persona agents have their own memory
        lifecycle in :mod:`agents.persona_runtime` and are unaffected.
        """
        if self.memory is not None:
            return
        if not self._memory_enabled():
            return
        memory_cfg = self.config.get("memory") or {}
        db_path = memory_cfg.get("db_path", "data/memory.db")
        min_score = memory_cfg.get("min_score")
        facade = MemoryFacade(
            agent_id=self.agent_id,
            db_path=db_path,
            default_min_score=min_score,
        )
        await facade.initialize()
        self._memory = facade
        logger.info(
            "Initialised MemoryFacade for task agent %s (db=%s)",
            self.agent_id, db_path,
        )

    async def close_memory(self) -> None:
        """Close the agent's :class:`MemoryFacade` if it was opened.

        Persona-runtime subclasses override this to close their own
        per-tier memory state; this base implementation only closes
        a :class:`MemoryFacade` when one was opened by
        :meth:`initialize_memory`.
        """
        if not isinstance(self.memory, MemoryFacade):
            return
        try:
            await self.memory.close()
        finally:
            self._memory = None

    # ─── Shared LLM Loop ────────────────────────────────────

    def _build_tool_definitions(self) -> list[dict[str, Any]]:
        """Build normalized tool definitions from the tool registry.

        S-12: Filters to only tools listed in agent's ``tools`` config.
        An empty list means no tools are exposed (e.g. planner agent).
        """
        # F-04: early return avoids iterating the full registry when no
        # tools are configured for this agent.
        allowed = self.config.get("tools", [])
        if not allowed:
            return []
        # N-04: convert to set for O(1) membership checks (matters when
        # the tool registry grows beyond the v0.1 built-in set of 4).
        allowed_set = set(allowed)
        return [
            {
                "name": td.name,
                "description": td.description,
                "parameters": td.parameters,
            }
            for td in list_tools()
            if td.name in allowed_set
        ]

    async def _execute_tools(self, tool_calls: list[ToolCall]) -> list[LLMToolResult]:
        """Execute tool calls sequentially, returning LLM-facing results.

        # TODO(v0.2): parallel tool execution with conflict detection
        """
        results: list[LLMToolResult] = []
        for call in tool_calls:
            tool_def = get_tool(call.name)
            if tool_def is None or tool_def.func is None:
                results.append(
                    LLMToolResult(
                        tool_call_id=call.id,
                        content=f"Unknown tool: {call.name}",
                        is_error=True,
                    )
                )
                continue
            try:
                result = await tool_def.func(**call.input)
                if result.success:
                    # PR-review SF4: Use JSON for dict/list data so the LLM
                    # sees {"key": true} instead of Python repr {'key': True}.
                    content = (
                        json.dumps(result.data)
                        if isinstance(result.data, (dict, list))
                        else str(result.data)
                    )
                else:
                    error_msg = result.error or "Tool failed"
                    # Include error_type when the @tool wrapper captured an
                    # exception (e.g. PermissionError, ValueError) so the LLM
                    # receives structured context about the failure category.
                    if result.error_type:
                        content = f"Tool error ({result.error_type}): {error_msg}"
                    else:
                        content = error_msg
                results.append(
                    LLMToolResult(
                        tool_call_id=call.id,
                        content=content,
                        is_error=not result.success,
                    )
                )
            except PermissionError as exc:
                # Defense-in-depth: normally unreachable because the @tool
                # wrapper catches all exceptions.  Retained for MCP/custom
                # tools that may bypass the decorator.
                # SF-06: log full details but return generic message to the LLM
                # (consistent with S-11 pattern for _run_llm_loop exceptions).
                logger.warning("Permission denied in tool %s: %s", call.name, exc)
                results.append(
                    LLMToolResult(
                        tool_call_id=call.id,
                        content="Permission denied",
                        is_error=True,
                    )
                )
            except Exception as exc:
                # Defense-in-depth: same rationale as PermissionError above.
                logger.warning("Unexpected error in tool %s: %s", call.name, exc)
                results.append(
                    LLMToolResult(
                        tool_call_id=call.id,
                        content="Internal tool error",
                        is_error=True,
                    )
                )
        return results

    async def _inject_memories(
        self,
        system_prompt: str,
        task: TaskInput,
    ) -> str:
        """Augment *system_prompt* with relevant memories when memory is enabled.

        Reads the orchestrator's RFC 0008 ``_context_package`` payload
        (when present) for the advisory ``budget_memory_tokens`` field
        and translates it into a recall ``limit`` via
        :func:`agents.memory.budget_to_limit`.  The query passed to
        ``retrieve_relevant`` is the task payload — Phase 2 keeps the
        query simple; PR 5 adds richer query-construction.

        Failures are caught and logged: a memory-tier outage must not
        block task execution, so the system prompt is returned unchanged.
        """
        if self.memory is None:
            return system_prompt
        budget_tokens = 0
        package_raw = task.context.get(CONTEXT_PACKAGE_KEY) if task.context else None
        if isinstance(package_raw, str) and package_raw:
            try:
                decoded = json.loads(package_raw)
                budget_tokens = int(decoded.get("budget_memory_tokens", 0))
            except (json.JSONDecodeError, TypeError, ValueError):
                logger.debug(
                    "agent %s: malformed _context_package, ignoring",
                    self.agent_id, exc_info=True,
                )
        limit = budget_to_limit(budget_tokens)
        try:
            entries = await self.memory.retrieve_relevant(
                task.payload, limit=limit,
            )
        except Exception:
            logger.warning(
                "agent %s: memory retrieve_relevant failed — proceeding without memory",
                self.agent_id, exc_info=True,
            )
            return system_prompt
        if not entries:
            return system_prompt
        memory_lines = [
            f"- {entry.content}" for entry in entries
        ]
        preamble = "Relevant memories from previous tasks:\n" + "\n".join(memory_lines)
        return f"{system_prompt}\n\n{preamble}"

    async def _run_llm_loop(
        self,
        task: TaskInput,
        system_prompt: str,
    ) -> TaskOutput:
        """Shared handle loop: LLM call → tool dispatch → repeat.

        Subclasses call this from handle() with their system prompt.
        """
        if self._llm_client is None:
            return TaskOutput(
                status=TaskStatus.FAILED,
                result="LLM client not configured",
            )

        # PR-review SF2: Fail fast with a clear message instead of raising
        # KeyError deep inside the provider call.
        if "model" not in self.config:
            return TaskOutput(
                status=TaskStatus.FAILED,
                result="Agent config missing required 'model' field",
            )

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": task.payload},
        ]
        # RFC 0008 PR plan PR 2 — opt-in memory injection.  Augments the
        # system prompt with a "Relevant memories" preamble when the agent
        # has memory.enabled=true.  The advisory budget comes from the
        # orchestrator's _context_package.budget_memory_tokens (PR 1 emits
        # 0; the facade's budget_to_limit() falls back to a small default).
        system_prompt = await self._inject_memories(system_prompt, task)
        tool_defs = self._llm_client.format_tool_definitions(
            self._build_tool_definitions()
        )
        total_tokens = 0
        tool_calls_count = 0

        # Reject negative limits immediately — they are not a valid sentinel
        # and indicate a misconfigured TaskConfig from the orchestrator.
        # Surface which field(s) were invalid via metadata to aid operator
        # diagnosis of misconfigured TaskConfigs (RFC 0006 PR 5c N-01).
        invalid_fields: list[str] = []
        if task.config.max_llm_calls < 0:
            invalid_fields.append("max_llm_calls")
        if task.config.max_tokens < 0:
            invalid_fields.append("max_tokens")
        if invalid_fields:
            return TaskOutput(
                status=TaskStatus.FAILED,
                result="Negative execution limits are not allowed",
                metadata={
                    "error_type": "permanent",
                    "invalid_fields": ",".join(invalid_fields),
                },
            )

        # 0 is the sentinel for "not set" (falsy), so `or` falls through
        # to the agent-level default, then to the system default.
        # See TaskInputConfig docstring and RFC 0006 §B.
        max_llm_calls = task.config.max_llm_calls or self.config.get(
            "max_llm_calls", DEFAULT_MAX_LLM_CALLS
        )
        max_tokens = task.config.max_tokens or self.config.get("max_tokens", DEFAULT_MAX_TOKENS)

        for _ in range(max_llm_calls):
            # review-fix S1: catch provider SDK exceptions (rate limits, auth
            # errors, network failures) so handle() always returns TaskOutput
            # instead of propagating unhandled exceptions.
            try:
                response: LLMResponse = await self._llm_client.create_message(
                    model=self.config["model"],
                    messages=messages,
                    system=system_prompt,
                    tools=tool_defs,
                    max_tokens=max_tokens,
                    temperature=self.config.get("temperature", 0.3),
                )
            except Exception as exc:
                logger.error(
                    "LLM provider error in agent %s: %s", self.agent_id, exc,
                )
                # S-11: Return generic message — SDK exceptions could contain
                # internal URLs or partial auth tokens.
                return TaskOutput(
                    status=TaskStatus.FAILED,
                    result="LLM provider error",
                    metadata={
                        "tokens_used": str(total_tokens),
                        "tool_calls": str(tool_calls_count),
                    },
                )
            total_tokens += response.usage.input_tokens + response.usage.output_tokens

            if response.stop_reason == StopReason.END_TURN:
                return TaskOutput(
                    status=TaskStatus.COMPLETED,
                    result=response.text or "",
                    metadata={
                        "tokens_used": str(total_tokens),
                        "tool_calls": str(tool_calls_count),
                    },
                )

            if response.stop_reason == StopReason.MAX_TOKENS:
                return TaskOutput(
                    status=TaskStatus.FAILED,
                    result="LLM response truncated: max_tokens limit reached",
                    metadata={
                        "tokens_used": str(total_tokens),
                        "tool_calls": str(tool_calls_count),
                    },
                )

            if response.stop_reason == StopReason.TOOL_USE:
                tool_results = await self._execute_tools(response.tool_calls)
                tool_calls_count += len(tool_results)
                messages = self._llm_client.append_tool_round(
                    messages, response, tool_results
                )
                continue

        return TaskOutput(
            status=TaskStatus.FAILED,
            result="Max LLM call iterations exceeded",
            metadata={
                "tokens_used": str(total_tokens),
                "tool_calls": str(tool_calls_count),
            },
        )
