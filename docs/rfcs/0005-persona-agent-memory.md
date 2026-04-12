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
  - [Dynamic Persona State](#dynamic-persona-state)
  - [Data-Driven TaskAgent Consolidation](#data-driven-taskagent-consolidation)
  - [Config Schema Updates](#config-schema-updates)
  - [CLI Command Wiring](#cli-command-wiring)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

This RFC implements the PersonaAgent runtime and three-tier memory system as the foundational v0.2 feature. It activates the existing `PersonaAgent` class scaffold (currently abstract-only) with: (1) an autonomous tick loop for goal-driven agents, (2) an event dispatch framework that routes `AgentEvent`s to persona agents, (3) three-tier memory (working, episodic, relationship), and (4) dynamic persona state injection. As a bundled improvement, it consolidates the three structurally identical v0.1 task agents into a single data-driven `TaskAgent` class with YAML-configured instructions.

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
6. **Dynamic persona state**: Runtime mood, stress, goal progress, and relationship deltas injected into agent system prompts.
7. **Agent type discrimination**: Explicit `type: task | persona` field in agent config, replacing capability heuristics.
8. **Data-driven TaskAgent**: Single `TaskAgent` class driven by YAML `instructions` field, replacing `CoderAgent` / `ReviewerAgent` / `PlannerAgent`.
9. **CLI command wiring**: Wire Rust CLI stubs to existing and new REST endpoints as each phase lands — `run`, `status`, `agent list/info`, `validate`, `test --persona`.

## Non-Goals

- **Channels & message routing** — separate RFC (depends on this one)
- **Sub-agent spawning implementation** — separate RFC; this RFC wires the `OrchestratorClient` protocol but does not implement `SubAgentSpawner`
- **External bridges** (Slack, Discord, email) — separate RFC
- **Communication protocols** (standup, debate, consensus) — separate RFC
- **Organizational topologies** — separate RFC
- **Distributed state / agent migration** — v0.3 scope
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
      background: "..."
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

For agents with `autonomy.level` in (`semi-autonomous`, `autonomous`, `supervisor`), the server runs an async tick loop:

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
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
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
                if self._idle_count >= self._idle_limit:
                    continue  # stay idle but keep loop alive
            else:
                self._idle_count = 0
                await self._executor.execute(
                    self._agent, meaningful[:self._max_actions]
                )
```

### Event Dispatch Framework

Events flow through a central dispatcher that routes to the appropriate persona agent:

```python
class EventDispatcher:
    """Routes AgentEvents to persona agents and executes resulting actions."""

    def __init__(
        self,
        agents: dict[str, PersonaAgent],
        executor: ActionExecutor,
    ):
        self._agents = agents
        self._executor = executor

    async def dispatch(self, agent_id: str, event: AgentEvent) -> list[AgentAction]:
        agent = self._agents.get(agent_id)
        if agent is None:
            raise ValueError(f"Unknown persona agent: {agent_id}")

        # Inject memory context before event handling
        await self._inject_memory_context(agent, event)

        actions = await agent.on_event(event)
        await self._executor.execute(agent, actions)
        return actions

    async def _inject_memory_context(
        self, agent: PersonaAgent, event: AgentEvent
    ) -> None:
        """Load relevant memories and inject into agent context."""
        # Working memory: recent conversation context
        # Episodic memory: relevant past experiences
        # Relationship memory: trust/interaction history with sender
        ...
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

All three tiers implement a common `MemoryStore` protocol:

```python
@runtime_checkable
class MemoryStore(Protocol):
    """Protocol for memory tier backends."""

    async def store(self, entry: MemoryEntry) -> str:
        """Store a memory entry, return its ID."""
        ...

    async def retrieve(
        self, query: str, *, limit: int = 10
    ) -> list[MemoryEntry]:
        """Retrieve relevant memories for a query."""
        ...

    async def close(self) -> None:
        """Clean up resources."""
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
        """Summarize lowest-priority compressible sections when over budget."""
        # Note: Compression triggers an LLM call for summarization (1-10s
        # latency). This should be run as an async background task so that
        # the current event/tick can proceed with pre-compression context.
        # Synchronous compression would block event handling unacceptably.
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

### Episodic Memory (Long-Term Storage)

SQLite-backed storage for conversation summaries and decisions:

```python
class EpisodicMemory:
    """Long-term memory store using SQLite."""

    def __init__(self, agent_id: str, db_path: str = "data/memory.db"):
        self._agent_id = agent_id
        self._db_path = db_path

    async def initialize(self) -> None:
        """Create tables if they don't exist."""
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
        """Compress old episodes via LLM summarization. Returns count processed."""
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
        """Decay all trust scores toward 0.5 (neutral)."""
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

### Dynamic Persona State

Runtime state injected into the system prompt before each LLM call:

```python
@dataclass
class PersonaState:
    """Mutable runtime state for a persona agent."""
    mood: str = "neutral"
    stress_level: float = 0.0           # 0.0 - 1.0
    recent_context: list[str] = field(default_factory=list)
    goal_progress: dict[str, float] = field(default_factory=dict)

    def to_prompt_section(self) -> str:
        """Format state for injection into system prompt."""
        lines = [f"Current mood: {self.mood}"]
        if self.stress_level > 0.3:
            lines.append(f"Stress level: {self.stress_level:.1f}/1.0")
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

### Data-Driven TaskAgent Consolidation

Replace `CoderAgent`, `ReviewerAgent`, `PlannerAgent` with a single class:

```python
class TaskAgent(BaseAgent):
    """Data-driven task agent. Behavior configured via YAML instructions field."""

    async def handle(self, task: TaskInput) -> TaskOutput:
        instructions = self.config.get("instructions", "")
        return await self._run_llm_loop(task, system_prompt=instructions)
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
- `persona` object (required for `type: persona`)
- `autonomy` object with `level`, `tick_interval_seconds`, `max_actions_per_tick`, `idle_after_ticks`
- `relationships` array
- `memory` configuration object

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
- Persona state fields have type validation (mood is a string, stress_level is clamped 0.0-1.0)
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

### Phase 3: Episodic Memory (SQLite)

**Summary**: Implement long-term episode storage with relevance-based retrieval.

**Deliverables**:
1. SQLite schema and migration
2. `EpisodicMemory` class with store/recall/summarize
3. Auto-summarization of old episodes (LLM-based compression)
4. Integration with persona event handling (store episode after each interaction)
5. Unit tests for CRUD, retrieval ranking, compression

**Dependencies**: Phase 2 (uses token estimation for compression)

### Phase 4: Relationship Memory

**Summary**: Implement per-agent-pair trust tracking and interaction history.

**Deliverables**:
1. SQLite schema (shares database with episodic memory)
2. `RelationshipMemory` class with trust CRUD, decay, interaction logging
3. Relationship summary generation for LLM prompt injection
4. Integration with persona state (trust deltas update relationship memory)
5. Unit tests for trust math, decay, interaction recording

**Dependencies**: Phase 3 (shares SQLite infrastructure)

### Phase 5: PersonaAgent Runtime + Event Dispatch

**Summary**: Wire the autonomous tick loop, event dispatcher, and action executor into the gRPC server.

**Deliverables**:
1. `ActionExecutor` class
2. `EventDispatcher` class
3. `TickScheduler` class for autonomous agents
4. `PersonaState` dataclass and prompt injection
5. Wire into `server.py`: start tick loops for persona agents, route events
6. Create a sample persona agent config for integration testing
7. Integration tests: event dispatch → on_event → action execution, tick loop lifecycle
8. Wire CLI: `orch test --persona <id>` for persona consistency checks

**Dependencies**: Phases 1-4

### Phase 6: Config Validation + Schema Updates

**Summary**: Implement the `validate.py` config validator with JSON Schema support.

**Deliverables**:
1. Implement `validate_config_dir()` using `jsonschema` library
2. Update `schemas/agent.schema.json` for persona fields
3. Add `make validate` target that actually works
4. Unit tests for validation pass/fail cases
5. Wire CLI: `orch validate <path>` — invoke Python validator or implement JSON Schema validation in Rust

**Dependencies**: Phase 1 (schema changes)

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents | `agents/base.py` | Add working memory integration to `_run_llm_loop()` |
| Python agents | `agents/task_agent.py` | **New** — data-driven TaskAgent class |
| Python agents | `agents/persona.py` | Add `PersonaState`, memory integration, `ActionExecutor` |
| Python agents | `agents/memory/working.py` | **Implement** — `WorkingMemory`, `ContextSection`, token estimation |
| Python agents | `agents/memory/episodic.py` | **Implement** — `EpisodicMemory`, SQLite schema |
| Python agents | `agents/memory/relationship.py` | **Implement** — `RelationshipMemory`, trust math |
| Python agents | `agents/memory/__init__.py` | Export memory classes |
| Python agents | `agents/server.py` | Update agent loader, add tick scheduler lifecycle |
| Python agents | `agents/coder.py` | **Remove** (instructions move to YAML) |
| Python agents | `agents/reviewer.py` | **Remove** (instructions move to YAML) |
| Python agents | `agents/planner_agent.py` | **Remove** (instructions move to YAML) |
| Python agents | `agents/validate.py` | **Implement** — JSON Schema config validation |
| Config | `config/agents.yaml` | Add `type`, `instructions` fields; add sample persona agent |
| Config | `schemas/agent.schema.json` | Add persona, autonomy, memory, type fields |
| Tests | `tests/unit/python/test_task_agent.py` | **New** — TaskAgent tests |
| Tests | `tests/unit/python/test_working_memory.py` | **New** — working memory tests |
| Tests | `tests/unit/python/test_episodic_memory.py` | **New** — episodic memory tests |
| Tests | `tests/unit/python/test_relationship_memory.py` | **New** — relationship memory tests |
| Tests | `tests/unit/python/test_persona_runtime.py` | **New** — event dispatch, tick loop, action executor tests |
| Tests | `tests/unit/python/test_validate.py` | **New** — config validation tests |
| Tests | `tests/integration/test_persona_e2e.py` | **New** — persona agent end-to-end with mock LLM |
| Rust CLI | `cli/src/main.rs` | Wire `run`, `status`, `agent list/info`, `logs`, `validate`, `test --persona` |
| Rust CLI | `cli/Cargo.toml` | Add `reqwest`, `serde_json`, `serde` dependencies |
| Python agents | `agents/pyproject.toml` | Add `aiosqlite` dependency (required by Q1 shared SQLite decision) |

## Test Strategy

- **Unit tests**: Each memory tier tested independently with in-memory SQLite. Token estimation accuracy. Trust math (clamping, decay). PersonaState serialization. TaskAgent YAML-driven behavior. Config validation pass/fail cases.
- **Integration tests**: Full event dispatch cycle (event → on_event → actions → execution) with mock LLM. Tick loop start/stop lifecycle. Memory persistence across agent restarts (SQLite round-trip). Persona agent handling task via backward-compatible `handle()` path.
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

*Implementation detail:* Default path `data/memory.db`, configurable via `memory.db_path` in agent config. One `aiosqlite` connection per agent instance, all pointing to the same file.

### Q2: Token estimation accuracy

**Decision: chars/4 for MVP, with tiktoken as an optional accuracy upgrade.**

*Analysis:*

The working memory's `compress_if_needed()` should trigger well before hitting the actual context limit — a 20% safety margin means we compress at ~80K tokens for a 100K window. At this headroom, chars/4 (which is ~85% accurate for English text with code) is sufficient to prevent overflow.

| Approach | Accuracy | Latency | Dependencies |
|----------|----------|---------|-------------|
| chars/4 | ~85% | <1μs | None |
| tiktoken | ~99% | ~1ms per call | `tiktoken` (~2MB, model-specific encodings) |

The cost of over-estimating tokens is unnecessary compression (minor quality loss). The cost of under-estimating is context overflow (LLM error). chars/4 slightly over-estimates for code (more ASCII), which errs on the safe side.

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

The `access_count` factor gives a mild boost to frequently retrieved memories (logarithmic to avoid runaway dominance). Each `recall()` hit increments `access_count` and updates `last_accessed_at` on returned episodes.

When no query text is provided (e.g., tick loop context loading), fall back to `importance × (1 + ln(1 + access_count)) × recency` only.

*FTS5 content-sync note:* FTS5 tables created with `content=episodes` do **not** auto-sync with the base table. Inserts, updates, and deletes in `episodes` must be mirrored to `episodes_fts` via triggers or explicit statements. Alternatively, a periodic `INSERT INTO episodes_fts(episodes_fts) VALUES('rebuild')` can be used for simplicity at the cost of stale results between rebuilds. The implementation must choose one strategy and test sync correctness.

*Implementation:* Add FTS5 table creation to `EpisodicMemory.initialize()` and `RelationshipMemory.initialize()`. The `recall()` method uses FTS5 when a query string is provided, falls back to recency × importance when not. Every `recall()` call updates `access_count` and `last_accessed_at` for returned rows.

See [Future Enhancement: Semantic Search via Vector Embeddings](#future-enhancement-semantic-search-via-vector-embeddings) for the full upgrade path from FTS5 keyword matching to vector-based semantic retrieval.

*Future enhancement — Ebbinghaus forgetting curve:* The `access_count` and `last_accessed_at` columns capture the data needed to implement retrieval-strengthened exponential decay ($R = e^{-t/S}$ where strength $S$ grows with each retrieval). This is a post-MVP upgrade path — swap the hyperbolic `1/(1 + age_days)` decay for exponential decay where the time constant is a function of `access_count`. Requires empirical tuning of decay rate and strengthening factor against real agent interaction data, so it is deferred until the system is running and producing observable memory access patterns.

---

## Decision / Next Steps

All 5 open questions are now resolved. Remaining steps before implementation:

1. Review and accept the RFC (status → 👍 Accepted)
2. Confirm the 6-phase implementation plan and PR ordering
3. Create `0005-pr-plan.md` with detailed PR breakdown

## Related Documentation

- [orchestr8-extension-spec.md](../orchestr8-extension-spec.md) — §E2 Persona Model, §E3 Autonomy Levels, §E7 Memory & Shared Knowledge
- [0004-python-agent-grpc-server.md](0004-python-agent-grpc-server.md) — Items Deferred to v0.2 (38 items, this RFC addresses ~15)
- [ai-agents-orchestration-spec.md](../ai-agents-orchestration-spec.md) — §2.1–2.3 Agent Architecture
- [ROADMAP.md](../../ROADMAP.md) — v0.2 planned components
