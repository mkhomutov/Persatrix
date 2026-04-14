"""
Orchestr8 Persona Agent Interface (v0.2+).

Extends BaseAgent with async event handling, sub-agent spawning,
and autonomous behavior.

Includes the ``PersonaAgent`` ABC, the concrete ``_LLMPersonaAgent``
with LLM-powered ``on_event()`` decision loop, and the
``create_persona_agent()`` factory.

Type definitions live in ``persona_types``, behavioral dimension
rendering in ``persona_behavior``, event dispatch in ``dispatch``,
and the tick scheduler in ``tick``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import abstractmethod
from typing import Any

from .base import BaseAgent, TaskInput, TaskOutput, TaskStatus

# Re-export everything that was previously importable from this module
# so that existing ``from agents.persona import X`` statements continue
# to work without modification.  New code should import from the
# specific submodule directly.
from .dispatch import ActionExecutor, EventDispatcher  # noqa: F401
from .llm_client import LLMClient, LLMResponse, LLMToolResult, StopReason, ToolCall
from .memory.episodic import EpisodicMemory
from .memory.relationship import RelationshipMemory
from .memory.working import ContextSection, WorkingMemory, estimate_tokens
from .persona_behavior import (
    DIMENSION_DESCRIPTIONS,  # noqa: F401
    render_behavior,
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
from .tools.registry import ToolDefinition, get_tool, list_tools

logger = logging.getLogger(__name__)

# Hard upper bounds for LLM-provided SPAWN_SUB_AGENT resource fields.
# Applied in _validate_action_payload() before the payload reaches
# ActionExecutor.  The action is not yet wired (returns 'not_implemented'),
# but caps are enforced at validation time so the boundary is in place
# when execution is wired in a future RFC.
# (PR review: SPAWN_SUB_AGENT resource fields not bounded at validation time.)
_MAX_SUB_AGENT_TOKENS: int = 100_000
_MAX_SUB_AGENT_TIMEOUT_SECONDS: int = 3_600   # 1 hour
_MAX_SUB_AGENT_LLM_CALLS: int = 50

# Per-tier truncation caps for memory context injected into working memory.
# build_context() enforces the overall token budget, but truncating per-item
# gives fairer distribution across entries within a tier.  Values balance
# detail vs. budget: notes are longest (agent-authored curated knowledge),
# relationship notes are medium, episode summaries shortest.
# (PR #60 review: inline magic numbers for truncation caps.)
_MAX_EPISODE_SUMMARY_CHARS: int = 200
_MAX_RELATIONSHIP_NOTES_CHARS: int = 300
_MAX_NOTE_CONTENT_CHARS: int = 500

# Trust score defaults for relationship context filtering.
# A score of exactly _DEFAULT_TRUST_SCORE (the initial value) provides no
# useful signal to the LLM.  Only inject trust when it has deviated by more
# than _TRUST_DEVIATION_THRESHOLD from the default.
# (PR #60 review: unnamed magic numbers in trust comparison.)
_DEFAULT_TRUST_SCORE: float = 0.5
_TRUST_DEVIATION_THRESHOLD: float = 0.01


def _truncate_with_ellipsis(text: str, max_chars: int) -> str:
    """Truncate *text* to *max_chars* with word-boundary awareness.

    If *text* fits within *max_chars*, it is returned unchanged.
    Otherwise it is sliced to *max_chars* and an attempt is made to cut at
    the last space so the LLM sees a complete word.  If the slice contains
    no space, the full slice is used.  ``"..."`` is always appended to
    signal truncation (giving a 3-char overage in the worst case, which
    is acceptable).

    Extracted from _inject_memory_context() where the same pattern was
    copy-pasted for episode summaries, relationship notes, and note content.
    (PR #60 review: truncation pattern duplicated 3 times.)
    """
    if len(text) <= max_chars:
        return text
    sliced = text[:max_chars]
    truncated = sliced.rsplit(" ", 1)[0]
    # Zero-space guard: if the slice has no space, rsplit returns it
    # unchanged (len(truncated) == len(sliced)), so we use the full slice.
    if len(truncated) == len(sliced):
        truncated = sliced
    return truncated + "..."


def _coerce_event_timeout(
    raw_value: object,
    default: float,
    agent_id: str,
) -> float:
    """Coerce a config-sourced timeout to ``float``.

    YAML configs can supply a string for a numeric key (e.g.
    ``event_timeout: "300"``).  ``asyncio.wait_for(timeout=...)``
    requires a real float; a non-numeric value raises TypeError
    that silently escapes lock acquisition with no useful log.

    Returns *default* with a warning when coercion fails.

    Extracted from ``on_event()`` / ``on_tick()`` where the same
    ``try: float(raw)`` guard was duplicated.
    (PR #60 review: timeout coercion duplicated between on_event/on_tick.)
    """
    try:
        return float(raw_value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning(
            "Agent %s: invalid event_timeout %r, using default %.0fs",
            agent_id, raw_value, default,
        )
        return default


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
                # SECURITY: sender_id and content originate from the
                # dispatcher today (trusted).  When external bridges
                # (Slack, Discord, email) are added in v0.2+, these
                # fields will carry untrusted user input — sanitize
                # and length-cap before injecting into the LLM prompt
                # to mitigate prompt injection risks.
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
        timeout = _coerce_event_timeout(
            self.config.get("event_timeout", self._DEFAULT_EVENT_TIMEOUT),
            self._DEFAULT_EVENT_TIMEOUT,
            self.agent_id,
        )
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

    async def _inject_memory_context(
        self, event: AgentEvent, *, query: str | None = None,
    ) -> None:
        """Inject episodic, relationship, and note context into working memory.

        Queries the three memory tiers for content relevant to the current
        event and adds them as ``WorkingMemory`` sections with priorities
        that keep them below the system/persona prompts (100/90) but above
        conversation history.

        Priorities: relationship=8, episodic=7, notes=6.
        (F-5b-1: implement deferred memory-context injection.)

        Design: each memory tier is wrapped in ``except Exception`` to ensure
        one tier's failure (DB lock, I/O error, corrupted data) never blocks
        event processing.  ``exc_info=True`` logs the full traceback so
        failures are visible to operators.  We intentionally catch broad
        ``Exception`` rather than specific types (OSError, aiosqlite.Error)
        because the memory tier implementations may evolve to raise different
        exception types, and the contract here is "never fail the event".
        ``BaseException`` subclasses (SystemExit, KeyboardInterrupt) are NOT
        caught by ``except Exception``.
        (PR #60 review: document intent of broad exception handling.)
        """
        # query is pre-computed by _on_event_inner() to avoid calling
        # _format_event() twice per event.  (F-60-2: deduplicate call.)
        if query is None:
            query = self._format_event(event)

        # Always remove all three memory sections before (re-)injecting.
        # WorkingMemory.add_section() overwrites a section by name when the
        # tier finds results.  But when a tier finds NO results (e.g. no FTS5
        # matches, or a TICK event that skips episodic recall), add_section()
        # is never called — so a stale section from the previous event silently
        # persists and contaminates the next event's LLM system prompt.
        # Removing unconditionally here makes all three tiers symmetric:
        # section is absent after the call if and only if no results were found.
        # The relationship tier had its own remove_section() inside the sender
        # block; that call is now redundant and has been removed.
        # (PR #60 review F-60-R1: stale episodic_recall/recent_notes sections
        # not cleared between events.)
        self._working_memory.remove_section("episodic_recall")
        self._working_memory.remove_section("recent_notes")
        self._working_memory.remove_section("relationship_context")

        # Three memory tiers are queried sequentially rather than concurrently
        # via asyncio.gather() because all three share the same aiosqlite
        # connection (same db_path).  aiosqlite serialises operations on a
        # single connection, so concurrent gather() would not increase
        # throughput and would add complexity.  If the tiers ever move to
        # separate DB files, this can be revisited.
        # (PR #60 review: document why sequential rather than gather().)

        # 1. Episodic recall — recent episodes matching event content.
        # Skip for TICK events: the boilerplate "Autonomous tick: review
        # your goals..." query matches broadly in FTS5, returning
        # low-relevance episodes.  Notes (tier 3) are still injected
        # because the agent's personal knowledge IS relevant for
        # autonomous goal review.
        # (PR #60 review: TICK events waste I/O on low-signal FTS5 matches.)
        if event.event_type == EventType.TICK:
            episodes = []
        else:
            try:
                episodes = await self._episodic_memory.recall(query, limit=5)
            except Exception:
                logger.warning(
                    "Agent %s: episodic recall failed, skipping",
                    self.agent_id, exc_info=True,
                )
                episodes = []

        if episodes:
            lines = ["Relevant past episodes:"]
            for ep in episodes:
                # Cap individual summaries to prevent a single verbose episode
                # from consuming a disproportionate share of the working memory
                # token budget.  build_context() enforces the overall budget, but
                # truncating here gives fairer distribution across episodes.
                # Ellipsis signals truncation to the LLM.  (F-60-R2-3.)
                # (PR #60 review: unbounded episode summary length.)
                summary = _truncate_with_ellipsis(
                    ep.summary, _MAX_EPISODE_SUMMARY_CHARS,
                )
                lines.append(f"- {summary}")
            text = "\n".join(lines)
            self._working_memory.add_section(ContextSection(
                name="episodic_recall",
                content=text,
                priority=7,
                token_count=estimate_tokens(text),
                compressible=True,
            ))

        # 2. Relationship summary for the sender (if present).
        sender_id = event.sender_id
        if sender_id:
            try:
                rel = await self._relationship_memory.get_relationship_summary(
                    sender_id,
                )
            except Exception:
                logger.warning(
                    "Agent %s: relationship lookup for %s failed, skipping",
                    self.agent_id, sender_id, exc_info=True,
                )
                rel = None

            if rel and rel.interaction_count > 0:
                lines = [
                    f"Relationship with {rel.other_agent_id}:",
                ]
                # Only inject trust when it has deviated from the default.
                # A score of exactly _DEFAULT_TRUST_SCORE provides no useful
                # signal to the LLM and implies a measured assessment when
                # it's just the initial value.
                # (F-60-4: skip default trust injection.)
                if abs(rel.trust_score - _DEFAULT_TRUST_SCORE) > _TRUST_DEVIATION_THRESHOLD:
                    lines.append(f"  Trust: {rel.trust_score:.2f}")
                lines.append(f"  Interactions: {rel.interaction_count}")
                if rel.notes:
                    # TODO(v0.3): sanitize rel.notes when A2A protocol allows
                    # external agents — a compromised peer could store prompt
                    # injection text in its relationship notes.
                    # (PR #60 review: internal prompt injection via peer memory.)
                    # Cap relationship notes to prevent excessive working memory
                    # usage.  No storage cap exists on rel.notes currently.
                    # Ellipsis signals truncation to the LLM.  (F-60-R2-3.)
                    # (F-60-5: unbounded relationship notes in prompt.)
                    rel_notes = _truncate_with_ellipsis(
                        rel.notes, _MAX_RELATIONSHIP_NOTES_CHARS,
                    )
                    lines.append(f"  Notes: {rel_notes}")
                text = "\n".join(lines)
                self._working_memory.add_section(ContextSection(
                    name="relationship_context",
                    content=text,
                    priority=8,
                    token_count=estimate_tokens(text),
                    compressible=True,
                ))

        # 3. Recent notes (top 5 matching event content).
        # Note: for TICK events the query is the same boilerplate
        # "Autonomous tick: review your goals..." string used above.
        # This may return low-signal notes as it does for episodes.
        # Notes recall is preserved on TICK (unlike episodic recall which is
        # skipped) because notes are agent-authored curated knowledge that
        # can be directly relevant to autonomous goal review.  Accepted
        # limitation: low-relevance TICK notes may occasionally be injected.
        # TODO(future): use a different query strategy for TICK notes to
        # improve signal quality (e.g. goal-topic query).
        try:
            notes = await self._episodic_memory.recall_notes(query, limit=5)
        except Exception:
            logger.warning(
                "Agent %s: note recall failed, skipping",
                self.agent_id, exc_info=True,
            )
            notes = []

        if notes:
            lines = ["Relevant notes:"]
            for note in notes:
                # Cap note content to prevent disproportionate token usage.
                # Notes can be up to 10KB each (_MAX_NOTE_CONTENT_BYTES);
                # _MAX_NOTE_CONTENT_CHARS balances detail vs budget (longer
                # than episode summaries since notes are user-authored).
                # Ellipsis signals truncation to the LLM.  (F-60-R2-3.)
                # (F-60-1: note content not truncated.)
                content = _truncate_with_ellipsis(
                    note.content, _MAX_NOTE_CONTENT_CHARS,
                )
                lines.append(f"- [{note.topic}] {content}")
            text = "\n".join(lines)
            self._working_memory.add_section(ContextSection(
                name="recent_notes",
                content=text,
                priority=6,
                token_count=estimate_tokens(text),
                compressible=True,
            ))

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
        await self._inject_memory_context(event, query=user_message)

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
        timeout = _coerce_event_timeout(
            self.config.get("event_timeout", self._DEFAULT_EVENT_TIMEOUT),
            self._DEFAULT_EVENT_TIMEOUT,
            self.agent_id,
        )
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
