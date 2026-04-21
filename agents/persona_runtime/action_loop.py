"""Multi-turn LLM action loop for _LLMPersonaAgent.

Contains the system prompt assembly, event formatting, tool execution,
action parsing/validation, and the core ``_on_event_inner()`` multi-turn
loop that drives LLM calls until ``stop_reason == "end_turn"`` or the
``max_llm_calls`` budget is exhausted.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from ..llm_client import LLMClient, LLMResponse, LLMToolResult, StopReason, ToolCall
from ..memory.episodic import EpisodicMemory
from ..memory.working import WorkingMemory
from ..persona_types import (
    ActionType,
    AgentAction,
    AgentEvent,
    EventType,
    PersonaState,
)
from ..tools.registry import ToolDefinition, get_tool, list_tools

if TYPE_CHECKING:
    from .memory_context import MemoryInjectionResult

logger = logging.getLogger(__name__)

__all__ = ["_ActionLoopMixin"]


# ─── Constants ─────────────────────────────────────────────

# Hard upper bounds for LLM-provided SPAWN_SUB_AGENT resource fields.
# Applied in _validate_action_payload() before the payload reaches
# ActionExecutor.  The action is not yet wired (returns 'not_implemented'),
# but caps are enforced at validation time so the boundary is in place
# when execution is wired in a future RFC.
# (PR review: SPAWN_SUB_AGENT resource fields not bounded at validation time.)
_MAX_SUB_AGENT_TOKENS: int = 100_000
_MAX_SUB_AGENT_TIMEOUT_SECONDS: int = 3_600   # 1 hour
_MAX_SUB_AGENT_LLM_CALLS: int = 50

# Persona-runtime fallback limits for LLM calls and output tokens.
# These intentionally differ from the task-agent defaults in defaults.py
# (DEFAULT_MAX_LLM_CALLS=5, DEFAULT_MAX_TOKENS=8192): persona agents run
# multi-turn tool-use loops that typically need more iterations, and their
# output tokens were historically capped lower.  Keeping persona defaults
# separate preserves the original persona_runtime.py behavior.
# (PR #95 review: shared defaults silently changed persona fallback values.)
_PERSONA_DEFAULT_MAX_LLM_CALLS: int = 10
_PERSONA_DEFAULT_MAX_TOKENS: int = 4096

# ─── Mixin ─────────────────────────────────────────────────


class _ActionLoopMixin:
    """Mixin providing the multi-turn LLM action loop for _LLMPersonaAgent."""

    # Attribute declarations for type checkers — set by __init__ or base classes.
    agent_id: str
    config: dict[str, Any]
    name: str
    role: str
    persona: dict[str, Any]
    _llm_client: LLMClient | None
    _working_memory: WorkingMemory
    _state: PersonaState
    _episodic_memory: EpisodicMemory
    _memory_tools: list[ToolDefinition]

    # Stub declarations for methods provided by sibling mixins / concrete class.
    if TYPE_CHECKING:
        def _format_event(self, event: AgentEvent) -> str: ...
        async def _inject_memory_context(
            self, event: AgentEvent, *, query: str | None = None,
        ) -> MemoryInjectionResult: ...
        def _build_system_prompt(self) -> str: ...
        async def _persist_persona_state(self) -> None: ...
        def _has_active_goal_payload(self) -> bool: ...
        def _has_pending_turn(self) -> bool: ...

    def _build_tool_definitions(self) -> list[dict[str, Any]]:
        """Build tool definitions including memory tools.

        Uses a dict keyed by tool name so memory tools take precedence
        over registry tools with the same name (F-5a-2: defense-in-depth,
        memory tools should shadow any same-named registry tools).
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
    _AGENT_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

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
                # Cap numeric resource fields to guard against LLM-generated
                # payloads with unbounded values (e.g. max_tokens: 500000).
                # Uses module-level hard caps; config-driven limits are a v0.3
                # concern once SPAWN_SUB_AGENT execution is wired.
                # (PR review: SPAWN_SUB_AGENT resource fields not bounded.)
                for field_name, cap in (
                    ("max_tokens", _MAX_SUB_AGENT_TOKENS),
                    ("timeout_seconds", _MAX_SUB_AGENT_TIMEOUT_SECONDS),
                    ("max_llm_calls", _MAX_SUB_AGENT_LLM_CALLS),
                ):
                    if field_name in p:
                        try:
                            val = int(p[field_name])
                        except (TypeError, ValueError):
                            logger.warning(
                                "SPAWN_SUB_AGENT %s is not numeric (%r), removing",
                                field_name, p[field_name],
                            )
                            del p[field_name]
                            continue
                        if val > cap:
                            logger.warning(
                                "SPAWN_SUB_AGENT %s %d exceeds cap %d, clamping",
                                field_name, val, cap,
                            )
                            p[field_name] = cap
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

    # ─── Core inner event handler ──────────────────────

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

        # 0. Format event once and inject memory context.
        # _format_event() is pure; computing it here avoids a redundant call
        # inside _inject_memory_context().  (F-60-2: deduplicate _format_event.)
        user_message = self._format_event(event)

        # Use raw event content for memory queries, not the formatted version.
        # _format_event() wraps user messages in <|user_message|> XML-style
        # delimiters for prompt injection mitigation — these delimiters break
        # FTS5 full-text search queries and produce no useful keyword matches.
        # (Bug: FTS5 receives '<|user_message user_id="..."|>...<|/user_message|>'
        # instead of the actual message text.)
        if event.event_type == EventType.MESSAGE_RECEIVED:
            memory_query = event.payload.get("content", "")
        else:
            memory_query = user_message
        # RFC 0017 §F: capture the injection result for the empty-context
        # TICK short-circuit below.  The MemoryInjectionResult exposes
        # memory_admitted_tokens so we can decide whether any memory was
        # actually relevant before paying the LLM call cost.
        injection_result = await self._inject_memory_context(event, query=memory_query)

        # RFC 0017 §F: empty-context TICK short-circuit.
        # Suppress the LLM call when all four conditions hold:
        #   1. This is an autonomous TICK event (not a user/task event).
        #   2. No memory was admitted (zero relevant episodes, notes, or
        #      relationships).  This includes the nominal case (recall-layer
        #      min_score threshold filtered low-signal content at the DB
        #      layer per PR 4) and the failure case (all three memory tier
        #      lookups raised exceptions and were swallowed by
        #      ``_inject_memory_context``'s except-and-continue handlers,
        #      which log warnings per tier).  In both cases paying for an
        #      LLM call provides no incremental value; the per-tier warning
        #      logs preserve operator visibility into DB outages.
        #   3. No active goal payload — the agent has nothing to work toward.
        #   4. No pending conversation turn — no user is waiting for a reply.
        # On match: return DO_NOTHING so TickScheduler increments idle_count
        # via its existing all_do_nothing branch.  Log at DEBUG with a stable
        # reason field so RFC 0019 (OTEL) can promote it to a counter later.
        if (
            event.event_type == EventType.TICK
            and injection_result.memory_admitted_tokens == 0
            and not self._has_active_goal_payload()
            and not self._has_pending_turn()
        ):
            logger.debug(
                "Agent %s: empty-context tick suppressed",
                self.agent_id,
                extra={"reason": "empty_context_tick", "agent_id": self.agent_id},
            )
            return [AgentAction(action_type=ActionType.DO_NOTHING, payload={})]

        # 1. Build system prompt and append working memory context.
        system_prompt = self._build_system_prompt()

        # Retrieve assembled working memory (episodic, relationship, notes)
        # and append to the system prompt so the LLM sees relevant memories.
        # build_context() returns sections sorted by priority (highest first),
        # dropping those that exceed the token budget.
        # (F-60-R2-1: build_context() was never called — injected memory
        #  sections were silently discarded with no effect on LLM behavior.)
        memory_sections = self._working_memory.build_context()
        if memory_sections:
            # Each element is a dict with "role" (the section name, e.g.
            # "episodic_recall") and "content" (the text to inject).
            # "role" is a WorkingMemory section identifier — NOT an LLM
            # conversation role (user/assistant/system).  We use only
            # "content" here; "role" labels are intentionally omitted from
            # the prompt to avoid confusing the LLM with metadata noise.
            # (PR review should-fix #6: document "role" vs LLM message role.)
            memory_text = "\n\n".join(s["content"] for s in memory_sections)
            system_prompt += "\n\n" + memory_text

        # 2. Multi-turn tool-use loop (user_message already computed above)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_message},
        ]
        tool_defs = self._llm_client.format_tool_definitions(
            self._build_tool_definitions()
        )

        max_llm_calls = self.config.get("max_llm_calls", _PERSONA_DEFAULT_MAX_LLM_CALLS)
        max_tokens = self.config.get("max_tokens", _PERSONA_DEFAULT_MAX_TOKENS)

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
