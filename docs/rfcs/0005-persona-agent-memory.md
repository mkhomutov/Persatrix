# RFC 0005 — Persona Agent & Memory System

**Type**: feature  
**Status**: 📋 Proposed  
**Author**: Engineering Team  
**Date**: 2026-04-11  
**Target**: v0.2  
**Depends on**: RFC 0001, RFC 0003, RFC 0004

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [PersonaAgent Runtime](#personaagent-runtime)
  - [Agent Type Discrimination](#agent-type-discrimination)
  - [Autonomous Tick Loop](#autonomous-tick-loop)
  - [Event Dispatch Framework](#event-dispatch-framework)
  - [Three-Tier Agent Memory System](#three-tier-agent-memory-system)
  - [Working Memory (Context Window Management)](#working-memory-context-window-management)
  - [Episodic Memory (Long-Term Storage)](#episodic-memory-long-term-storage)
    - [Future Enhancement: Semantic Search via Vector Embeddings](#future-enhancement-semantic-search-via-vector-embeddings)
  - [Relationship Memory (Trust & Interaction)](#relationship-memory-trust--interaction)
  - [Agent-Initiated Memory Tools](#agent-initiated-memory-tools)
  - [Dynamic Persona State](#dynamic-persona-state)
  - [Behavioral Dimensions & Rendering](#behavioral-dimensions--rendering)
  - [Data-Driven TaskAgent Consolidation](#data-driven-taskagent-consolidation)
  - [Config Schema Updates](#config-schema-updates)
  - [CLI Command Wiring](#cli-command-wiring)
  - [Go Orchestrator Impact](#go-orchestrator-impact)
  - [REST API for Persona Observability](#rest-api-for-persona-observability)
  - [SQLite Schema Migration Strategy](#sqlite-schema-migration-strategy)
  - [Memory Observability](#memory-observability)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Future Considerations](#future-considerations)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

This RFC implements the PersonaAgent runtime and three-tier memory system as the foundational v0.2 feature. It activates the existing `PersonaAgent` class scaffold (currently abstract-only) with: (1) an autonomous tick loop for goal-driven agents, (2) an event dispatch framework that routes `AgentEvent`s to persona agents, (3) three-tier memory (working, episodic, relationship), (4) dynamic persona state injection, and (5) agent-initiated memory tools (`store_note`, `recall_notes`, `update_note`, `delete_note`) for deliberate knowledge capture. As a bundled improvement, it consolidates the three structurally identical v0.1 task agents into a single data-driven `TaskAgent` class with YAML-configured instructions.

## Motivation

v0.1 delivers end-to-end workflow execution with task agents that respond to assigned work. v0.2 transforms Orchestr8 into an **agent society engine** where agents have persistent identities, autonomous goals, evolving relationships, and long-term memory.

The PersonaAgent + Memory system is the **critical path dependency** for all other v0.2 features:

- **Channels & Bridges** (future RFC) need persona agents that can receive and initiate messages
- **Sub-agent spawning** (future RFC) needs the `OrchestratorClient` protocol wired into persona agents
- **Communication protocols** (standup, debate, consensus) need agents with memory of past interactions
- **Organizational topologies** need agents with relationship awareness and trust-based decision making
- **Observer mode & session replay** need event streams from persona agent interactions

Without this RFC, no other v0.2 feature can be meaningfully implemented.

### Data-Driven TaskAgent Motivation

The three v0.1 task agents (`CoderAgent`, `ReviewerAgent`, `PlannerAgent`) are structurally identical — each is a system prompt constant + `_run_llm_loop()` call. Consolidating into a single `TaskAgent` with a YAML `instructions` field:
- Eliminates code duplication (3 files → 1 class)
- Makes new task agent types a config-only change
- Naturally introduces the `type: task | persona` discrimination needed for v0.2

## Goals

1. **Autonomous tick loop**: Persona agents with `autonomy.level >= autonomous` run a configurable `on_tick()` loop that checks goals, processes inbox, and decides actions.
2. **Event dispatch**: Route `AgentEvent`s (messages, mentions, task assignments, sub-agent results) to persona agents via `on_event()`, collect `AgentAction`s, and execute them.
3. **Working memory**: Context window manager that tracks token usage per section (system prompt, persona, memories, conversation), automatically summarizes old history when the window fills, and supports priority-weighted retention.
4. **Episodic memory**: SQLite-backed long-term storage of conversation summaries, decisions, and outcomes with relevance-based retrieval.
5. **Relationship memory**: Per-agent-pair trust scores, interaction history tracking, and configurable trust decay.
6. **Dynamic persona state**: Runtime mood (enum-constrained), stress, energy, goal progress, and relationship deltas injected into agent system prompts.
7. **Structured behavioral dimensions**: Replace free-text `traits` / `communication_style` / `decision_making` with five 3-point behavioral dimension enums (`directness`, `detail_focus`, `formality`, `risk_tolerance`, `expressiveness`), rendered into natural language for LLM prompts.
8. **Agent type discrimination**: Explicit `type: task | persona` field in agent config, replacing capability heuristics.
9. **Data-driven TaskAgent**: Single `TaskAgent` class driven by YAML `instructions` field, replacing `CoderAgent` / `ReviewerAgent` / `PlannerAgent`.
10. **CLI command wiring**: Wire Rust CLI stubs to existing and new REST endpoints as each phase lands — `run`, `status`, `agent list/info`, `validate`, `test --persona`.
11. **Agent-initiated memory tools**: Built-in `store_note`, `recall_notes`, `update_note`, `delete_note` tools that let agents deliberately capture and manage structured knowledge (distinct from framework-managed automatic memory).
12. **Configurable learning behavior**: Per-agent `memory.notes` config section controlling whether agents can self-direct note-taking, how many notes they retain, and auto-reflection triggers.

## Non-Goals

- **Channels & message routing** — separate RFC (depends on this one)
- **Sub-agent spawning implementation** — separate RFC; this RFC wires the `OrchestratorClient` protocol but does not implement `SubAgentSpawner`
- **External bridges** (Slack, Discord, email) — separate RFC
- **Communication protocols** (standup, debate, consensus) — separate RFC
- **Organizational topologies** — separate RFC
- **Distributed state / agent migration** — v0.3 scope
- **Persona template composition** (`extends: "personas.yaml#template"`) — template field deep-merge semantics, template-extending-template composition, and resolution during config loading require careful design. Deferred to a separate v0.2 RFC. The `templates/personas.yaml` and `templates/sub_agents.yaml` files exist but composition is not activated by this RFC.
- **Full autonomous level** with goal planning — v0.2 delivers `passive`, `reactive`, and `semi-autonomous`; full `autonomous` and `supervisor` levels are a v0.2 follow-up
- **Shared knowledge base** (org-wide document store) — deferred to later v0.2 RFC
- **Task refusal behavior** (`can_refuse_tasks` from extension spec E2.1) — deferred to follow-up when full autonomous level is implemented
- **Vector store / embeddings for memory retrieval** — MVP uses SQLite with text matching; vector search is post-MVP

## Design / Implementation

### PersonaAgent Runtime

The existing `PersonaAgent` class in [agents/persona.py](../../agents/persona.py) already defines the correct interface (`on_event()`, `on_tick()`, `handle()` bridge). This RFC activates it by:

1. **Wiring event delivery** in the gRPC server — when a persona agent receives a task, wrap it as `AgentEvent(TASK_ASSIGNED)` and dispatch via `on_event()`
2. **Adding action execution** — a new `ActionExecutor` processes `AgentAction` results (send message, complete task, delegate, spawn sub-agent)
3. **Injecting memory and state** into the persona agent's LLM context before each `on_event()` / `on_tick()` call

```python
class ActionExecutor:
    """Executes AgentAction results from persona agents."""

    async def execute(self, agent: PersonaAgent, actions: list[AgentAction]) -> None:
        for action in actions:
            match action.action_type:
                case ActionType.COMPLETE_TASK:
                    pass  # handled by PersonaAgent.handle() return
                case ActionType.SEND_MESSAGE:
                    await self._send_message(agent, action.payload)
                case ActionType.DELEGATE:
                    await self._delegate(agent, action.payload)
                case ActionType.SPAWN_SUB_AGENT:
                    await self._spawn_sub_agent(agent, action.payload)
                case ActionType.USE_TOOL:
                    await self._use_tool(agent, action.payload)
                case ActionType.REQUEST_APPROVAL:
                    pass  # TODO: v0.2 approval workflow
                case ActionType.GRANT_APPROVAL:
                    pass  # TODO: v0.2 approval workflow
                case ActionType.DENY_APPROVAL:
                    pass  # TODO: v0.2 approval workflow
                case ActionType.DO_NOTHING:
                    pass
```

#### Concurrency Control

`on_event()` and `on_tick()` can fire concurrently — the tick loop runs on a schedule while events arrive asynchronously. Both mutate shared state: `PersonaState`, `WorkingMemory`, and the LLM client. Without synchronization, concurrent calls could double-compress working memory, race on energy/stress updates, or produce interleaved SQLite writes.

**Requirement**: Each persona agent holds an `asyncio.Lock` that serializes `on_event()` and `on_tick()` execution. The `TickScheduler` acquires the lock before calling `on_tick()`, and the `EventDispatcher` acquires it before calling `on_event()`:

```python
class PersonaAgent(BaseAgent, ABC):
    def __init__(self, agent_id: str, config: dict[str, Any]) -> None:
        super().__init__(agent_id, config)
        self._lock = asyncio.Lock()
```

```python
# In EventDispatcher.dispatch():
async with agent._lock:
    await self._inject_memory_context(agent, event)  # must be inside lock
    actions = await agent.on_event(event)

# In TickScheduler._loop():
async with self._agent._lock:
    actions = await self._agent.on_tick()
```

This guarantees at most one concurrent `on_event()` or `on_tick()` per agent. Memory injection (`_inject_memory_context`, which mutates `WorkingMemory`) is inside the lock to prevent racing with a concurrent `on_tick()`. Events arriving while the agent is mid-tick queue on the lock and execute sequentially.

#### Memory Lifecycle in Server

The `MemoryLifecycle` protocol defines `initialize()` and `close()`. These are called at well-defined points in the server lifecycle:

- **`initialize()`**: Called during `server.py` startup, after the agent is constructed but before the gRPC server starts accepting requests. All three memory tiers must be initialized (tables created, config-seeded data loaded) before any `on_event()` or `on_tick()` can fire.
- **`close()`**: Called during graceful shutdown, **after** the gRPC server stops accepting new requests but **before** the process exits. In-flight memory operations (pending SQLite writes from the current `on_event()`/`on_tick()`) are allowed to complete via the concurrency lock — shutdown waits for the lock to be released before calling `close()`.

```python
# In server.py startup:
for agent in persona_agents.values():
    await agent.working_memory.initialize()
    await agent.episodic_memory.initialize()
    await agent.relationship_memory.initialize()

# In server.py shutdown (before stopping gRPC):
for agent in persona_agents.values():
    await agent.close_memory()
```

`PersonaAgent` exposes a public `close_memory()` method that encapsulates the lock acquisition, keeping `_lock` as a private implementation detail:

```python
class PersonaAgent(BaseAgent, ABC):
    async def close_memory(self) -> None:
        """Gracefully close all memory tiers, waiting for in-flight operations."""
        async with self._lock:
            await self.working_memory.close()
            await self.episodic_memory.close()
            await self.relationship_memory.close()
```

#### Multi-Agent-Per-Process Hosting

For v0.2 MVP, the server supports multiple persona agents in a single Python process. The `TickScheduler` creates one `asyncio.Task` per autonomous agent, and the `EventDispatcher` holds a dict of all persona agents. The per-agent `asyncio.Lock` (see [Concurrency Control](#concurrency-control)) ensures agents don't interfere with each other's state.

**Scaling consideration**: If the server hosts N persona agents each with `tick_interval_seconds: 60`, that's N concurrent `on_tick()` calls every minute plus incoming events. The concurrency lock serializes per-agent, not globally — agents run in parallel, only concurrent access to the *same* agent is serialized. For v0.2 MVP, this is sufficient; v0.3 may require multi-process hosting for large agent populations.

### Agent Type Discrimination

Add explicit `type` field to agent config:

```yaml
# config/agents.yaml
agents:
  - id: "coder-01"
    type: "task"                    # task agent — responds to assigned work
    instructions: "You are a code generation specialist..."
    model: "claude-sonnet-4-20250514"
    tools: [file_read, file_write, shell_exec]

  - id: "sarah-chen"
    type: "persona"                 # persona agent — event-driven with autonomy
    persona:
      title: "VP of Engineering"
      background: |
        15 years in software engineering. Former tech lead at a Series B startup.
        Values pragmatism over perfection.
      behavior:
        directness: direct          # indirect | balanced | direct
        detail_focus: big-picture    # big-picture | balanced | detail-focused
        formality: professional      # casual | professional | formal
        risk_tolerance: moderate     # cautious | moderate | bold
        expressiveness: reserved     # reserved | moderate | expressive
      quirks:
        - "Starts every Monday with 'Alright, what's on fire?'"
        - "Hates meetings longer than 30 minutes"
      goals:
        primary: "Ship v2.0 on time with acceptable quality"
        secondary: ["Reduce tech debt by 20%"]
        hidden: "Prove the team can self-organize"
      knowledge:
        domains: ["system design", "team management", "Go"]
        limitations: ["frontend/CSS", "ML internals"]
    autonomy:
      level: "semi-autonomous"
      tick_interval_seconds: 60
```

The agent loader in `server.py` uses `type` to instantiate `TaskAgent` or a `PersonaAgent` subclass:

```python
def _load_agent(agent_config: dict[str, Any]) -> BaseAgent:
    agent_type = agent_config.get("type", "task")
    match agent_type:
        case "task":
            return TaskAgent(agent_config["id"], agent_config)
        case "persona":
            # Factory that creates a concrete PersonaAgent subclass
            # implementing on_event() with the LLM-powered decision loop.
            return create_persona_agent(agent_config["id"], agent_config)
        case _:
            raise ValueError(f"Unknown agent type: {agent_type}")
```

### Autonomous Tick Loop

For agents with `autonomy.level` in (`semi-autonomous`, `autonomous`, `supervisor`), the server runs an async tick loop. Agents with `passive` or `reactive` level do not get a tick loop — they only respond to events via `on_event()`.

```python
class TickScheduler:
    """Drives on_tick() for autonomous persona agents."""

    def __init__(self, agent: PersonaAgent, executor: ActionExecutor):
        self._agent = agent
        self._executor = executor
        self._interval = agent.config.get("autonomy", {}).get(
            "tick_interval_seconds", 60
        )
        self._max_actions = agent.config.get("autonomy", {}).get(
            "max_actions_per_tick", 3
        )
        self._idle_limit = agent.config.get("autonomy", {}).get(
            "idle_after_ticks", 10
        )
        self._idle_count = 0
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Graceful shutdown: cancel the loop but allow the current action
        batch to finish. If the agent is mid-tick (ActionExecutor processing),
        we wait up to a bounded timeout for it to complete before forcing
        cancellation. This prevents silent loss of in-flight actions (e.g.,
        SEND_MESSAGE) and ensures pending memory writes are flushed."""
        if self._task:
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)

            # Skip LLM call entirely when idle — avoids wasting tokens
            # on agents that repeatedly produce DO_NOTHING actions.
            # Wake from idle when EventDispatcher calls wake().
            if self._idle_count >= self._idle_limit:
                continue

            try:
                async with self._agent._lock:
                    actions = await self._agent.on_tick()
            except Exception:
                # Log and continue — an unhandled exception must not kill
                # the tick loop permanently for this agent.
                logger.exception(
                    "Tick failed for agent %s", self._agent.agent_id
                )
                continue
            meaningful = [a for a in actions if a.action_type != ActionType.DO_NOTHING]
            if not meaningful:
                self._idle_count += 1
            else:
                self._idle_count = 0
                await self._executor.execute(
                    self._agent, meaningful[:self._max_actions]
                )

    async def wake(self) -> None:
        """Reset idle state. Called by EventDispatcher on incoming event.

        When a persona agent enters idle mode (idle_count >= idle_limit),
        the tick loop skips on_tick() calls to save LLM costs. When a new
        event arrives for that agent, the EventDispatcher calls wake() to
        reset the idle counter so the next tick cycle resumes normal
        behavior. This makes agents responsive to new activity while still
        saving costs during quiet periods."""
        self._idle_count = 0
```

### Event Dispatch Framework

Events flow through a central dispatcher that routes to the appropriate persona agent:

#### `AgentEvent` Structure

`AgentEvent` has two dict fields with distinct purposes:

- **`payload`** (`dict[str, Any]`): Event-specific content — task details, message text, mention context. Produced by the event source.
- **`metadata`** (`dict[str, Any]`): Framework-managed routing and tracking info — `cascade_depth`, `timestamp`, `trace_id`, `source_tick_id`. Produced and consumed by the framework (`EventDispatcher`, `TickScheduler`), never by the agent's LLM prompt.

This separation ensures framework control fields don't leak into the agent's LLM context and event content doesn't interfere with routing logic.

```python
class EventDispatcher:
    """Routes AgentEvents to persona agents and executes resulting actions."""

    def __init__(
        self,
        agents: dict[str, PersonaAgent],
        executor: ActionExecutor,
        tick_schedulers: dict[str, TickScheduler] | None = None,
    ):
        self._agents = agents
        self._executor = executor
        self._tick_schedulers = tick_schedulers or {}

    async def dispatch(self, agent_id: str, event: AgentEvent) -> list[AgentAction]:
        agent = self._agents.get(agent_id)
        if agent is None:
            raise ValueError(f"Unknown persona agent: {agent_id}")

        # Wake the agent's tick scheduler from idle on incoming events
        if scheduler := self._tick_schedulers.get(agent_id):
            await scheduler.wake()

        async with agent._lock:
            # Inject memory context inside the lock to prevent racing with
            # a concurrent on_tick() that may be mutating working memory.
            # _inject_memory_context() calls wm.add_section() and spawns a
            # fire-and-forget compress_if_needed() task — both mutate
            # WorkingMemory state that on_tick() also reads/writes.
            await self._inject_memory_context(agent, event)
            actions = await agent.on_event(event)
        await self._executor.execute(agent, actions)
        return actions

    async def _inject_memory_context(
        self, agent: PersonaAgent, event: AgentEvent
    ) -> None:
        """Load relevant memories and inject into agent context.

        Priority order (highest first):
        1. System prompt + persona description (non-compressible)
        2. Dynamic persona state (mood, energy, stress)
        3. Relationship context with event sender (if sender_id present)
        4. Relevant episodic memories (FTS5 query on event summary)
        5. Recent agent notes (inject_recent_notes most relevant)
        6. Current conversation history (compressible)
        """
        wm = agent.working_memory

        # Relationship context for the sender (if applicable)
        if event.sender_id:
            rel_summary = await agent.relationship_memory.get_relationship_summary(
                event.sender_id
            )
            wm.add_section(ContextSection(
                name="relationship",
                content=format_relationship(rel_summary),
                priority=70,
                token_count=estimate_tokens(format_relationship(rel_summary)),
            ))

        # Relevant episodic memories
        event_summary = summarize_event(event)  # extract searchable text from payload
        episodes = await agent.episodic_memory.recall(event_summary, limit=5)
        if episodes:
            episode_text = format_episodes(episodes)
            wm.add_section(ContextSection(
                name="episodic_memories",
                content=episode_text,
                priority=60,
                token_count=estimate_tokens(episode_text),
            ))

        # Trigger working memory compression if over budget (fire-and-forget;
        # current event proceeds with pre-compression context)
        asyncio.create_task(wm.compress_if_needed(agent.llm_client))
```

#### Concrete `on_event()` Decision Loop

`PersonaAgent` is abstract — subclasses must implement `on_event()`. The `create_persona_agent()` factory (Phase 5) returns a concrete subclass whose `on_event()` drives an LLM-powered decision loop:

1. **Build prompt**: Assemble system prompt from persona config + behavioral dimensions (via `render_behavior()`) + dynamic state (via `PersonaState.to_prompt_section()`) + injected memory context (via `_inject_memory_context()`)
2. **Format event**: Convert the `AgentEvent` into a user message describing what happened
3. **Run tool-use loop**: Send the assembled messages to the configured model, with memory tools available. If the LLM returns tool calls (e.g., `store_note`, `recall_notes`), execute them and feed results back in a multi-turn loop — same pattern as `BaseAgent._run_llm_loop()` for task agents. The loop continues until the LLM produces a final response without tool calls.
4. **Parse response**: Extract `AgentAction` list from the LLM's final (non-tool-call) response
5. **Return actions**: The framework's `ActionExecutor` handles execution; results may generate new events

**Tool-use loop vs single-shot call**: Unlike a simple `chat()` → parse flow, persona agents run a multi-turn tool-use loop to allow memory tools to execute *during* the LLM's reasoning, not after. This matches `BaseAgent._run_llm_loop()` semantics. The loop terminates when the LLM produces a response with no tool calls, which is then parsed into `AgentAction`s for the `ActionExecutor`.

```python
class _LLMPersonaAgent(PersonaAgent):
    """Concrete PersonaAgent with LLM-powered decision loop."""

    async def on_event(self, event: AgentEvent) -> list[AgentAction]:
        # 1. Build system prompt from persona + behavior + state + memories
        system_prompt = self._build_system_prompt()

        # 2. Format the event as a user message
        user_message = self._format_event(event)

        # 3. Multi-turn tool-use loop (same pattern as BaseAgent._run_llm_loop)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        while True:
            response = await self._llm_client.chat(
                messages=messages,
                tools=self._available_tools,
            )
            if not response.tool_calls:
                break
            # Execute tool calls (memory tools, agent tools) and append results
            tool_results = await self._execute_tools(response.tool_calls)
            messages.append(response.to_message())
            messages.extend(tool_results)

        # 4. Parse LLM response into agent actions
        actions = self._parse_actions(response)

        # 5. Store episode for this interaction (framework-managed)
        await self.episodic_memory.store_episode(
            summary=f"Event: {event.event_type.value} → Actions: {[a.action_type.value for a in actions]}",
            context={"event": event.payload, "sender": event.sender_id},
        )

        return actions
```

### Three-Tier Agent Memory System

> **Scope note:** The extension spec (E7.1) defines four memory tiers: Working, Episodic,
> Shared Knowledge Base, and Relationship. This RFC implements three *agent-scoped* tiers.
> The fourth tier (Shared Knowledge Base — org-wide document store) is deferred to a
> separate v0.2 RFC (see [Non-Goals](#non-goals)).

#### Architecture

```
PersonaAgent
  ├── WorkingMemory      (in-process, per-conversation)
  │   └── token-counted context sections
  ├── EpisodicMemory     (SQLite, per-agent)
  │   └── conversation summaries, decisions, outcomes
  └── RelationshipMemory (SQLite, per-agent-pair)
      └── trust scores, interaction patterns, history
# Shared Knowledge Base — deferred to separate v0.2 RFC (see Non-Goals)
```

The three tiers have **intentionally distinct interfaces** — they are not interchangeable backends behind a common protocol. `WorkingMemory` manages in-process token-counted sections (`add_section()` / `build_context()`), `EpisodicMemory` stores persistent summaries (`store_episode()` / `recall()`), and `RelationshipMemory` tracks trust (`get_trust()` / `update_trust()`). A `store(entry) → retrieve(query)` protocol was considered but rejected because it would force semantically different operations into a lowest-common-denominator interface, making each tier harder to use correctly. Each tier exposes the interface natural to its domain.

All three tiers share a common lifecycle contract:

```python
@runtime_checkable
class MemoryLifecycle(Protocol):
    """Lifecycle management shared by all memory tiers."""

    async def initialize(self) -> None:
        """Set up storage (create tables, allocate resources)."""
        ...

    async def close(self) -> None:
        """Clean up resources (close DB connections, flush pending writes)."""
        ...
```

### Working Memory (Context Window Management)

Manages what fits in the LLM context window using priority-weighted sections:

```python
@dataclass
class ContextSection:
    """A section of the LLM context window."""
    name: str                    # "system", "persona", "memories", "conversation"
    content: str
    priority: int                # higher = kept longer (system=100, persona=90, ...)
    token_count: int
    compressible: bool = True    # False for system prompt

class WorkingMemory:
    """Context window manager with priority-weighted retention."""

    def __init__(self, max_tokens: int = 100_000):
        self._max_tokens = max_tokens
        self._sections: list[ContextSection] = []

    def add_section(self, section: ContextSection) -> None: ...
    def total_tokens(self) -> int: ...

    async def compress_if_needed(self, llm_client: LLMClient) -> None:
        """Summarize lowest-priority compressible sections when over budget.

        This method is a coroutine, but callers should spawn it as a
        fire-and-forget background task (``asyncio.create_task()``) so
        the current event/tick can proceed with pre-compression context.
        The next ``build_context()`` call will pick up the compressed
        sections.  Direct ``await`` is acceptable during shutdown or
        testing where blocking is tolerable.
        """
        # Implementation note: Compression triggers an LLM call (1-10s
        # latency). The summarization call may use a cheaper/faster model
        # than the agent's primary model — route via optimization.yaml's
        # ``context_management.summarization.model`` field when available
        # (e.g. haiku-class for summarization vs sonnet-class for agent
        # reasoning).
        ...

    def build_context(self) -> list[dict[str, str]]:
        """Return ordered messages for LLM call."""
        ...
```

Token counting uses a lightweight estimator (chars/4 for MVP, tiktoken optional):

```python
def estimate_tokens(text: str) -> int:
    """Estimate token count. MVP: chars/4. TODO: tiktoken for accuracy."""
    return len(text) // 4
```

> **Accuracy note**: chars/4 is approximately 85% accurate for English prose. For heavily structured content (JSON, YAML, code with long variable names), it can **under-count by 30-40%** because tokenizers split on punctuation and capitalization boundaries. For CJK text, it drastically **over-counts**. The 20% safety margin (compress at ~80K for a 100K window) handles typical cases, but agents working with multilingual content or heavily structured data should use the `accurate=True` tiktoken path (see [Q2](#q2-token-estimation-accuracy)) or risk hitting context limits.

### Episodic Memory (Long-Term Storage)

SQLite-backed storage for conversation summaries and decisions:

```python
class EpisodicMemory:
    """Long-term memory store using SQLite."""

    def __init__(self, agent_id: str, db_path: str = "data/memory.db"):
        self._agent_id = agent_id
        self._db_path = db_path

    async def initialize(self) -> None:
        """Create tables if they don't exist.

        FTS5 availability check: FTS5 is enabled by default in CPython
        builds, but some minimal Python distributions (Alpine Linux,
        certain Docker images) may exclude it. initialize() tests FTS5
        availability with a throwaway 'CREATE VIRTUAL TABLE' and falls
        back to LIKE-based queries with a warning log if unavailable.
        """
        # episodes(id, agent_id, summary, context, outcome, timestamp, importance)

    async def store_episode(
        self,
        summary: str,
        context: dict[str, Any],
        outcome: str | None = None,
        importance: float = 0.5,
    ) -> str: ...

    async def recall(
        self,
        query: str,
        *,
        limit: int = 10,
        min_importance: float = 0.0,
    ) -> list[Episode]:
        """Retrieve relevant episodes. Increments access_count on returned entries."""
        ...

    async def summarize_old_episodes(
        self,
        older_than_days: int = 30,
        llm_client: LLMClient | None = None,
    ) -> int:
        """Compress old episodes via LLM summarization. Returns count processed.

        Like ``WorkingMemory.compress_if_needed()``, this triggers LLM
        calls for summarization.  These framework-initiated calls may be
        routed to a cheaper model than the agent's primary model via
        ``optimization.yaml``'s ``context_management.summarization.model``
        setting — episode summarization doesn't need persona-quality
        reasoning.
        """
        ...
```

Schema:

```sql
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    context_json TEXT,          -- JSON blob of structured context
    outcome TEXT,
    importance REAL DEFAULT 0.5,
    access_count INTEGER DEFAULT 0,  -- incremented on each recall() hit
    last_accessed_at REAL,           -- updated on each recall() hit
    tags_json TEXT,                   -- JSON array of string tags for structured recall
    created_at REAL NOT NULL,        -- Unix timestamp
    compressed_at REAL,              -- when last summarized
    compression_level INTEGER DEFAULT 0  -- 0=raw, 1=summarized, 2=distilled
);

CREATE INDEX idx_episodes_agent ON episodes(agent_id);
CREATE INDEX idx_episodes_importance ON episodes(importance DESC);
CREATE INDEX idx_episodes_created ON episodes(created_at DESC);
```

#### Future Enhancement: Semantic Search via Vector Embeddings

> **Status:** Deferred to post-MVP. Tracked here so the upgrade path is clear when the time comes.
> See also [Q5 decision](#q5-memory-retrieval-relevance-scoring) for the MVP scoring formula this replaces.

MVP retrieval uses SQLite FTS5 (BM25 keyword matching). This misses semantic connections — e.g., an agent searching "billing dispute resolution" won't find an episode titled "payment disagreement" because no keywords overlap. Vector embeddings solve this.

**Upgrade plan:**

1. **Embedding model**: Use the same LLM provider's embedding endpoint (e.g., `text-embedding-3-small` for OpenAI, `voyage-3-lite` for Anthropic) or a local model via Ollama. Add an `EmbeddingProvider` protocol alongside `LLMProvider` from RFC 0004.

2. **Storage**: Add a `embedding BLOB` column to `episodes` and `interactions` tables. SQLite doesn't have native vector indexing, so two options:
   - **sqlite-vec** extension (pure SQLite, no external deps) — good for <100k episodes per agent
   - **External vector store** (Qdrant, ChromaDB) — needed if episode counts grow large or if cross-agent semantic search is required

3. **Schema migration**:
   ```sql
   ALTER TABLE episodes ADD COLUMN embedding BLOB;  -- float32 vector as bytes
   ALTER TABLE episodes ADD COLUMN embedding_model TEXT;  -- track which model generated it
   ```

4. **Scoring formula update**: Replace BM25 with cosine similarity, keep the existing importance and recency factors:
   $$\text{score} = \text{cosine\_sim}(query\_emb, episode\_emb) \times \text{importance} \times (1 + \ln(1 + \text{access\_count})) \times \frac{1}{1 + \text{age\_days}}$$

5. **Hybrid retrieval**: Best results come from combining keyword (FTS5) and semantic (vector) scores. Retrieve top-K from each, merge with reciprocal rank fusion (RRF), then apply importance/recency weighting.

6. **Backfill**: On first upgrade, batch-embed all existing episodes. Add a migration command: `orch memory embed --agent <id> --backfill`.

7. **Cost considerations**: Embedding API calls add latency (~50ms) and cost per `store_episode()`. Cache embeddings and only re-embed on episode compression. Budget tracking in `config/optimization.yaml` should include embedding token costs.

**Prerequisites before implementing:**
- `EmbeddingProvider` protocol (extend RFC 0004's `LLMProvider` design)
- Running system with observable memory access patterns (to validate that FTS5 is insufficient)
- Cost/latency benchmarks comparing FTS5 vs vector vs hybrid on real agent interaction data

### Relationship Memory (Trust & Interaction)

Tracks per-agent-pair trust and interaction patterns:

```python
class RelationshipMemory:
    """Per-agent-pair trust and interaction tracking."""

    def __init__(self, agent_id: str, db_path: str = "data/memory.db"):
        self._agent_id = agent_id
        self._db_path = db_path

    async def get_trust(self, other_agent_id: str) -> float:
        """Get current trust score for another agent (0.0-1.0)."""
        ...

    async def update_trust(
        self,
        other_agent_id: str,
        delta: float,
        reason: str,
    ) -> float:
        """Update trust score. Returns new value (clamped to 0.0-1.0)."""
        ...

    async def record_interaction(
        self,
        other_agent_id: str,
        interaction_type: str,
        outcome: str,
        sentiment: float = 0.0,
    ) -> None: ...

    async def apply_decay(self, decay_rate: float = 0.01) -> None:
        """Decay all trust scores toward 0.5 (neutral).

        Intentionally bidirectional: trust above 0.5 decays downward
        (certainty erodes without reinforcement), and distrust below 0.5
        decays upward (modeling natural forgiveness without requiring
        positive interaction). This means an agent betrayed at trust 0.1
        will gradually return to neutral — a deliberate design choice
        that prevents permanent grudges in long-running simulations.

        If unidirectional decay (toward 0.0 only) is preferred, the
        decay target can be made configurable in a follow-up.
        """
        ...

    async def get_relationship_summary(
        self, other_agent_id: str
    ) -> dict[str, Any]:
        """Get full relationship context for injection into LLM prompt."""
        ...
```

Schema:

```sql
CREATE TABLE IF NOT EXISTS relationships (
    agent_id TEXT NOT NULL,
    other_agent_id TEXT NOT NULL,
    trust_score REAL DEFAULT 0.5,
    interaction_count INTEGER DEFAULT 0,
    last_interaction_at REAL,
    notes TEXT,
    PRIMARY KEY (agent_id, other_agent_id)
);

CREATE TABLE IF NOT EXISTS interactions (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    other_agent_id TEXT NOT NULL,
    interaction_type TEXT NOT NULL,
    outcome TEXT,
    sentiment REAL DEFAULT 0.0,
    created_at REAL NOT NULL
);
```

#### Trust Bootstrapping from Config

The `relationships` section in `agents.yaml` defines `trust_level` as a static initial value. `RelationshipMemory` must seed its runtime trust scores from these config-defined values on first initialization. Without this, carefully authored relationship configs (e.g., `trust_level: 0.9` between a manager and report) would be ignored, and all relationships would start at the 0.5 default.

**Seeding logic** (in `RelationshipMemory.initialize()`):
1. Read the agent's `relationships` config entries
2. For each entry with a `trust_level`, insert into the `relationships` table **only if no row exists** for that `(agent_id, other_agent_id)` pair
3. Existing rows (from prior runs) are never overwritten — config seeds the initial state, runtime behavior evolves it

This ensures config-defined trust is respected on first run but doesn't reset trust that has evolved through actual interactions.

### Agent-Initiated Memory Tools

The three memory tiers (working, episodic, relationship) are **framework-managed** — the system automatically stores episodes after interactions and updates trust scores. The agent has no say in what gets remembered. This is insufficient: agents need to *deliberately* capture structured knowledge — "this API returns paginated results", "the PM prefers bullet-point status updates", "last time we used this approach it failed because of X".

This is fundamentally an **agent action**, not a framework side-effect. It belongs in the tool layer, not the memory infrastructure.

#### Why tools, not a skill or memory config

| Approach | Pros | Cons |
|----------|------|------|
| **Tool** (`@tool` decorator) | Fits existing architecture. Agent decides when to use it. Gets permission gating, auditing, budget tracking for free. LLMs understand tool-calling natively. | Agent must be prompted to use it (system prompt injection). |
| **Skill** (system prompt only) | No new code needed — just tell the agent to "take notes". | No persistence mechanism. Notes vanish with the context window. LLM compliance is unreliable. |
| **Automatic framework behavior** | Zero agent effort. | Can't know what the agent considers noteworthy. Over-stores noise or misses insights. |

Tools are the right layer because note-taking is a **deliberate agent decision with side effects** (database write), which is exactly what the tool abstraction models.

#### Tool Definitions

Three built-in memory tools, registered in `agents/tools/builtin.py` alongside `file_read`, `file_write`, etc.:

```python
@tool(
    name="store_note",
    description="Save a structured note for future reference. Use this to remember important facts, decisions, patterns, or lessons learned.",
    permissions=["memory:write"],
)
async def store_note(
    topic: str,
    content: str,
    tags: list[str] | None = None,
) -> ToolResult:
    """Store a note in the agent's personal knowledge base."""
    ...


@tool(
    name="recall_notes",
    description="Search your saved notes by topic or keyword. Returns the most relevant notes.",
    permissions=["memory:read"],
)
async def recall_notes(
    query: str,
    limit: int = 5,
) -> ToolResult:
    """Retrieve notes matching the query using FTS5 text search."""
    ...


@tool(
    name="update_note",
    description="Update or refine a previously saved note.",
    permissions=["memory:write"],
)
async def update_note(
    note_id: str,
    content: str,
) -> ToolResult:
    """Replace the content of an existing note. Topic and tags are preserved."""
    ...


@tool(
    name="delete_note",
    description="Delete a note that is no longer accurate or relevant. Use this for self-correction when you realize a previously saved note is wrong or obsolete.",
    permissions=["memory:write"],
)
async def delete_note(
    note_id: str,
) -> ToolResult:
    """Remove a note from the agent's personal knowledge base. The note
    is permanently deleted — use update_note if the note just needs
    correction rather than removal."""
    ...
```

#### Storage Schema

Notes share the existing SQLite database (Q1 decision) with a new table:

```sql
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    content TEXT NOT NULL,
    tags_json TEXT,              -- JSON array of string tags
    access_count INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX idx_notes_agent ON notes(agent_id);
CREATE INDEX idx_notes_topic ON notes(topic);

-- FTS5 for recall_notes search
-- Note: content_rowid=rowid uses the implicit integer rowid that SQLite
-- auto-generates for tables without an INTEGER PRIMARY KEY. The notes
-- table uses TEXT PRIMARY KEY (id), but SQLite always exposes an implicit
-- integer rowid alongside it. FTS5 requires this integer rowid for
-- content sync — it is NOT the TEXT id column.
CREATE VIRTUAL TABLE notes_fts USING fts5(
    topic, content, tags_json,
    content=notes, content_rowid=rowid
);
```

Like `EpisodicMemory` and `RelationshipMemory`, notes are **agent-scoped** — every query is filtered by `agent_id`, fixed at the memory instance level. An agent cannot access another agent's notes without constructing a new instance (same isolation pattern as [Memory Isolation](#memory-isolation-shared-database)).

> **Design note — `agent_id` injection into memory tools**: The tool function signatures above don't include `agent_id` because it must not be LLM-controllable (an agent should not be able to read/write another agent's notes by passing a different ID). Instead, memory tools are instantiated per-agent at agent construction time using a **closure-based factory**: `PersonaAgent.__init__()` creates tool instances that capture `self.agent_id` in a closure, then registers them in the agent's tool registry. This extends the existing `@tool` pattern (which uses module-level registration in `builtin.py`) with agent-scoped tool instances. The factory pattern looks like:
>
> ```python
> def create_memory_tools(agent_id: str, db: aiosqlite.Connection) -> list[Tool]:
>     @tool(name="store_note", permissions=["memory:write"])
>     async def store_note(topic: str, content: str, ...) -> ToolResult:
>         # agent_id captured from closure — not an LLM parameter
>         await db.execute("INSERT INTO notes ... WHERE agent_id = ?", (agent_id, ...))
>         ...
>     return [store_note, recall_notes, update_note, delete_note]
> ```
>
> This ensures `agent_id` is fixed at construction, not per-call, and that multi-agent processes get correctly scoped tool instances.

#### Configurable Learning Behavior

Note-taking ability is controlled per-agent in the `memory` config section. This is the **hard infrastructure gate** — tool permissions in `agents.yaml` control *whether* an agent can take notes, while the `memory.notes` config controls *how much* and *how often*.

```yaml
# config/agents.yaml — persona agent with note-taking enabled
- id: "sarah-chen"
  type: "persona"
  memory:
    db_path: "data/memory.db"
    notes:
      enabled: true              # default: true for persona agents, false for task agents
      max_notes: 500             # per-agent cap; oldest low-access notes pruned when exceeded
      auto_reflect_after: 5      # after N interactions, inject "consider what's worth noting" nudge
      inject_recent_notes: 3     # include N most relevant notes in context per LLM call
```

**How `auto_reflect_after` works**: After every N interactions (configurable, default 5), the framework appends a one-line nudge to the system prompt: *"You have completed {N} interactions since your last note. Consider whether any patterns, decisions, or lessons are worth recording with `store_note`."* This is a **soft behavioral nudge**, not a forced action — the LLM decides whether to call the tool. Agents with `detail_focus: detail-focused` naturally respond to this nudge more often than `big-picture` agents, creating personality-consistent learning behavior without a dedicated "learning" dimension.

**Interaction with `notes.enabled: false`**: When `memory.notes.enabled` is `false`, the `auto_reflect_after` nudge is not injected and the interaction counter is not maintained. Since the agent cannot use `store_note` in this configuration, the reflection nudge would be a wasted prompt token with no actionable outcome.

**Counter persistence**: The interaction counter for `auto_reflect_after` is stored in the SQLite database (a lightweight `agent_state` table keyed by `agent_id`) so that it survives process restarts. Without persistence, agents restarting frequently would never reach the nudge threshold.

**What about auto-extracting lessons?** A tempting alternative is framework-initiated reflection: after every interaction, the system calls the LLM asking "what should this agent remember?" and auto-stores the result. This is explicitly **not** done because:
1. It doubles LLM costs (extra call per interaction)
2. The framework can't know what the agent's persona would consider noteworthy
3. It removes agency — the agent should decide what matters, not the framework
4. Over-storage creates noise that degrades retrieval quality

**Relationship to existing memory tiers:**

| Concern | Episodic Memory | Agent Notes |
|---------|----------------|-------------|
| **Who decides to store** | Framework (automatic after interactions) | Agent (deliberate tool call) |
| **Content type** | Conversation summaries, outcomes | Facts, patterns, lessons, structured knowledge |
| **Granularity** | One episode per conversation/interaction | As granular as the agent wants |
| **Lifecycle** | Auto-compressed over time (raw → summarized → distilled) | Persistent until explicitly updated or pruned by cap |
| **Retrieval** | `recall()` by query relevance | `recall_notes()` by topic/keyword |

Both use the same SQLite database. Episodic memory is the agent's autobiography; notes are the agent's personal knowledge base.

### Dynamic Persona State

Runtime state injected into the system prompt before each LLM call. `mood` is constrained to an enum (not free-text) to prevent unpredictable drift when the framework auto-updates mood from interaction analysis. `energy` decays per action and recovers on idle, naturally pairing with the tick loop's `idle_after_ticks` mechanic.

```python
class Mood(Enum):
    """Constrained mood states. Each maps to known prompt behavior."""
    NEUTRAL = "neutral"
    FOCUSED = "focused"
    FRUSTRATED = "frustrated"
    ENERGIZED = "energized"
    UNCERTAIN = "uncertain"
    SATISFIED = "satisfied"


@dataclass
class PersonaState:
    """Mutable runtime state for a persona agent."""
    mood: Mood = Mood.NEUTRAL
    stress_level: float = 0.0           # 0.0 - 1.0
    energy: float = 1.0                 # 0.0 - 1.0, decays per action, recovers on idle
    recent_context: list[str] = field(default_factory=list)
    goal_progress: dict[str, float] = field(default_factory=dict)

    def to_prompt_section(self) -> str:
        """Format state for injection into system prompt."""
        lines = [f"Current mood: {self.mood.value}"]
        if self.stress_level > 0.3:
            lines.append(f"Stress level: {self.stress_level:.1f}/1.0")
        if self.energy < 0.5:
            lines.append(f"Energy level: {self.energy:.1f}/1.0 — conserve effort, prefer delegation")
        if self.recent_context:
            lines.append("Recent context:")
            for ctx in self.recent_context[-5:]:
                lines.append(f"  - {ctx}")
        if self.goal_progress:
            lines.append("Goal progress:")
            for goal, progress in self.goal_progress.items():
                lines.append(f"  - {goal}: {progress:.0%}")
        return "\n".join(lines)
```

**Energy mechanics:**
- Each LLM call / action costs `0.05` energy (configurable)
- Each idle tick recovers `0.1` energy (configurable)
- For non-tick agents (`passive` / `reactive` level without a tick loop), energy recovers passively at `+0.1` per elapsed `tick_interval_seconds` since the last action, computed lazily on the next `on_event()` call. This prevents agents without tick loops from permanently draining energy. **Default recovery interval**: If the agent has no `autonomy` config section (or no `tick_interval_seconds`), the default of 60 seconds is used as the recovery interval. The formula is: `recovery = 0.1 × floor(elapsed_seconds / recovery_interval)`, clamped to `[0.0, 1.0]`.
- Low energy (< 0.3) → shorter responses, more delegation, less initiative
- Energy is clamped to `[0.0, 1.0]`

#### PersonaState Persistence

`PersonaState` is in-memory during normal operation for performance, but must survive process restarts. Without persistence, an agent that has been running for hours — accumulating stress, tracking goal progress, draining energy — resets to defaults on restart.

**Persistence strategy**: Serialize `PersonaState` to the `agent_state` table (see [agent_state schema](#agent_state-schema)) at the end of each `on_event()` and `on_tick()` cycle. Load from the table during `initialize()`. If no row exists (first run), use dataclass defaults.

```python
# At the end of on_event() / on_tick():
await self._persist_persona_state()

# During initialize():
self.persona_state = await self._load_persona_state() or PersonaState()
```

This ensures mood, stress, energy, and goal progress are recovered after restarts. The `recent_context` list is **not** persisted — it is rebuilt from working memory on the next event.

### Behavioral Dimensions & Rendering

The persona `behavior` config uses **five structured 3-point enum dimensions** instead of free-text `traits` / `communication_style` / `decision_making`. This ensures consistent, comparable, and validatable personality configuration across agents.

**Why structured dimensions over free-text:**
- Config authors pick from a small set of meaningful knobs — two agents' differences are visible at a glance
- The LLM gets a consistent, well-formed natural language description (not raw enums)
- The framework can programmatically compare personalities, validate configs, and algorithmically generate personality profiles for experiments
- 5 dimensions × 3 values = 243 distinct personality profiles — more than enough

**Why 3-point enums, not floats or 5-point scales:** LLMs can't distinguish `assertiveness: 0.55` from `assertiveness: 0.65` in behavioral output. Three levels match the resolution at which LLMs actually produce different behavior.

#### Dimension Definitions

| Dimension | Values | What it controls |
|-----------|--------|------------------|
| `directness` | `indirect` \| `balanced` \| `direct` | Hedging, confrontation, qualification. The single most impactful axis. |
| `detail_focus` | `big-picture` \| `balanced` \| `detail-focused` | Depth of analysis, response length, what gets flagged vs ignored |
| `formality` | `casual` \| `professional` \| `formal` | Tone, word choice, structure. Drives cross-hierarchy interaction feel. |
| `risk_tolerance` | `cautious` \| `moderate` \| `bold` | Decision hedging, willingness to act on incomplete info, approval-seeking. Feeds into autonomy behavior. |
| `expressiveness` | `reserved` \| `moderate` \| `expressive` | Emotional language, reactions to events, how feelings enter reasoning |

**Defaults**: All five dimensions default to their middle value (`balanced` or `moderate`) when not specified in config. For persona agents, the `behavior` object is **required** in validation, but individual dimensions within it are optional — omitted dimensions use their default. This means a config that specifies only `directness: direct` produces a valid agent with `direct` directness and `balanced`/`moderate` for the other four dimensions. Task agents do not have a `behavior` section.

#### Rendering Layer

A `render_behavior()` function maps dimensions to natural language for the system prompt:

```python
DIMENSION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "directness": {
        "indirect": "Diplomatic and tactful. Softens criticism, asks questions instead of stating objections directly.",
        "balanced": "Balances directness with tact. States positions clearly but frames feedback constructively.",
        "direct": "Says exactly what they think. Doesn't sugarcoat feedback or hedge opinions.",
    },
    "detail_focus": {
        "big-picture": "Focuses on high-level patterns and architecture. Skips minutiae to keep discussions strategic.",
        "balanced": "Addresses both high-level concerns and specific details as needed.",
        "detail-focused": "Thorough and meticulous. Flags edge cases, checks specifics, prefers exhaustive analysis.",
    },
    "formality": {
        "casual": "Informal and approachable. Uses humor, contractions, and conversational language.",
        "professional": "Clear and structured. Uses professional language without being stiff.",
        "formal": "Precise and formal. Uses structured reports, proper titles, and measured language.",
    },
    "risk_tolerance": {
        "cautious": "Wants thorough analysis before decisions. Asks for more data. Flags risks others might overlook.",
        "moderate": "Balances speed with diligence. Comfortable with reasonable assumptions.",
        "bold": "Willing to make calls with incomplete information and course-correct. Bias toward action.",
    },
    "expressiveness": {
        "reserved": "Keeps emotions out of professional communication. Focuses on facts and logic.",
        "moderate": "Acknowledges emotions when relevant but keeps focus on substance.",
        "expressive": "Openly shares reactions and feelings. Communication is warm, enthusiastic, or frustrated as the situation warrants.",
    },
}


def render_behavior(behavior: dict[str, str]) -> str:
    """Convert structured behavior dimensions into natural language for LLM prompt."""
    lines: list[str] = []
    for dimension, value in behavior.items():
        desc = DIMENSION_DESCRIPTIONS.get(dimension, {}).get(value)
        if desc:
            lines.append(f"- {desc}")
    return "\n".join(lines)
```

This ensures 50 different config authors produce identically-formatted personality descriptions, varying only along intentional dimensions.

#### What replaced what

| Old field (extension spec E2.1) | Replacement | Rationale |
|---|----|---|
| `traits: [pragmatic, direct]` | `behavior` dimensions | Traits are ambiguous labels; dimensions are behavioral levers. "Pragmatic" = `risk_tolerance: moderate` + `detail_focus: big-picture` |
| `communication_style` (free-text) | `directness` + `formality` + `detail_focus` | These three dimensions cover what people actually varied in `communication_style`, but consistently |
| `decision_making` (free-text) | `risk_tolerance` + `directness` | "Data-driven" → `risk_tolerance: cautious`. "Trusts instincts" → `risk_tolerance: bold` |

**Kept as free-text:** `background` and `quirks` — these set unique character voice and flavor, not controllable behavioral variation.

### Data-Driven TaskAgent Consolidation

Replace `CoderAgent`, `ReviewerAgent`, `PlannerAgent` with a single class:

```python
class TaskAgent(BaseAgent):
    """Data-driven task agent. Behavior configured via YAML instructions field."""

    async def handle(self, task: TaskInput) -> TaskOutput:
        instructions = self.config.get("instructions", "")
        # Preserve the "Role: ..." prefix that existing agents (CoderAgent,
        # ReviewerAgent, PlannerAgent) prepend — dropping it would be a
        # subtle behavioral regression affecting LLM output quality.
        system_prompt = f"Role: {self.role}\n\n{instructions}"
        return await self._run_llm_loop(task, system_prompt=system_prompt)
```

The three existing agents become config entries:

```yaml
agents:
  - id: "coder-01"
    type: "task"
    instructions: |
      You are a code generation specialist. Write clean, well-tested code
      following the project's conventions. Include error handling and type hints.
    model: "claude-sonnet-4-20250514"
    tools: [file_read, file_write, shell_exec]

  - id: "reviewer-01"
    type: "task"
    instructions: |
      You are a code review specialist. Analyze code for bugs, performance,
      security, and style issues. Provide actionable feedback.
    model: "claude-sonnet-4-20250514"
    tools: [file_read]

  - id: "planner-01"
    type: "task"
    instructions: |
      You are a task decomposition specialist. Break complex requests into
      ordered subtasks with clear deliverables and dependencies.
    model: "claude-sonnet-4-20250514"
    tools: []
```

### Config Schema Updates

Update `schemas/agent.schema.json` to add:
- `type` enum: `["task", "persona"]`
- `instructions` string field (required for `type: task`)
- `persona` object (required for `type: persona`), containing:
  - `title`, `background` (string)
  - `behavior` object with 5 enum-constrained dimensions: `directness` (`indirect` | `balanced` | `direct`), `detail_focus` (`big-picture` | `balanced` | `detail-focused`), `formality` (`casual` | `professional` | `formal`), `risk_tolerance` (`cautious` | `moderate` | `bold`), `expressiveness` (`reserved` | `moderate` | `expressive`)
  - `quirks` (array of strings)
  - `goals` object (`primary`, `secondary`, `hidden`)
  - `knowledge` object (`domains`, `limitations`)
- `autonomy` object with `level`, `tick_interval_seconds`, `max_actions_per_tick`, `idle_after_ticks`
- `relationships` array
- `memory` configuration object with `db_path` (string), `notes` object (`enabled` bool, `max_notes` int, `auto_reflect_after` int, `inject_recent_notes` int), `relationship` object (`decay_rate` float, default 0.01 — per-cycle trust decay rate toward neutral, aligning with extension spec E7.2)

### CLI Command Wiring

The Rust CLI (`cli/src/main.rs`) already defines command structures for all v0.2 features, but every handler is a stub. As each phase lands server-side features, the corresponding CLI commands get wired to REST endpoints.

The CLI is a **thin client** — all business logic lives server-side. CLI work is not a standalone RFC; it tails each feature phase.

#### Existing v0.1 endpoints (wire in Phase 1)

These REST endpoints already exist but the CLI stubs print "not yet implemented":

| CLI Command | REST Endpoint | Status |
|-------------|--------------|--------|
| `orch run <workflow>` | `POST /api/v1/workflows/run` | Stub → wire |
| `orch status [id]` | `GET /api/v1/workflows/{id}/status` | Stub → wire |
| `orch agent list` | `GET /api/v1/agents` | Stub → wire |
| `orch agent info <id>` | `GET /api/v1/agents/{id}` | Stub → wire |
| `orch logs <id>` | `GET /api/v1/executions/{id}/logs` | Stub → wire |

#### New v0.2 endpoints (wire as phases land)

| CLI Command | REST Endpoint | Wired in Phase |
|-------------|--------------|----------------|
| `orch validate <path>` | Local (calls Python validator or validates in-process) | Phase 6 |
| `orch test --persona <id>` | New endpoint or local persona consistency check | Phase 5 |
| `orch cost <period>` | `GET /api/v1/cost/summary` (exists as stub) | Future RFC |
| `orch replay <session>` | New endpoint (observer mode) | Future RFC |

#### Implementation pattern

Each CLI command follows the same pattern — `reqwest` HTTP call, deserialize JSON, format output:

```rust
Commands::Run { workflow, input, profile } => {
    let url = format!("{}/api/v1/workflows/run", cli.server);
    let body = serde_json::json!({
        "workflow_file": workflow,
        "input": input.unwrap_or_default(),
        "profile": profile,
    });
    let resp = reqwest::Client::new()
        .post(&url)
        .json(&body)
        .send()
        .await?;
    let result: serde_json::Value = resp.json().await?;
    println!("{}", serde_json::to_string_pretty(&result)?);
}
```

### Go Orchestrator Impact

This RFC focuses on Python agent and Rust CLI changes. The Go orchestrator requires corresponding changes that are **documented here for awareness** but scoped to a follow-up RFC (or implemented as part of Phase 5 if small enough). The Python agent runtime can be developed and tested independently using mock orchestrator interactions.

#### Assumptions This RFC Makes About the Orchestrator

1. **Agent type routing**: The orchestrator's registry must understand `type: task` vs `type: persona` to route work appropriately. Task agents receive `ExecuteTask` RPCs; persona agents receive events. For Phase 1-4, persona agents are backward-compatible via the `handle()` bridge — the orchestrator can treat them as task agents. Phase 5 requires the orchestrator to dispatch events.
2. **Unidirectional gRPC (for now)**: The current gRPC contract (`task.proto`, `agent_message.proto`) is orchestrator → agent only. This RFC's `ActionExecutor` handles `SEND_MESSAGE`, `DELEGATE`, and `SPAWN_SUB_AGENT`, all of which need to call **back** to the orchestrator. For MVP, these actions are handled within the Python process (single-process multi-agent hosting), avoiding the need for reverse gRPC. Cross-process action execution requires the callback protocol below.
3. **Tick loop ownership**: The tick loop runs in the Python agent process (`TickScheduler`), not in the Go orchestrator. The orchestrator does not need to coordinate tick scheduling — it only needs to avoid sending tasks during ticks, which the per-agent `asyncio.Lock` handles.

#### Deferred Orchestrator Changes (Follow-Up RFC)

| Change | Why Needed | When |
|--------|-----------|------|
| **Agent type field in registry** | Route events to persona agents, tasks to task agents | When events cross process boundaries |
| **Event routing service** | Dispatch `AgentEvent`s over gRPC to persona agents in separate processes | Multi-process deployment |
| **Agent→Orchestrator callback proto** | `SEND_MESSAGE`, `DELEGATE`, `SPAWN_SUB_AGENT` actions need a reverse gRPC service | Cross-process persona interactions |
| **Tick awareness** | Orchestrator avoids assigning tasks during tick execution | Optimization (not blocking) |

**Proto changes for events/actions**: The 10 event types and 9 action types defined in this RFC are Python-only enums. If events flow over gRPC (multi-process deployment), these need proto definitions. For MVP (single-process), Python enums are sufficient. Proto definitions should be added when the orchestrator event routing RFC is written, ensuring the enum values are wire-compatible.

#### Callback Protocol Sketch

When agent-initiated actions need to cross process boundaries, a new gRPC service is required:

```protobuf
// Future: orchestrator_callback.proto
service OrchestratorCallback {
  rpc SendMessage(SendMessageRequest) returns (SendMessageResponse);
  rpc DelegateTask(DelegateTaskRequest) returns (DelegateTaskResponse);
  rpc SpawnSubAgent(SpawnSubAgentRequest) returns (SpawnSubAgentResponse);
}
```

This is explicitly **not** implemented in this RFC. The `OrchestratorClient` protocol in `persona.py` is the Python-side interface that will wrap these RPCs when they're available. For now, `ActionExecutor` handles actions in-process.

### REST API for Persona Observability

Persona agent state, memory contents, and relationship data need REST endpoints for debugging and monitoring. Without these, diagnosing persona agent behavior requires direct SQLite inspection.

**Endpoint signatures** (implementation deferred to Phase 5 or follow-up, but defined here for API contract stability):

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/agents/{id}/state` | GET | Current `PersonaState` (mood, stress, energy, goal_progress) |
| `/api/v1/agents/{id}/memory/episodes` | GET | Recent episodes with optional `?query=` FTS5 search |
| `/api/v1/agents/{id}/memory/notes` | GET | Agent's notes with optional `?topic=` filter |
| `/api/v1/agents/{id}/memory/relationships` | GET | Trust scores and interaction summaries for all known agents |
| `/api/v1/agents/{id}/tick` | POST | Manually trigger a single `on_tick()` cycle (debug/test only) |

These endpoints are served by the Go orchestrator, which proxies to the Python agent process. Authentication and authorization follow the existing REST API patterns. The `POST .../tick` endpoint is restricted to development/staging environments.

### SQLite Schema Migration Strategy

The memory system defines schemas for `episodes`, `relationships`, `interactions`, `notes`, and `agent_state` tables. Future phases will evolve these schemas (e.g., adding the `embedding` column for vector search). A versioned migration strategy is required from day one.

**Approach: `schema_version` table + ordered migration functions.**

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL,
    description TEXT
);
```

Each memory class's `initialize()` method:
1. Creates `schema_version` if it doesn't exist
2. Reads the current max version (0 if no rows)
3. Applies all migration functions with version > current, in order
4. Records each applied migration in `schema_version`

```python
MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "Initial schema", """
        CREATE TABLE IF NOT EXISTS episodes (...);
        CREATE TABLE IF NOT EXISTS notes (...);
        -- ... all Phase 3 tables
    """),
    # (2, "Add embedding column", "ALTER TABLE episodes ADD COLUMN embedding BLOB;"),
]

async def _apply_migrations(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, description TEXT)"
    )
    row = await db.execute_fetchone("SELECT MAX(version) FROM schema_version")
    current = row[0] or 0
    for version, desc, sql in MIGRATIONS:
        if version > current:
            await db.executescript(sql)
            await db.execute(
                "INSERT INTO schema_version VALUES (?, ?, ?)",
                (version, time.time(), desc),
            )
    await db.commit()
```

Migrations are forward-only (no rollback) — this is sufficient for an embedded database where the deployment unit is the whole application.

#### `agent_state` Schema

The `agent_state` table stores per-agent persistent state that must survive process restarts — the `auto_reflect_after` interaction counter (see [Configurable Learning Behavior](#configurable-learning-behavior)) and the serialized `PersonaState` (see [PersonaState Persistence](#personastate-persistence)):

```sql
CREATE TABLE IF NOT EXISTS agent_state (
    agent_id TEXT PRIMARY KEY,
    interaction_count INTEGER DEFAULT 0,   -- counter for auto_reflect_after nudge
    persona_state_json TEXT,               -- serialized PersonaState (mood, stress, energy, goal_progress)
    updated_at REAL NOT NULL
);
```

This table is created in the initial migration (migration version 1) alongside `episodes`, `notes`, and other tables. The `persona_state_json` column stores a JSON blob with fields: `mood` (string enum value), `stress_level` (float), `energy` (float), `goal_progress` (dict). The `recent_context` list from `PersonaState` is **not** persisted — it is rebuilt from working memory on the next event.

### Memory Observability

Memory operations should emit structured metrics for debugging behavioral issues in production. The following metrics should be tracked using the existing telemetry infrastructure (Go orchestrator exposes Prometheus metrics; Python agents log structured events that the orchestrator can aggregate):

| Metric | Type | Labels | Purpose |
|--------|------|--------|---------|
| `memory_episode_store_duration_ms` | Histogram | `agent_id` | Detect slow SQLite writes |
| `memory_episode_recall_duration_ms` | Histogram | `agent_id` | Detect slow FTS5 queries |
| `memory_notes_count` | Gauge | `agent_id` | Track note accumulation toward `max_notes` |
| `memory_working_compression_count` | Counter | `agent_id` | Track context window pressure |
| `memory_trust_score` | Gauge | `agent_id`, `other_agent_id` | Monitor trust score distributions |
| `memory_fts5_query_duration_ms` | Histogram | `agent_id`, `table` | FTS5 performance per table |

Implementation: Each memory class includes a `_emit_metric()` call at operation boundaries. For MVP, these are structured log lines (`logger.info("memory.metric", ...)`) that can be scraped by the orchestrator's telemetry pipeline. Prometheus instrumentation is a post-MVP enhancement.

## Security Considerations

### Memory Data at Rest

Episodic and relationship memory are stored in SQLite. For v0.2 MVP, the database file is local and unencrypted. Sensitive data (conversation content, trust scores) is accessible to anyone with filesystem access to the data directory.

**Mitigations:**
- Data directory is configurable and should be placed on encrypted volumes in production
- Memory entries do not store raw LLM API keys or credentials
- Agent permission boundaries still apply — agents can only access their own memories

### Memory Isolation (Shared Database)

The shared SQLite database (Q1 decision) has no built-in row-level security. Application-level enforcement is **security-critical**: every query against the shared database must filter by `agent_id` to prevent cross-agent memory leakage.

**Enforcement pattern:** All memory classes (`EpisodicMemory`, `RelationshipMemory`) must accept `agent_id` at construction and automatically scope every query. The `agent_id` must never be a parameter of individual query methods — it is fixed at the instance level. This makes cross-agent access structurally impossible without constructing a new instance. The same pattern applies to FTS5 queries, which must include `agent_id` filtering to prevent cross-agent content leakage via full-text search.

Integration tests must verify that agent A cannot retrieve agent B's episodes or relationship data through the shared database connection.

### Persona State Injection

Dynamic persona state is injected into LLM prompts. Adversarial input in event payloads could attempt to manipulate the persona state section.

**Mitigations:**
- Persona state fields have type validation (`mood` is enum-constrained to 6 values, `stress_level` and `energy` are clamped 0.0-1.0)
- Behavioral dimensions are enum-constrained (3 values per dimension) — free-text personality injection is not possible
- State is constructed from trusted internal sources (framework-managed), not from raw user input
- Goal progress values are numeric, not free-text

### Trust Score Manipulation

Relationship trust scores influence agent behavior. A compromised agent could send events designed to inflate trust with a target agent.

**Mitigations:**
- Trust deltas are clamped (max ±0.2 per interaction, configurable)
- Trust decay naturally reverts scores toward neutral
- Audit logging of all trust changes for post-hoc review

### Autonomous Loop Resource Consumption

The tick loop could cause runaway LLM calls if not bounded.

**Mitigations:**
- `max_actions_per_tick` limits actions per cycle (default: 3)
- `idle_after_ticks` reduces activity when nothing is happening
- Per-agent token budgets (from `config/optimization.yaml`) apply to tick-initiated calls
- **Framework-initiated memory operations** (working memory compression, episode summarization, `auto_reflect_after` nudges leading to tool calls) also consume tokens and must be attributed to the agent's budget. These costs can spike during periods of high activity — per-agent budget enforcement must include framework-initiated LLM calls, not just agent-initiated ones

**Budget integration details**: The memory system reads the agent's token budget from `config/optimization.yaml` via the agent config's `model` field (which maps to a pricing/budget entry). Framework-initiated LLM calls (compression, summarization) use the same `LLMClient` as agent-initiated calls, so budget tracking is automatic if the client enforces it. When the budget is exhausted:
- **Working memory compression**: Skipped — the agent operates with uncompressed context, which may cause LLM context overflow errors. This is preferable to silent data loss.
- **Episode summarization**: Skipped — old episodes remain unsummarized but are still queryable.
- **`auto_reflect_after` nudge**: Still injected (it's just a prompt string, no LLM cost), but if the agent's tool call triggers an LLM response, that response is subject to the budget.

### Agent Note Storage Abuse

Memory tools allow agents to write to the shared SQLite database. A compromised or misbehaving agent could flood the notes table with garbage data, consuming disk space and degrading FTS5 query performance.

**Mitigations:**
- `max_notes` per-agent cap (default 500) with automatic pruning of oldest low-access notes
- `memory:write` permission required — deny-by-default; task agents don't get it unless explicitly configured
- Note content size is bounded (configurable max, default 10KB per note)
- Agent-scoped isolation (same pattern as episodic/relationship memory) — an agent can only write/read its own notes
- Note writes are logged for audit

### Error Recovery for Memory Failures

SQLite writes can fail due to disk full, permission errors, WAL checkpoint failures, or database corruption. The MVP policy is **best-effort persistence** — memory failures don't crash the agent:

| Failure | Behavior | Rationale |
|---------|----------|-----------|
| Episodic memory write fails | Log error, continue processing. Agent loses this episode but completes the current interaction. | Episodes are summaries — the interaction result is already returned to the caller. |
| Relationship memory write fails | Log error, continue. Trust score update is lost. | Next interaction will re-derive trust delta. |
| Note write fails | Return error to LLM via `ToolResult`. Agent sees the failure and can retry or skip. | Agent has agency over note-taking — surfacing the error is more useful than hiding it. |
| Working memory (in-process) | Always available — no external dependency. | No failure mode. |
| `initialize()` fails | **Fatal** — agent cannot start without memory tables. Propagate error, fail agent registration. | Running without any persistence would produce silently wrong behavior. |

This policy is logged via the `memory_*` metrics (see [Memory Observability](#memory-observability)) so operators can detect persistent storage issues.

## Phased Implementation Plan

### Phase 1: Agent Type System + Data-Driven TaskAgent + CLI Wiring

**Summary**: Add `type` field to agent config, consolidate task agents into `TaskAgent`, update agent loader. Wire existing v0.1 CLI stubs to REST endpoints.

**Deliverables**:
1. Add `type` field to `schemas/agent.schema.json`
2. Create `TaskAgent` class with YAML `instructions` support
3. Update `server.py` agent loader to dispatch on `type`
4. Migrate existing `agents.yaml` entries to use `type: task` + `instructions`
5. Remove `CoderAgent`, `ReviewerAgent`, `PlannerAgent` classes (move instructions to YAML)
6. Tests for `TaskAgent` and agent loading
7. Wire CLI: `orch run`, `orch status`, `orch agent list`, `orch agent info`, `orch logs` → existing REST endpoints
8. Add `reqwest` + `serde_json` + `tokio` dependencies to `cli/Cargo.toml`

**Dependencies**: None (builds on v0.1 agent infrastructure)

### Phase 2: Working Memory + Token Estimation

**Summary**: Implement context window manager with priority sections and automatic summarization.

**Deliverables**:
1. `ContextSection` dataclass and `WorkingMemory` class
2. Token estimation function (chars/4 MVP)
3. Priority-weighted compression (summarize lowest-priority sections first)
4. Integration with `BaseAgent._run_llm_loop()` to pass working memory context
5. Unit tests for token counting, section prioritization, compression

**Dependencies**: Phase 1

### Phase 3a: Schema Migration + Episodic Memory Core

**Summary**: Implement SQLite schema migration infrastructure and core episodic memory (store/recall).

**Deliverables**:
1. SQLite schema migration infrastructure (`schema_version` table, ordered migrations)
2. Initial migration: `episodes` + `agent_state` tables, FTS5 index for episodes, sync triggers
3. `EpisodicMemory` class with `store_episode()` and `recall()` (FTS5-based)
4. FTS5 availability check with `LIKE`-based fallback (see [Episodic Memory](#episodic-memory-long-term-storage))
5. Integration with persona event handling (store episode after each interaction)
6. Unit tests for CRUD, retrieval ranking, FTS5 trigger sync, migration forward-application

**Dependencies**: Phase 2 (uses token estimation for compression)

### Phase 3b: Agent-Initiated Memory Tools

**Summary**: Add `store_note`, `recall_notes`, `update_note`, `delete_note` built-in tools for agent-initiated knowledge capture.

**Deliverables**:
1. Migration: `notes` table + `notes_fts` FTS5 index + sync triggers
2. `store_note`, `recall_notes`, `update_note`, `delete_note` tools in `agents/tools/builtin.py`
3. Notes pruning logic (oldest low-access notes removed when `max_notes` exceeded)
4. `auto_reflect_after` nudge injection + counter persistence in `agent_state` table
5. Unit tests for note CRUD, FTS5 search ranking, pruning, nudge injection

**Dependencies**: Phase 3a (shares SQLite infrastructure and migration system)

### Phase 3c: Episode Auto-Summarization

**Summary**: Implement LLM-based compression of old episodes and reflection nudge behavior.

**Deliverables**:
1. `summarize_old_episodes()` method with LLM-based compression (raw → summarized → distilled)
2. Configurable summarization thresholds (`older_than_days`, `compression_level`)
3. `auto_reflect_after` counter persistence across restarts (round-trip test)
4. Unit tests for summarization, compression level transitions

**Dependencies**: Phase 3b (uses `agent_state` table, episode infrastructure)

### Phase 4: Relationship Memory

**Summary**: Implement per-agent-pair trust tracking and interaction history.

**Deliverables**:
1. SQLite schema (shares database with episodic memory)
2. `RelationshipMemory` class with trust CRUD, decay, interaction logging
3. Relationship summary generation for LLM prompt injection
4. Integration with persona state (trust deltas update relationship memory)
5. Unit tests for trust math, decay, interaction recording

**Dependencies**: Phase 3a (shares SQLite infrastructure)

### Phase 5: PersonaAgent Runtime + Event Dispatch

**Summary**: Wire the autonomous tick loop, event dispatcher, and action executor into the gRPC server.

**Deliverables**:
1. `ActionExecutor` class
2. `EventDispatcher` class with `_inject_memory_context()` implementation and idle wake-up signal
3. `TickScheduler` class for autonomous agents (with graceful shutdown and idle wake-up)
4. Per-agent `asyncio.Lock` for concurrency control (see [Concurrency Control](#concurrency-control))
5. `PersonaState` dataclass (`Mood` enum, `energy` field), prompt injection, and persistence to `agent_state` table
6. `render_behavior()` function and `DIMENSION_DESCRIPTIONS` mapping (with dimension defaults)
7. Wire into `server.py`: memory lifecycle calls at startup/shutdown, start tick loops for persona agents, route events
8. `create_persona_agent()` factory with LLM-powered multi-turn `on_event()` decision loop
9. Create a sample persona agent config for integration testing
10. Integration tests: event dispatch → on_event → action execution, tick loop lifecycle (including idle/wake), concurrency lock
11. Wire CLI: `orch test --persona <id>` for persona consistency checks

**Dependencies**: Phases 1-4

### Phase 6: Config Validation + Schema Updates

**Summary**: Implement the `validate.py` config validator with JSON Schema support. Schema structure is updated in Phase 1 (to support `type` field), but persona-specific field validation is fully testable only after Phase 5 lands all persona config fields.

**Deliverables**:
1. Implement `validate_config_dir()` using `jsonschema` library
2. Update `schemas/agent.schema.json` for persona fields (behavioral dimension defaults, memory config, autonomy)
3. Add `make validate` target that actually works
4. Unit tests for validation pass/fail cases (including persona-specific fields, behavioral dimension enum validation, `behavior` defaults)
5. Wire CLI: `orch validate <path>` — invoke Python validator or implement JSON Schema validation in Rust

**Dependencies**: Phase 5 (all persona config fields must exist to validate them fully)

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents | `agents/base.py` | Add working memory integration to `_run_llm_loop()` |
| Python agents | `agents/task_agent.py` | **New** — data-driven TaskAgent class |
| Python agents | `agents/persona.py` | Add `PersonaState` (`Mood` enum, `energy`), `render_behavior()`, memory integration, `ActionExecutor`, `metadata` field on `AgentEvent`, per-agent `asyncio.Lock` for concurrency control, PersonaState persistence |
| Python agents | `agents/memory/working.py` | **Implement** — `WorkingMemory`, `ContextSection`, token estimation (with accuracy note) |
| Python agents | `agents/memory/episodic.py` | **Implement** — `EpisodicMemory`, SQLite schema, migration infrastructure, FTS5 availability check with fallback |
| Python agents | `agents/memory/relationship.py` | **Implement** — `RelationshipMemory`, trust math (bidirectional decay), config trust bootstrapping |
| Python agents | `agents/memory/__init__.py` | Export memory classes |
| Python agents | `agents/tools/builtin.py` | Add `store_note`, `recall_notes`, `update_note`, `delete_note` built-in tools |
| Python agents | `agents/server.py` | Update agent loader, add tick scheduler lifecycle, memory lifecycle (initialize/close), notes config |
| Python agents | `agents/coder.py` | **Remove** (instructions move to YAML) |
| Python agents | `agents/reviewer.py` | **Remove** (instructions move to YAML) |
| Python agents | `agents/planner_agent.py` | **Remove** (instructions move to YAML) |
| Python agents | `agents/validate.py` | **Implement** — JSON Schema config validation |
| Config | `config/agents.yaml` | Add `type`, `instructions` fields; add sample persona agent |
| Config | `schemas/agent.schema.json` | Add persona, autonomy, memory, type fields; behavioral dimension defaults |
| Tests | `tests/unit/python/test_task_agent.py` | **New** — TaskAgent tests |
| Tests | `tests/unit/python/test_working_memory.py` | **New** — working memory tests |
| Tests | `tests/unit/python/test_episodic_memory.py` | **New** — episodic memory tests (including FTS5 fallback) |
| Tests | `tests/unit/python/test_memory_tools.py` | **New** — store_note, recall_notes, update_note, delete_note tool tests |
| Tests | `tests/unit/python/test_relationship_memory.py` | **New** — relationship memory tests (including bidirectional decay) |
| Tests | `tests/unit/python/test_persona_runtime.py` | **New** — event dispatch, tick loop (idle/wake), action executor, concurrency lock, memory lifecycle tests |
| Tests | `tests/unit/python/test_validate.py` | **New** — config validation tests (including dimension defaults) |
| Tests | `tests/integration/test_persona_e2e.py` | **New** — persona agent end-to-end with mock LLM |
| Rust CLI | `cli/src/main.rs` | Wire `run`, `status`, `agent list/info`, `logs`, `validate`, `test --persona` |
| Rust CLI | `cli/Cargo.toml` | Add `reqwest`, `serde_json`, `serde` dependencies |
| Python agents | `agents/pyproject.toml` | Add `aiosqlite` dependency (required by Q1 shared SQLite decision) |

## Test Strategy

- **Unit tests**: Each memory tier tested independently with in-memory SQLite. Token estimation accuracy (including accuracy note for structured/multilingual content). Trust math (clamping, bidirectional decay toward 0.5). PersonaState serialization (Mood enum, energy clamping) and persistence round-trip (serialize → restart → deserialize). Behavioral dimension rendering (all 5 dimensions × 3 values) and defaults for omitted dimensions. TaskAgent YAML-driven behavior. Config validation pass/fail cases (including invalid behavior dimension values). Memory tools (`store_note`, `recall_notes`, `update_note`, `delete_note`) CRUD and permission gating. Note pruning when `max_notes` exceeded. FTS5 search ranking for notes. FTS5 availability check and LIKE-based fallback. FTS5 trigger sync correctness (insert/update/delete). Schema migration forward-application. Energy recovery for non-tick agents (lazy computation with default 60s interval).
- **Integration tests**: Full event dispatch cycle (event → on_event → actions → execution) with mock LLM. Concurrency control: concurrent `on_event()` and `on_tick()` calls serialize via `asyncio.Lock`. Tick loop start/stop lifecycle including graceful shutdown (verify in-flight actions complete). Tick loop idle detection and wake-up on new event. Memory lifecycle: `initialize()` at startup, `close()` at shutdown with in-flight operation completion. Memory persistence across agent restarts (SQLite round-trip including PersonaState). `auto_reflect_after` counter persistence across restarts. Persona agent handling task via backward-compatible `handle()` path. `auto_reflect_after` nudge injection after N interactions. Agent-initiated note-taking round-trip (store → recall → delete → verify removal). `AgentEvent.metadata` cascade depth propagation through event dispatch. Multi-turn tool-use loop in `_LLMPersonaAgent.on_event()`. Memory write failure resilience (agent continues processing on SQLite error). Cross-agent memory isolation: verify agent A cannot retrieve agent B's episodes, notes, or relationships through the shared SQLite database — validates `agent_id` scoping and closure-based tool factory injection (security-critical).
- **E2E / smoke tests**: Submit a workflow that routes to a persona agent configured in `agents.yaml`, verify completion. Not gated on this RFC — existing e2e tests cover task agent path.
- **Manual tests**: Start a persona agent with `autonomy.level: semi-autonomous`, verify tick loop runs and logs actions. Verify `make validate` passes with updated configs.

## Open Questions

### Q1: Shared vs per-agent SQLite databases?

**Decision: Single shared database with agent_id partitioning (WAL mode).**

Per-agent databases simplify concurrent access and cleanup. A single shared database simplifies deployment and cross-agent queries (e.g., "who does agent X trust most?").

*Analysis:*

| Factor | Per-Agent DB | Shared DB |
|--------|-------------|-----------|
| Concurrency | No contention by default | WAL mode handles concurrent reads; writes serialized per-connection |
| Cross-agent queries | Requires opening multiple DBs, complex joins impossible | Natural — `SELECT * FROM relationships WHERE other_agent_id = ?` |
| Deployment | N files to manage, backup, migrate | Single file |
| Cleanup | Delete one file per agent | `DELETE WHERE agent_id = ?` |
| Schema migration | Must migrate N databases | Migrate once |
| Agent processes | v0.1 runs one agent per process; shared DB needs `aiosqlite` per-connection | Same |

Relationship memory is inherently cross-agent (trust between agent pairs), making per-agent DBs awkward — trust records would need to live in one agent's DB or be duplicated. A shared DB with `agent_id` column in every table provides logical isolation with physical simplicity.

WAL mode (`PRAGMA journal_mode=WAL`) supports concurrent readers with a single writer, which matches our access pattern (agents mostly read memories, writes happen after interactions). The MVP runs agents in a single Python process, so contention is minimal.

*Implementation detail:* Default path `data/memory.db`, configurable via `memory.db_path` in agent config. One `aiosqlite` connection per agent instance, all pointing to the same file. Connection pooling is not needed for MVP — `aiosqlite` wraps a single `sqlite3` connection in a background thread, and WAL mode allows concurrent readers. If contention becomes observable (measurable via the `memory_episode_store_duration_ms` metric), a connection pool can be introduced later.

### Q2: Token estimation accuracy

**Decision: chars/4 for MVP, with tiktoken as an optional accuracy upgrade.**

*Analysis:*

The working memory's `compress_if_needed()` should trigger well before hitting the actual context limit — a 20% safety margin means we compress at ~80K tokens for a 100K window. At this headroom, chars/4 (which is ~85% accurate for English text with code) is sufficient to prevent overflow.

| Approach | Accuracy | Latency | Dependencies |
|----------|----------|---------|-------------|
| chars/4 | ~85% | <1μs | None |
| tiktoken | ~99% | ~1ms per call | `tiktoken` (~2MB, model-specific encodings) |

The cost of over-estimating tokens is unnecessary compression (minor quality loss). The cost of under-estimating is context overflow (LLM error). chars/4 slightly over-estimates for code (more ASCII), which errs on the safe side.

**Known limitations**: For heavily structured content (JSON, YAML, code with long variable names), chars/4 can under-count by 30-40% because tokenizers split on punctuation and capitalization boundaries. For CJK text, chars/4 drastically over-counts. The 20% safety margin handles typical English/code workloads. Agents working with multilingual content should use the `accurate=True` path or risk context overflow.

*Implementation:*

```python
def estimate_tokens(text: str, *, accurate: bool = False) -> int:
    if accurate:
        try:
            import tiktoken
            # tiktoken only knows OpenAI model names; use cl100k_base as a
            # reasonable cross-model approximation. For precise Anthropic
            # counting, use the `anthropic` SDK's token counting API instead.
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except ImportError:
            pass
    return len(text) // 4
```

Add `tiktoken` as an optional dependency in `pyproject.toml` (`pip install orchestr8-agents[tiktoken]`). The `accurate` flag defaults to `False`; callers opt in when precision matters (e.g., billing estimates).

### Q3: How should old CoderAgent/ReviewerAgent/PlannerAgent imports be handled?

**Decision: (b) Clean break — update all imports to TaskAgent, remove old files.**

*Analysis:*

The three classes are structurally identical: each defines a system prompt string and calls `_run_llm_loop()`. Only one test file ([tests/unit/python/test_agents.py](../../tests/unit/python/test_agents.py)) imports them directly. There are no external consumers.

| Option | Effort | Maintenance Debt | Clean |
|--------|--------|-----------------|-------|
| (a) Thin wrappers subclassing TaskAgent | Low | 3 extra files that serve no purpose | No |
| (b) Remove files, update imports | Medium | Zero | Yes |
| (c) Re-export aliases | Low | Confusing for new contributors | No |

Option (a) and (c) preserve backward compatibility for zero external consumers — pure maintenance debt. Option (b) is a clean break that aligns with the RFC's goal of consolidation.

*Migration plan (Phase 1):*
1. Create `TaskAgent` in `agents/task_agent.py`
2. Move system prompts from `CoderAgent`/`ReviewerAgent`/`PlannerAgent` to `config/agents.yaml` as `instructions` fields
3. Update `test_agents.py`: replace class-specific tests with parametrized `TaskAgent` tests that vary `agent_id` + `instructions`
4. Update `server.py`: replace `_resolve_agent_type()` capability heuristic with `type` field dispatch
5. Delete `agents/coder.py`, `agents/reviewer.py`, `agents/planner_agent.py`
6. Update `agents/__init__.py` exports

The `TestCrossAgent` parameterized tests in `test_agents.py` already validate shared behavior — these naturally map to `TaskAgent` parametrization.

### Q4: Should tick loop actions trigger new events?

**Decision: Yes, with cascade depth limiting (max 5) and per-tick token budget.**

*Analysis:*

Multi-agent interaction is the core value proposition of v0.2. If Agent A's tick sends a message to Agent B, Agent B must process it via `on_event()` — otherwise persona agents can't converse. But unbounded event cascading creates two risks:

1. **Infinite loops**: Agent A messages Agent B, B responds to A, A responds to B, ...
2. **Runaway costs**: Each cascade triggers LLM calls, consuming tokens and money

*Safeguards (defense in depth):*

| Layer | Mechanism | Default |
|-------|-----------|---------|
| Cascade depth | `EventDispatcher` tracks `cascade_depth` in event metadata; rejects events beyond limit | max 5 |
| Per-tick token budget | Sum of tokens consumed by all LLM calls originating from one tick; stop when exceeded | From `optimization.yaml` agent budget |
| Per-tick action limit | `max_actions_per_tick` already caps actions per cycle | 3 |
| Idle detection | `idle_after_ticks` reduces tick frequency when nothing happens | 10 ticks |

*Implementation:*

The `EventDispatcher.dispatch()` method adds `cascade_depth` to the event metadata. When dispatching a tick-originated action that produces a new event, it increments the depth. If `depth >= max_cascade_depth`, the event is logged and dropped (not silently — the originating agent receives a `DO_NOTHING` with reason `"cascade_limit_reached"`).

```python
async def dispatch(self, agent_id: str, event: AgentEvent) -> list[AgentAction]:
    depth = event.metadata.get("cascade_depth", 0)
    if depth >= self._max_cascade_depth:
        logger.warning("Cascade limit reached", agent_id=agent_id, depth=depth)
        return []
    # ... normal dispatch, child events get cascade_depth=depth+1
```

### Q5: Memory retrieval relevance scoring

**Decision: recency × importance for MVP, plus SQLite FTS5 full-text search as a low-cost enhancement.**

*Analysis:*

| Approach | Relevance Quality | Latency | Dependencies | Effort |
|----------|------------------|---------|-------------|--------|
| recency × importance only | Low — ignores content | <1ms | None | Trivial |
| + SQLite FTS5 | Medium — keyword matching | <5ms | Built into SQLite | Low |
| + vector embeddings | High — semantic | ~50ms + API call | External embedding service | High |

Vector embeddings are explicitly a non-goal for MVP. However, pure recency × importance returns memories in chronological order regardless of content relevance — an agent asking "what happened in the billing discussion?" gets the most recent memories, not billing-related ones.

SQLite FTS5 is a **built-in** SQLite extension (available in Python's `sqlite3` module by default) that provides full-text search with BM25 ranking. It adds meaningful content relevance at near-zero cost:

```sql
-- Create virtual table alongside the regular table
CREATE VIRTUAL TABLE episodes_fts USING fts5(summary, context_json, content=episodes, content_rowid=rowid);

-- Query with BM25 ranking
SELECT e.*, fts.rank
FROM episodes_fts fts
JOIN episodes e ON e.rowid = fts.rowid
WHERE episodes_fts MATCH ?
ORDER BY (fts.rank * -1) * e.importance * (1.0 / (1 + (unixepoch('now') - e.created_at) / 86400.0))
LIMIT ?;
```

*Composite scoring formula:*

$$\text{score} = \text{BM25}(query, episode) \times \text{importance} \times (1 + \ln(1 + \text{access\_count})) \times \frac{1}{1 + \text{age\_days}}$$

*Implementation note:* SQLite FTS5 `rank` values are **negative** (lower = more relevant match). The SQL uses `fts.rank * -1` to convert to a positive score before combining with other factors. Implementers should preserve this negation — omitting it inverts the relevance ordering.

The `access_count` factor gives a mild boost to frequently retrieved memories (logarithmic to avoid runaway dominance). Each `recall()` hit increments `access_count` and updates `last_accessed_at` on returned episodes.

When no query text is provided (e.g., tick loop context loading), fall back to `importance × (1 + ln(1 + access_count)) × recency` only.

*FTS5 content-sync note:* FTS5 tables created with `content=episodes` do **not** auto-sync with the base table. **Decision: Use triggers.** Triggers are more reliable than periodic rebuilds because they keep the FTS5 index in sync transactionally — there is no window of stale search results. The same approach applies to the `notes_fts` table.

```sql
-- Triggers to keep episodes_fts in sync with episodes table
CREATE TRIGGER episodes_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts(rowid, summary, context_json) VALUES (new.rowid, new.summary, new.context_json);
END;
CREATE TRIGGER episodes_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, summary, context_json) VALUES('delete', old.rowid, old.summary, old.context_json);
END;
CREATE TRIGGER episodes_au AFTER UPDATE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, summary, context_json) VALUES('delete', old.rowid, old.summary, old.context_json);
    INSERT INTO episodes_fts(rowid, summary, context_json) VALUES (new.rowid, new.summary, new.context_json);
END;
```

The same trigger pattern is applied to `notes_fts` for the notes table.

*Implementation:* Add FTS5 table creation to `EpisodicMemory.initialize()` and `RelationshipMemory.initialize()`. The `recall()` method uses FTS5 when a query string is provided, falls back to recency × importance when not. Every `recall()` call updates `access_count` and `last_accessed_at` for returned rows.

See [Future Enhancement: Semantic Search via Vector Embeddings](#future-enhancement-semantic-search-via-vector-embeddings) for the full upgrade path from FTS5 keyword matching to vector-based semantic retrieval.

*Future enhancement — Ebbinghaus forgetting curve:* The `access_count` and `last_accessed_at` columns capture the data needed to implement retrieval-strengthened exponential decay ($R = e^{-t/S}$ where strength $S$ grows with each retrieval). This is a post-MVP upgrade path — swap the hyperbolic `1/(1 + age_days)` decay for exponential decay where the time constant is a function of `access_count`. Requires empirical tuning of decay rate and strengthening factor against real agent interaction data, so it is deferred until the system is running and producing observable memory access patterns.

### Q6: Should agent-initiated note-taking use a dedicated "learning" behavioral dimension?

**Decision: No — use existing dimensions + tool availability + config knobs instead.**

*Analysis:*

A dedicated `learning_style` dimension (e.g., `passive | reflective | active`) would directly control how much an agent self-reflects and takes notes. However:

| Factor | Dedicated dimension | Existing dimensions + tool config |
|--------|--------------------|------------------------------------|
| LLM differentiability | Low — LLMs struggle to meaningfully vary "learning rate" behavior | High — `detail_focus` and `risk_tolerance` already influence note-taking naturally |
| Configurability | Soft only (prompt nudge) | Hard + soft: `memory.notes.enabled` gates capability, `auto_reflect_after` controls frequency, `max_notes` caps storage |
| Personality consistency | Awkward — "learning ability" is not a personality trait | Natural — detail-focused cautious agents take more notes than big-picture bold agents |
| Dimensionality cost | 6th dimension, 729 profiles (3^6) — complexity for marginal return | Zero additional dimensions |

The combination of:
- `detail_focus: detail-focused` → agent naturally flags more things worth noting
- `risk_tolerance: cautious` → agent wants to record decisions for future reference
- `memory.notes.enabled: true` → hard gate on capability
- `memory.notes.auto_reflect_after: 5` → configurable reflection frequency
- Tool permission `memory:write` → deny-by-default access control

...provides more precise, testable, and reliable control over learning behavior than a single behavioral enum could.

---

## Future Considerations

The following ideas were raised during RFC review. They are out of scope for this RFC but worth capturing for future work. Each builds naturally on the infrastructure this RFC introduces.

### Memory Snapshots for Session Replay

The Non-Goals mention "observer mode & session replay" as a future feature. Adding a `snapshot()` method to `PersonaState` and `WorkingMemory` that serializes full state at a point in time would be zero-cost preparation. Store snapshots alongside episodes for faithful reconstruction of what an agent "saw" and "felt" at decision points. This enables session replay without requiring the full observer-mode infrastructure.

### Mood Transition Rules

The `Mood` enum has 6 values but no rules governing transitions. A transition matrix (e.g., `SATISFIED` can't jump directly to `FRUSTRATED` without passing through `NEUTRAL`) would make mood changes more natural and prevent whiplash behavior. Even a soft constraint (logged warning on "surprising" transitions) would help. Consider this when mood is actively used in production and behavioral patterns are observable.

### Relationship Reciprocity Asymmetry

The relationship model stores `(agent_id, other_agent_id)` → trust as a directed graph, but provides no mechanism for detecting or reasoning about asymmetry. If Agent A trusts Agent B at 0.9 but Agent B trusts Agent A at 0.3, this is a meaningful social signal. A `get_trust_asymmetry(other_id) → float` method on `RelationshipMemory`, with asymmetry injected into the relationship summary for the LLM prompt, would enable interesting social dynamics without adding complexity.

### Decision Audit Trail

Episodic memory stores conversation summaries, but not the reasoning chain that led to a decision. For debugging agent behavior, operators need to answer "why did the agent choose to delegate instead of act?" A lightweight `decision_log` (either a column on `episodes` or a separate table) storing the triggering event, compressed working memory context, LLM response, and chosen action outcome would provide audit capability without full session replay cost.

### Personality Drift Detection

With 243 personality profiles, an interesting failure mode is when an LLM's outputs drift away from configured personality dimensions over long conversations. A lightweight post-hoc check — after every N interactions, compare recent agent outputs against the expected behavioral profile using a cheap classifier LLM call — could detect drift and optionally re-inject a stronger persona reminder. Log drift as a metric for observability.

### Working Memory Pinning

The working memory compression algorithm evicts lowest-priority sections first, but sometimes an agent encounters mid-conversation information that's critical to retain despite low structural priority. A `pin(section_name)` mechanism would let the agent (or framework) mark specific context sections as non-compressible for the duration of a task.

### Agent Memory Export/Import

For debugging, testing, and "personality transfer," agents should be able to export their full memory state (episodes, notes, relationships, persona state) to a JSON file and import it back. Use cases: snapshot before destructive experiments, clone a veteran agent's knowledge to bootstrap a new agent, share memories across environments (dev → staging), unit test with realistic memory state. CLI: `orch agent export-memory <id> --output agent-memory.json` and `orch agent import-memory <id> --input agent-memory.json`.

### Conversation Threading in Episodic Memory

Episodes are currently flat — each interaction produces one row. Multi-turn conversations spanning 10+ exchanges produce 10+ disconnected episodes. Adding a `thread_id` (or `conversation_id`) column would let `recall()` retrieve entire conversation arcs, not isolated moments. `recall()` could optionally expand results: "give me this episode and all episodes from the same thread."

### Trust Decay as a Server-Level Scheduled Task

Trust decay currently depends on someone calling `apply_decay()` — presumably during `on_tick()`. But passive agents don't have ticks, and agents that are offline accumulate no decay. A server-level scheduled task (e.g., daily) that iterates all agent pairs and applies decay would ensure consistent trust evolution regardless of agent activity.

### Working Memory Compression History

When working memory is compressed, the original content is lost. Keeping a log of compressed sections (original token count, compressed token count, timestamp, summary text) enables post-hoc analysis ("why did the agent forget about X?") and potential re-expansion from episodic memory when the compressed topic becomes relevant again.

### Rate Limiting on Memory Tools

Beyond `max_notes`, limit the rate at which agents call memory tools per tick/event cycle. An LLM in a note-taking spiral could burn through budget. A `max_writes_per_cycle` config (e.g., 3 `store_note` + `update_note` calls per `on_event`/`on_tick` cycle) is a cheap safeguard against pathological tool-use loops.

### Relationship-Type-Aware Trust Dynamics

`update_trust()` takes a flat delta, but trust dynamics should differ by relationship type. Managers might have higher expectations (negative delta multiplied by 1.5x), external agents might be harder to trust (positive delta at 0.5x), and subordinates might be more forgiving (negative delta at 0.5x). These multipliers would be internal to `update_trust()` based on the stored relationship type, creating more realistic organizational dynamics without API complexity.

### Cross-Agent Knowledge Discovery

While agents can't read each other's memories (proper isolation), the orchestrator could offer a knowledge routing query: given a topic, return which agents have relevant episodes (relevance score + episode count, no content exposed). The requesting agent can then decide to delegate to the knowledgeable agent. This enables organic knowledge routing without breaking memory isolation.

### Memory Importance Auto-Scoring

Episodes currently receive a static `importance: float = 0.5` default. Auto-scoring based on event type weight (`TASK_ASSIGNED` > `MENTION` > `TICK`), action count, trust delta magnitude, and agent energy at time of interaction would improve retrieval quality without agent involvement.

### Agent "Forgetting" as Explicit Action

The RFC has `delete_note` for agent-initiated knowledge removal, but no equivalent for episodic memory. An `archive_episode` tool that marks episodes as low-importance rather than deleting them would reduce retrieval noise while preserving auditability — useful when an agent realizes a past interaction was based on wrong assumptions.

### Behavioral Dimension Compatibility Matrix

With 243 possible personality profiles, some combinations may produce contradictory LLM behavior (e.g., `directness: direct` + `expressiveness: reserved` creates "blunt but emotionless" prompts). A compatibility matrix or set of "known good" profiles would help config authors. Could also be implemented as a validation warning rather than an error.

---

## Decision / Next Steps

All 6 open questions are now resolved. Remaining steps before implementation:

1. Review and accept the RFC (status → 👍 Accepted)
2. Confirm the 6-phase implementation plan and PR ordering
3. Create `0005-pr-plan.md` with detailed PR breakdown

## Related Documentation

- [orchestr8-extension-spec.md](../orchestr8-extension-spec.md) — §E2 Persona Model, §E3 Autonomy Levels, §E7 Memory & Shared Knowledge
- [0004-python-agent-grpc-server.md](0004-python-agent-grpc-server.md) — Items Deferred to v0.2 (38 items, this RFC addresses ~15)
- [ai-agents-orchestration-spec.md](../ai-agents-orchestration-spec.md) — §2.1–2.3 Agent Architecture
- [ROADMAP.md](../../ROADMAP.md) — v0.2 planned components
