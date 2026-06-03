"""
Persatrix Base Agent Interface.

All agents implement BaseAgent. Task agents override handle().
Persona agents extend PersonaAgent (see persona.py) which adds
event-driven communication, sub-agent spawning, and autonomy.
"""

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

from .defaults import DEFAULT_MAX_LLM_CALLS, DEFAULT_MAX_TOKENS
from .generated import wallet_pb2 as walletpb
from .llm_client import (
    BudgetExceededError,
    LLMClient,
    LLMResponse,
    LLMToolResult,
    StopReason,
    ToolCall,
)
from .memory import MemoryStore, SharedPoolRegistry, budget_to_limit
from .memory.facade_procedural import procedural_kwargs_from_config
from .prompt_loader import load_snippet
from .security import maybe_wrap_tool_content

# Task dataclasses live in the leaf module agents.task_types; re-exported
# here to preserve the historical `from agents.base import TaskInput` paths.
from .task_types import (
    CONTEXT_PACKAGE_KEY,
    TaskInput,
    TaskInputConfig,
    TaskOutput,
    TaskStatus,
)
from .tools.registry import get_tool, list_tools

logger = logging.getLogger(__name__)

__all__ = [
    "CONTEXT_PACKAGE_KEY", "BaseAgent", "TaskInput",
    "TaskInputConfig", "TaskOutput", "TaskStatus",
]


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

        For task agents this is a :class:`MemoryStore` opened by
        :meth:`initialize_memory` when ``memory.enabled`` is set in
        ``config/agents.yaml`` (deny-by-default; RFC 0008 PR plan PR 2).
        Persona-runtime subclasses override it to expose a ``MemoryNamespace``
        over the three persona-memory tiers. Read-only — internal code mutates
        ``self._memory``, and tests that inject a mock should do the same.
        """
        return self._memory

    def _memory_enabled(self) -> bool:
        memory_cfg = self.config.get("memory") or {}
        return bool(memory_cfg.get("enabled", False))

    @abstractmethod
    async def handle(self, task: TaskInput) -> TaskOutput:
        """Process a task and return a result.

        The primary interface for v0.1 task agents and the backward-compatible
        entry point for persona agents.
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

    @property
    def agent_type(self) -> str:
        """Agent kind from config (``task``|``persona``|…), ``""`` when unset; sent
        to the orchestrator at registration so the console can disable chat for
        task agents (extends the RFC 0048 §A agent DTO). Display metadata only.
        """
        result: str = self.config.get("type", "")
        return result

    async def health_check(self) -> bool:
        """Returns True if the agent is healthy and ready to accept tasks."""
        return True

    async def shutdown(self) -> None:
        """Called during graceful shutdown. Override to clean up resources."""
        pass

    # ─── Memory lifecycle (RFC 0008 PR plan PR 2) ───────────────

    async def initialize_memory(
        self, *, shared_pools: SharedPoolRegistry | None = None,
    ) -> None:
        """Create and open the agent's :class:`MemoryStore` if enabled (no-op when
        ``memory.enabled`` is false; idempotent). ``shared_pools`` (RFC 0008 PR 4)
        wires named cross-agent pools.
        """
        if self.memory is not None:
            return
        if not self._memory_enabled():
            return
        memory_cfg = self.config.get("memory") or {}
        db_path = memory_cfg.get("db_path", "data/memory.db")
        store = MemoryStore(
            agent_id=self.agent_id,
            db_path=db_path,
            default_min_score=memory_cfg.get("min_score", 0.20),
            episodic_cap=int(memory_cfg.get("episodic_cap", 1000)),
            ttl_low_importance_days=int(memory_cfg.get("ttl_low_importance_days", 30)),
            eviction_cadence_seconds=int(memory_cfg.get("eviction_cadence_seconds", 3600)),
            shared_pools=shared_pools,
            **procedural_kwargs_from_config(memory_cfg),
        )
        await store.initialize()
        self._memory = store
        logger.info(
            "Initialised MemoryStore for task agent %s (db=%s)", self.agent_id, db_path
        )

    async def close_memory(self) -> None:
        """Close the agent's :class:`MemoryStore` if it was opened.

        Persona-runtime subclasses override this to close their per-tier state.
        """
        if not isinstance(self.memory, MemoryStore):
            return
        try:
            await self.memory.close()
        finally:
            self._memory = None

    # ─── Shared LLM Loop ────────────────────────────────────

    def _build_tool_definitions(self) -> list[dict[str, Any]]:
        """Build normalized tool definitions from the tool registry.

        S-12: filters to only tools in the agent's ``tools`` config; an empty
        list exposes no tools (e.g. planner agent).
        """
        # F-04: early return avoids iterating the full registry when no tools
        # are configured for this agent.
        allowed = self.config.get("tools", [])
        if not allowed:
            return []
        # N-04: set for O(1) membership checks as the tool registry grows.
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
        """Execute tool calls sequentially, returning LLM-facing results."""
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
                    # SF4: JSON for dict/list so the LLM sees {"k": true} not Python repr.
                    content = (
                        json.dumps(result.data)
                        if isinstance(result.data, (dict, list))
                        else str(result.data)
                    )
                    content = maybe_wrap_tool_content(call.name, content)
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
                # Defense-in-depth: normally unreachable (the @tool wrapper catches
                # all exceptions), retained for MCP/custom tools that bypass the
                # decorator. SF-06: log full details, return a generic LLM message.
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

        **Phase 2 trust boundary (PR-220 review M1, OWASP LLM01).**  Memory
        content is concatenated verbatim into the system prompt with no
        sanitisation.  The Phase-2 contract trusts every entry written
        under the same ``agent_id`` because (a) the SQLite store is
        scoped per ``agent_id`` so cross-agent bleed cannot occur, and
        (b) operators control which tools can call ``store_observation``
        via the deny-by-default permission whitelist.  A relevance floor
        is plumbed via the facade's ``default_min_score`` (read from
        ``config/agents.yaml`` ``memory.min_score``) so operators can
        raise the bar without code change.  Scope-isolation and per-call
        ``min_score`` enforcement land in RFC 0008 PR 5 once SQL-side
        filtering is wired; until then any tool that persists
        attacker-controlled text is in scope for the trust assumption.
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
        preamble = load_snippet("memory-preamble") + "\n" + "\n".join(memory_lines)
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

        # RFC 0023 PR 3 lease; PR 5 sub-agent override (parent attribution; OQ §7).
        parent = task.config.sub_agent_parent_id
        lease_cause = walletpb.CAUSE_SUB_AGENT if parent else walletpb.CAUSE_WORKFLOW_TASK
        lease_agent_id = parent or self.agent_id

        for _ in range(max_llm_calls):
            # review-fix S1: catch provider SDK exceptions (rate limits, auth
            # errors, network failures) so handle() always returns TaskOutput
            # instead of propagating unhandled exceptions.
            try:
                response: LLMResponse = await self._llm_client.create_message(
                    model=self.config["model"],
                    model_alias=self.config.get("model_alias"),
                    messages=messages,
                    system=system_prompt,
                    tools=tool_defs,
                    max_tokens=max_tokens,
                    temperature=self.config.get("temperature", 0.3),
                    cause=lease_cause,
                    workflow_id=task.workflow_id,
                    agent_id=lease_agent_id,
                )
            except BudgetExceededError as exc:
                # RFC 0023 § E — the wallet denied the lease (or could not
                # be reached). Surface it as a structured task failure so
                # the orchestrator can tell a budget rejection apart from a
                # generic LLM-provider outage.
                logger.warning(
                    "LLM call denied by the wallet in agent %s: %s",
                    self.agent_id, exc,
                )
                return TaskOutput(
                    status=TaskStatus.FAILED,
                    result=f"budget exceeded ({exc.scope or exc.reason}): {exc.message}",
                    metadata={
                        "error_type": "budget_exceeded",
                        "budget_scope": exc.scope,
                        "budget_reason": exc.reason,
                        "tokens_used": str(total_tokens),
                        "tool_calls": str(tool_calls_count),
                    },
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
