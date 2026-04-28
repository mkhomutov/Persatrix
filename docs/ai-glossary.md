# Persatrix AI Glossary

Canonical terminology for assistants and contributors writing or editing Persatrix
artifacts (code, docs, RFCs, PRs, reviews, plans). The terms below are the
default vocabulary; alternative phrasings should be avoided unless clarity
genuinely requires them.

This file is referenced by both `.github/CLAUDE.md` and
`.github/copilot-instructions.md`. Update it before introducing new terms.

---

## Authoring Rules

1. **Canonical first.** Use the canonical term on first mention and in headings;
   do not silently alternate with synonyms.
2. **Expand acronyms once.** First occurrence in a document spells out
   project-specific or non-standard acronyms (e.g. "directed acyclic graph
   (DAG)"); subsequent uses may be short. Industry-standard acronyms used
   throughout this glossary — LLM, REST, gRPC, YAML, MCP, JSON, SQL — do
   not need expansion.
3. **No duplicate synonyms.** Within one response, PR description, or doc
   section, pick one term and stick with it.
4. **Keep definitions brief.** Glossary entries are reference, not tutorials —
   one or two sentences plus an example.
5. **Update before introducing.** Adding a new project term to code or docs
   requires adding it here in the same change.

---

## Terms

### Persatrix
- **Aliases:** —
- **Disallowed:** "the framework" (when ambiguous), "Persatrix.io", "the platform"
- **Definition:** The polyglot AI agent orchestration framework: Go orchestrator,
  Python agent runtime, Rust CLI, connected via gRPC and REST.
- **Example:** "Persatrix v0.3.0 introduces internal channels."

### Orchestrator
- **Aliases:** "Go orchestrator" (when contrasting with agents)
- **Disallowed:** "server", "backend", "controller", "coordinator service"
- **Definition:** The Go service in `cmd/orchestrator/` that plans, schedules,
  executes, and tracks workflows. Holds no LLM logic.
- **Example:** "The orchestrator dispatches tasks to agents over gRPC."

### Agent
- **Aliases:** —
- **Disallowed:** "bot", "worker", "actor" (when meaning agent)
- **Definition:** A Python runtime entity that performs work via an LLM and
  tools. Either a task agent or a persona agent.
- **Example:** "Each agent is registered in the orchestrator's registry."

### Task Agent
- **Aliases:** —
- **Disallowed:** "stateless agent", "simple agent", "v0.1 agent"
- **Definition:** A stateless agent that receives a task and returns a result.
  No persona, relationships, or long-term memory.
- **Example:** "`code-writer` is a task agent invoked from a workflow step."

### Persona Agent
- **Aliases:** —
- **Disallowed:** "character agent", "persona bot", "smart agent"
- **Definition:** An agent extended with persona, relationships, autonomy, and
  memory. Identified by the presence of a `persona` block in `config/agents.yaml`.
- **Example:** "`ember-owl` is a persona agent with `semi-autonomous` autonomy."

### Sub-agent
- **Aliases:** —
- **Disallowed:** "child agent", "spawned agent", "sub_agent" (in prose)
- **Definition:** An ephemeral task agent spawned by a persona agent for atomic
  work. Inherits a restricted permission set.
- **Example:** "The persona spawns a sub-agent to summarize a long document."

### Workflow
- **Aliases:** —
- **Disallowed:** "pipeline", "job", "flow", "DAG" — all when meaning workflow.
  ("DAG" is fine for the *structure*; "pipeline" is fine when describing the
  planner→scheduler→executor orchestration pipeline as in RFC 0001 — just not
  as a synonym for a workflow definition.)
- **Definition:** A directed acyclic graph (DAG) of tasks defined in YAML under
  `workflows/`, supporting sequential, parallel, conditional, and looped steps.
  ("DAG" describes the structure of a workflow, not a synonym for the workflow itself.)
- **Example:** "The workflow fans out three review tasks before aggregation."

### Task
- **Aliases:** "step" (only inside a workflow definition)
- **Disallowed:** "job", "unit"
- **Definition:** A single unit of work assigned to an agent, with a status
  lifecycle: `pending → running → completed | failed | cancelled`.
- **Example:** "The task failed after exhausting `max_retries`."

### Tool
- **Aliases:** —
- **Disallowed:** "function", "skill" (see the **Skill** entry below — RFC 0014
  reserves "skill" for a higher-level concept), "plugin"
- **Definition:** A typed callable an agent can invoke, declared with the
  `@tool(name=..., permissions=[...])` decorator in `agents/tools/`.
- **Example:** "The `file_read` tool requires the `fs.read` permission."

### Skill
- **Aliases:** —
- **Disallowed:** "tool" (when meaning a skill); "capability" (when meaning a
  registered skill)
- **Definition:** A first-class agent capability above the atomic tool layer:
  reasoning patterns, domain knowledge, procedural workflows, and
  meta-cognitive behaviors. Versioned and tracked in the Skill Registry.
  Reserved term — see [RFC 0014](rfcs/0014-agent-skill-registry-lifecycle.md);
  📋 Proposed for v0.4.0, not yet implemented. Do not use "skill" to mean
  "tool" in v0.3.0 code or docs.
- **Example:** "RFC 0014 introduces the `code-review` skill, composed from the
  `file_read` and `lint` tools."

### Permission
- **Aliases:** —
- **Disallowed:** "scope", "ACL entry", "right"
- **Definition:** A whitelisted capability granted to an agent in
  `config/agents.yaml`. The model is deny-by-default.
- **Example:** "Add `net.http` to the agent's permissions to allow outbound HTTP."

### Channel
- **Aliases:** —
- **Disallowed:** "room", "chatroom", "topic", "queue"
- **Definition:** A named internal message bus where agents publish and receive
  messages (RFC 0011). v0.3.0 channels are internal only.
- **Example:** "Agents subscribed to the `#planning` channel receive the message."

### Channel Bridge
- **Aliases:** —
- **Disallowed:** "bridge" (when ambiguous with **MCP Bridge**), "connector",
  "adapter" (when meaning channel bridge), "integration"
- **Definition:** A v0.5.0 component that connects an internal channel to an
  external service (Slack, Discord, email, Telegram). Always qualified as
  "channel bridge" to disambiguate from **MCP Bridge** (a separate, existing
  concept — see below).
- **Example:** "The Slack channel bridge mirrors `#ops` into a Slack workspace."

### MCP Bridge
- **Aliases:** —
- **Disallowed:** "bridge" (unqualified, when meaning the MCP integration);
  "MCP connector"
- **Definition:** The Python component in
  [`agents/tools/mcp_bridge.py`](../agents/tools/mcp_bridge.py) that connects to
  external Model Context Protocol (MCP) servers and exposes their tools to
  agents over stdio or SSE. Distinct from **Channel Bridge** above. Configured
  via `config/mcp-servers.yaml`.
- **Example:** "The MCP bridge surfaces filesystem tools from a stdio MCP server."

### Message Bus
- **Aliases:** —
- **Disallowed:** "event bus", "broker", "pub/sub"
- **Definition:** The communication backbone for structured messages between
  the orchestrator and agents (request, response, event, delegation).
- **Example:** "Delegation messages travel over the message bus."

### Memory
- **Aliases:** —
- **Disallowed:** "store", "history", "context cache"
- **Definition:** Persona-agent state with three tiers: **episodic**
  (interaction events), **relationship** (per-agent trust and history), and
  **working** (in-context scratch memory). See RFC 0008.
- **Example:** "Conversation summaries are written to episodic memory via
  `EpisodicMemory.store_episode` in `agents/memory/episodic.py`."

### Episode
- **Aliases:** —
- **Disallowed:** "memory entry", "log entry", "history record"
- **Definition:** A persisted record of a past interaction, stored in the
  agent's SQLite episodic memory database (`agents/memory/episodic.py`) with
  summary, participants, importance, and timestamps. Today, one episode is
  written per event-handler invocation; [RFC 0020](rfcs/0020-interaction-lifecycle.md)
  (📋 Proposed for v0.3.0) refines this to one episode per closed interaction.
- **Example:** "The agent recalls three relevant episodes when a similar
  question recurs."

### Trust Level
- **Aliases:** "trust score"
- **Disallowed:** "rapport", "affinity", "reputation"
- **Definition:** A `[0.0, 1.0]` floating-point value on a relationship row in
  an agent's relationship-memory store, seeded from the `relationships` block
  in `config/agents.yaml` and updated as interactions accumulate. Persisted
  in `agents/memory/relationship.py`.
- **Example:** "`iron-fox` is seeded with `trust_level: 0.9` toward
  `ember-owl`."

### Persona
- **Aliases:** —
- **Disallowed:** "character", "profile", "identity block"
- **Definition:** The YAML block on a persona agent describing background,
  personality, goals, and knowledge.
- **Example:** "The persona's `goals.primary` field anchors long-running behavior."

### Autonomy Level
- **Aliases:** —
- **Disallowed:** "agency level", "freedom level"
- **Definition:** One of `passive`, `reactive`, `semi-autonomous`, `autonomous`,
  `supervisor`. Controls what the agent may initiate without approval.
- **Example:** "A `semi-autonomous` agent may delegate but needs approval for external comms."

### Registry
- **Aliases:** "agent registry"
- **Disallowed:** "directory", "catalog"
- **Definition:** The orchestrator component (`internal/registry/`) that loads
  and indexes agent definitions from `config/agents.yaml`.
- **Example:** "The registry rejects agents with duplicate IDs."

### Planner
- **Aliases:** —
- **Disallowed:** "decomposer", "task splitter"
- **Definition:** The orchestrator subsystem (`internal/planner/`) that
  decomposes a goal or workflow into a task graph.
- **Example:** "The planner expands template variables before scheduling."

### Scheduler
- **Aliases:** —
- **Disallowed:** "dispatcher", "queue manager"
- **Definition:** The orchestrator subsystem (`internal/scheduler/`) that
  orders task execution and enforces concurrency limits.
- **Example:** "The scheduler runs fan-out branches in parallel."

### Executor
- **Aliases:** —
- **Disallowed:** "runner", "task runner"
- **Definition:** The orchestrator subsystem (`internal/executor/`) that
  invokes agents over gRPC and applies resilience policies.
- **Example:** "The executor retries the task on transient gRPC errors."

### Cost Tracking
- **Aliases:** "cost"
- **Disallowed:** "billing", "metering" (when meaning cost tracking),
  "spend tracker"
- **Definition:** Per-task and per-agent token and dollar accounting under
  `internal/cost/`, with budgets defined in `config/optimization.yaml`.
- **Example:** "The cost tracker halts the workflow when the daily budget is exceeded."

### Telemetry
- **Aliases:** "observability" (when speaking broadly), "structured logging"
  (when scoped to RFC 0018 — the Structured Logging Framework)
- **Disallowed:** "metrics" (when meaning the full pipeline), "logging"
  (when meaning the full pipeline)
- **Definition:** Structured logs, metrics, and traces emitted via OpenTelemetry
  (RFC 0018, RFC 0019). See `docs/observability.md`.
- **Example:** "Channel delivery latency is exported via the telemetry pipeline."

### CLI
- **Aliases:** "Rust CLI", "`persatrix` CLI"
- **Disallowed:** "command-line tool" (in code/docs prose), "client"
- **Definition:** The Rust binary in `cli/` that wraps REST calls. Holds no
  business logic.
- **Example:** "`persatrix chat` opens a human chat session via the REST API."

### RFC
- **Aliases:** —
- **Disallowed:** "design doc" (use only for non-RFC design notes), "proposal"
- **Definition:** A numbered design document under `docs/rfcs/` following
  `RFC_TEMPLATE.md`. Status moves through `📋 Proposed → 🚧 In Progress → ✅ Implemented`.
- **Example:** "RFC 0011 specifies the channel model."

### PR Plan
- **Aliases:** —
- **Disallowed:** "PR breakdown", "implementation plan"
- **Definition:** A companion document `docs/rfcs/NNNN-pr-plan.md` that splits
  an RFC into PR-sized units with checklists.
- **Example:** "Mark the matching PR plan checkbox when the PR merges."

### Optimization Profile
- **Aliases:** —
- **Disallowed:** "model config", "routing config"
- **Definition:** A named entry in `config/optimization.yaml` defining model
  routing, caching policy, and budget caps.
- **Example:** "The `cheap-summary` profile routes to Haiku with aggressive caching."

### Agent ID
- **Aliases:** —
- **Disallowed:** "agent name" (when meaning the ID), "slug"
- **Definition:** A stable identifier matching `^[a-z0-9][a-z0-9-]*[a-z0-9]$`,
  unique across `config/agents.yaml`.
- **Example:** "Use the agent ID `ember-owl`, not the display name `Ember Owl`."

### Persona Nickname
- **Aliases:** —
- **Disallowed:** "persona ID" (when meaning the nickname), "human name"
- **Definition:** A nickname-style two-word identifier (e.g. `ember-owl`).
  Generated via `make generate-persona-nickname COUNT=5`.
- **Example:** "Persona agents must use nicknames, not human-like names."

### Claude Code
- **Aliases:** —
- **Disallowed:** "Claude" (when meaning the assistant), "Anthropic CLI"
- **Definition:** Anthropic's CLI assistant. Configured via `.github/CLAUDE.md`.
- **Example:** "Claude Code follows the response-style rules in CLAUDE.md."

### Copilot
- **Aliases:** "GitHub Copilot"
- **Disallowed:** "the assistant" (when ambiguous), "AI pair"
- **Definition:** GitHub's assistant. Configured via `.github/copilot-instructions.md`.
- **Example:** "Copilot reads the same project guidelines as Claude Code."

## RFC 0008 — Context Budget & Packaging

Terms introduced by [RFC 0008](rfcs/0008-agent-memory-context-optimization.md)
PR 1 (orchestrator-side context-budget allocator + per-step packaging
foundation).

### Context Budget Total
- **Aliases:** "workflow context budget"
- **Disallowed:** "context cap", "global token budget"
- **Definition:** Workflow-level token ceiling (`workflow.context_budget_total`
  in YAML; `WorkflowDefinition.ContextBudgetTotal` in Go) that opts the
  workflow into per-step context packaging. When `0` (the default), packaging
  is disabled and steps receive the raw outputs map verbatim (legacy
  passthrough). Required whenever any step sets `context_budget`.
- **Example:** "The workflow declares `context_budget_total: 6000`, so each
  step receives a `_context_package` payload."

### Context Package
- **Aliases:** —
- **Disallowed:** "context bundle", "prompt package"
- **Definition:** The per-step JSON payload (versioned, frozen at `version: 1`)
  attached to `TaskRequest.context` under the reserved key
  `_context_package`. Carries the admitted step outputs, pinned sections,
  budget allocation, and packager metrics. Produced by the
  `internal/executor/packaging.Packager`.
- **Example:** "The agent reads `_context_package.step_outputs` to hydrate
  prompt context."

### Packager
- **Aliases:** —
- **Disallowed:** "context builder", "context assembler"
- **Definition:** The `internal/executor/packaging.Packager` component that
  assembles a `Package` from candidates under a token budget using a greedy
  knapsack over `RelevanceScorer` density.
- **Example:** "The packager admits the highest-density candidates first."

### Relevance Scorer
- **Aliases:** —
- **Disallowed:** "ranker", "relevance ranker"
- **Definition:** The `RelevanceScorer` interface in
  `internal/executor/packaging` that returns a `[0.0, 1.0]` relevance score
  for a `Candidate` given a `QueryContext`. Default implementation is
  `HeuristicScorer` (importance + dependency proximity + token-overlap).
- **Example:** "A future embedding-backed relevance scorer will replace the
  heuristic default."

### Pinned Section
- **Aliases:** —
- **Disallowed:** "must-include section", "anchor section"
- **Definition:** A `Candidate` with `Pinned: true` that is admitted to the
  package even when its tokens exceed the remaining budget; over-budget
  pinned admission surfaces the `pinned_overflow` warning in
  `Package.Metrics.Warnings`.
- **Example:** "System-instruction sections are marked as pinned sections."

### Extractive Truncation
- **Aliases:** —
- **Disallowed:** "summarisation", "compression" (when meaning the v0.3
  Phase-1 mechanism)
- **Definition:** RFC 0008 Phase 1's truncation mode: candidates are admitted
  whole or dropped whole — no per-section summarisation. Compression is
  expressed as the ratio of total non-pinned tokens to admitted tokens.
- **Example:** "Phase 1 ships extractive truncation; semantic summarisation
  arrives in Phase 2."

### High Compression Ratio
- **Aliases:** —
- **Disallowed:** "high compression"
- **Definition:** A `Package.Metrics.CompressionRatio` at or above
  `HighCompressionRatioThreshold` (4.0). Surfaces the
  `high_compression_ratio` warning so operators can detect workloads that
  are dropping a large share of their candidates.
- **Example:** "The dispatch logged `high_compression_ratio` because 80% of
  outputs were dropped."

### Extreme Compression Cap
- **Aliases:** —
- **Disallowed:** "compression ceiling"
- **Definition:** The `ExtremeCompressionCap` constant (10.0) that caps
  `Package.Metrics.CompressionRatio` and doubles as the sentinel emitted
  with the `extreme_compression_capped` warning when all non-pinned
  candidates are dropped or the raw ratio would exceed the cap.
- **Example:** "When every non-pinned candidate is dropped the package
  emits the extreme compression cap."

## RFC 0008 — MemoryFacade (PR 2)

Terms introduced by [RFC 0008](rfcs/0008-agent-memory-context-optimization.md)
PR 2 (Python-side `MemoryFacade` for task agents).

### MemoryFacade
- **Aliases:** —
- **Disallowed:** "memory manager", "memory context" (when meaning the facade
  class)
- **Definition:** The `agents.memory.facade.MemoryFacade` class that provides
  a stable, tier-agnostic memory API for task agents (RFC 0008 §B). Wraps
  the underlying `EpisodicMemory` tier and exposes `retrieve_relevant`,
  `store_observation`, `store_procedure`, `list_candidates`, and `compress`.
  Lifecycle is per-process: one instance per task-agent process, shared
  across concurrent gRPC calls.
- **Example:** "The task agent calls `MemoryFacade.retrieve_relevant(query,
  limit=5)` to hydrate its LLM prompt with relevant past observations."

### MemoryEntry
- **Aliases:** —
- **Disallowed:** "memory record", "episode" (when meaning the facade
  projection)
- **Definition:** A frozen dataclass (`agents.memory.facade.MemoryEntry`)
  returned by `MemoryFacade.retrieve_relevant`. Tier-agnostic projection of
  an underlying episode with fields `id`, `content`, `importance`, `tags`,
  `created_at`, `score`, and `scope`. Callers must not depend on the
  underlying storage tier.
- **Example:** "Each `MemoryEntry.score` is the FTS5 relevance score from
  `EpisodicMemory.recall`, normalised to `[0, 1]`."

### CompressedView
- **Aliases:** —
- **Disallowed:** "compressed context", "memory summary" (when meaning the
  dataclass)
- **Definition:** A frozen dataclass (`agents.memory.facade.CompressedView`)
  returned by `MemoryFacade.compress`. Carries `summary` (extractive
  concatenation of admitted entries in Phase 2; abstractive in PR 5),
  `entries_dropped`, `tokens_before`, and `tokens_after`. Required by
  RFC 0020 PR 4's summarize-on-close path.
- **Example:** "RFC 0020's `InteractionTracker` calls `facade.compress(
  entries, target_tokens=1000)` and stores `CompressedView.summary` as the
  interaction summary."

### Candidate (facade)
- **Aliases:** —
- **Disallowed:** "packaging candidate" (when meaning the Python facade type;
  use "packaging candidate" only for the Go `executor/packaging.Candidate`)
- **Definition:** A frozen dataclass (`agents.memory.facade.Candidate`) for
  agent-side context-package admission (RFC 0008 §B, `list_candidates`).
  Phase 2 stub returns `[]`; populated in PR 5 when the agent-side
  candidate-listing API integrates with the orchestrator-side packaging
  pipeline.
- **Example:** "In PR 5 each `Candidate` will carry `importance` and `tokens`
  so the facade can rank by density before handing off to the packager."

### MemoryDisabledError
- **Aliases:** —
- **Disallowed:** "MemoryError" (standard Python built-in), "uninitialised
  error"
- **Definition:** `agents.memory.facade.MemoryDisabledError` — raised when a
  memory write operation is attempted on an uninitialised `MemoryFacade`.
  Subclasses `RuntimeError` for backward compatibility. When `memory.enabled:
  false` (the deny-by-default config), the facade is `None` and callers that
  attempt writes should surface `MemoryDisabledError` rather than silently
  no-op'ing.
- **Example:** "An integration test with `memory.enabled: false` asserts that
  `store_observation` raises `MemoryDisabledError`, confirming the
  misconfiguration is visible at startup."

## RFC 0008 — Eviction (PR 2a)

Terms introduced by [RFC 0008](rfcs/0008-agent-memory-context-optimization.md)
PR 2a (episodic-tier eviction follow-on to PR 2).

### EvictionPass
- **Aliases:** —
- **Disallowed:** "eviction job", "eviction sweep" (when meaning the class)
- **Definition:** The `agents.memory.eviction.EvictionPass` class that runs
  one TTL + size-cap eviction sweep over the agent's `episodes` rows
  (RFC 0008 §G). Stateless across runs — the
  `~agents.memory.facade.MemoryFacade` schedules one instance per cadence
  tick from its background loop. Returns an `EvictionStats` report.
- **Example:** "`EvictionPass(agent_id, episodic_cap=1000,
  ttl_low_importance_days=30).run(db)` returns the per-pass stats."

### EvictionStats
- **Aliases:** —
- **Disallowed:** "eviction result", "eviction report" (when meaning the
  dataclass)
- **Definition:** A frozen dataclass (`agents.memory.eviction.EvictionStats`)
  carrying `ttl_evicted`, `cap_evicted`, and `total_after`. Returned by
  `EvictionPass.run` so the caller can log / trace each pass.
- **Example:** "The eviction loop logs `stats.ttl_evicted` and
  `stats.cap_evicted` only when either is non-zero, to keep idle cadences
  quiet."


