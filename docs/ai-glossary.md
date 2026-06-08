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

### Disposition
- **Aliases:** "respond disposition", "respond policy" (legacy)
- **Disallowed:** "respond mode", "reply trigger"
- **Definition:** A channel member's **role in the conversation** — the `respond`
  field on a membership (RFC 0030 relevance amendment): `participant` (open
  floor), `addressed` (replies only when `@`-mentioned), `observer` (never
  replies), or `chair` (a low-threshold facilitator). It declares *how eager* a
  member is; the response gate makes the per-message call. Reframed in v0.3.7
  from the mechanical `respond_policy` trigger; the legacy `always` /
  `when_mentioned` / `never` values still load and normalize.
- **Example:** "Give the demo personas the `participant` disposition so they join
  the open floor."

### Salience Bid
- **Aliases:** "salience gate", "Tier B bid"
- **Disallowed:** "relevance gate" (that is the whole RFC 0030 Layer 3 surface;
  the bid is its Tier B), "salience wake" (the RFC 0024 *autonomy* trigger — a
  distinct concept)
- **Definition:** The cheap `fast`-model, leased call (v0.3.8, RFC 0030 Tier B) a
  `participant`/`chair` runs on an un-addressed open-floor message to decide
  *whether to speak*: "do I have something genuinely new to add that hasn't
  already been said?" It reads the in-round transcript and stays **silent**
  unless its score clears the member's **disposition threshold** — the
  no-pile-on mechanism. Bias-to-silence by default; fails closed to silence on a
  parse/lease error.
- **Example:** "On the open-floor question the salience bid kept three of the
  four participants silent — only the one with something new spoke."

### Disposition Threshold
- **Aliases:** "salience threshold", "the `threshold` field"
- **Disallowed:** "salience-wake threshold" (RFC 0024 autonomy), "min_score"
  (the recall relevance floor — a different gate)
- **Definition:** The per-member salience-score floor (`[0, 1]`) the **salience
  bid** must clear to speak (v0.3.8). **Unset → bias-to-silence** (only a decisive
  score speaks); a `chair` defaults to a low value so it clears readily. A
  threshold on a non-open-floor disposition is a config error.
- **Example:** "Lower the chair's disposition threshold so it facilitates more
  actively."

### Chair
- **Aliases:** "chair disposition", "facilitator"
- **Disallowed:** "moderator" (the v0.4.0 Layer 5 role that can *close* a
  conversation — the chair's deferred active half), "admin", "owner"
- **Definition:** A channel **disposition** (v0.3.8): a `participant` with a low
  **disposition threshold**, so it clears the **salience bid** readily and keeps a
  discussion moving. In v0.3.8 a chair is a *facilitator only* — it **cannot**
  close, wrap up, or terminate an interaction (that is the Layer 5 moderator,
  v0.4.0). Convergence comes from the governance layers, not the chair.
- **Example:** "Mark the lead persona as `chair` so it nudges the brainstorm
  along without dominating it."

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
  **working** (in-context scratch memory). See RFC 0008. The **working** tier
  here is the in-RAM bridged scratch tier (see also `Scratchpad (memory tier)`),
  *not* the live in-channel transcript the persona runtime feeds the LLM each
  turn — the latter is the `Conversation Window` and rides the LLM `messages`
  array, not a memory tier.
- **Example:** "Conversation summaries are written to episodic memory via
  `EpisodicMemory.store_episode` in `agents/memory/episodic.py`."

### Conversation Window
- **Aliases:** —
- **Disallowed:** "conversational working memory", "transcript window",
  "in-conversation memory", "in-progress conversation memory" (all when
  meaning the live `messages`-array transcript)
- **Definition:** The last N turns of the current channel reconstructed from
  the channel store on every persona turn and rendered into the LLM
  `messages` array (peer turns → `role="user"`, the persona's own turns →
  `role="assistant"`). Owned by `agents/persona_runtime/conversation_window.py`
  (introduced by [RFC 0034](rfcs/0034-persona-conversational-working-memory.md)).
  Distinct from the working-memory tier under `Memory`: the Conversation
  Window is not persisted as a tier and does not consume the system-prompt
  memory budget ([RFC 0017](rfcs/0017-persona-memory-injection-budget.md)).
- **Example:** "The Conversation Window holds the last 20 turns; episode
  summaries continue to ride the system prompt."

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

### Participant ID
- **Aliases:** —
- **Disallowed:** "user id" (when meaning a channel participant), "member id"
- **Definition:** The identifier of a channel participant — either an **Agent
  ID** or a non-agent participant such as a CLI user (`User_1`). Matches
  `^[A-Za-z0-9][A-Za-z0-9_-]*$` (RFC 0011 §A; enforced by the JSON Schema,
  the loader, and the channel store). The pattern is intentionally looser
  than the **Agent ID** pattern so non-agent participants can keep their
  upstream casing (e.g. CLI usernames). Reserved characters: `:` (canonical
  address separator) and any whitespace.
- **Example:** "The membership row carries both the agent's `Participant ID`
  `code-writer` and the human user's `Participant ID` `User_1`."

### Channel Type
- **Aliases:** —
- **Disallowed:** "channel kind", "channel category"
- **Definition:** The fixed vocabulary `group | dm | thread` used by the
  channel store (RFC 0011) to classify rows in the `channels` table:
  - **group** — user-declared, named, multi-participant; subject to the
    global `max_channels` cap.
  - **dm** — auto-created direct message between exactly two participants;
    canonical id is `dm:<a>:<b>` with `<a> < <b>` lexicographically.
  - **thread** — reply chain anchored to a parent message id; cascade-prunes
    when the parent is pruned.
- **Example:** "The Channel Type `dm` is exempt from the named-group cap."

### ChannelMessageEvent
- **Aliases:** —
- **Disallowed:** "channel event", "channel msg" (when meaning the wire type)
- **Definition:** The protobuf message defined in
  [`proto/task.proto`](../proto/task.proto) that carries a single channel
  publish from the orchestrator to one subscribed agent over the
  `AgentService.ReceiveChannelMessage` RPC (RFC 0011 §C). Field shape:
  `message_id`, `channel_id`, `channel_type` (`group | dm | thread`,
  duplicated from the `channel_id` prefix for log/metric ergonomics — the
  orchestrator MUST validate agreement on publish), `sender_id`, `content`,
  `timestamp` (RFC 3339), `thread_id` (empty string when not a reply),
  `mentions`. Introduced in v0.3.0 (PR #246).
- **Example:** "Each `ChannelMessageEvent` carries the `channel_type` so
  observability counters need not parse the `channel_id` prefix."

### TaskAck
- **Aliases:** —
- **Disallowed:** "ack", "delivery ack" (when meaning the wire type)
- **Definition:** The minimal protobuf ack message
  (`{ bool success, string error_message }`) defined in
  [`proto/task.proto`](../proto/task.proto) and returned by fire-and-acknowledge
  RPCs such as `AgentService.ReceiveChannelMessage`. v0.3.0 uses
  at-most-once delivery semantics: `success=false` signals the agent
  rejected or could not process the event and the orchestrator does **not**
  retry in this release. Introduced in v0.3.0 (PR #246).
- **Example:** "The PR-3 stub returns `TaskAck(success=False)` so the wire
  format is exercised without falsely claiming delivery."

### ReceiveChannelMessage
- **Aliases:** —
- **Disallowed:** "deliver channel message" (when meaning the RPC),
  "channel deliver"
- **Definition:** The `AgentService` gRPC RPC defined in
  [`proto/task.proto`](../proto/task.proto) that the orchestrator calls to
  hand a `ChannelMessageEvent` to one subscribed agent and receive a
  `TaskAck`. Stub-only in PR #246 (RFC 0011 PR 3); the real handler —
  constructing an `AgentEvent(event_type=CHANNEL_MESSAGE)` and dispatching
  through `EventDispatcher` — lands in RFC 0011 PR 4 alongside the
  orchestrator-side `DispatchChannelMessage` action.
- **Example:** "Each subscriber on a publish receives one
  `ReceiveChannelMessage` invocation."

### Respond Policy
- **Aliases:** —
- **Disallowed:** "response mode", "trigger mode"
- **Definition:** Per-membership delivery hint stored on the
  `memberships.respond_policy` column (RFC 0011 §A). Values:
  `when_mentioned` (default), `always`, `never`. Persisted at the channel
  store; the dispatch layer consults it when deciding whether to wake a
  member on a publish. **DM channels bypass the policy** for the
  synchronous-reply façade described under "Chat-as-DM" — see
  [RFC 0011 amendment](rfcs/0011-amendment-chat-as-dm.md).
- **Example:** "Setting `respond_policy: never` on the audit-log channel
  membership silences the agent without removing visibility."

### Chat-as-DM
- **Aliases:** "chat-as-DM unification" (long form on first mention).
- **Disallowed:** "chat channel" (ambiguous — could mean group),
  "DM-chat bridge".
- **Definition:** v0.3.0 unification ([RFC 0011 amendment](rfcs/0011-amendment-chat-as-dm.md), amending RFC 0016)
  modelling every user–agent chat as a `dm` channel
  `dm:<user>:<agent>` in the RFC 0011 channel store. The
  `POST /api/v1/agents/{id}/chat` REST endpoint, the `SendChatMessage`
  gRPC RPC, and the `persatrix chat` REPL are preserved as
  synchronous-reply façades — they publish on the DM channel, await one
  `SEND_CHANNEL_MESSAGE` reply on the same channel, and return it to the
  caller. Eliminates the parallel chat transport that v0.2.1 introduced
  and is the reason `EventType.MESSAGE_RECEIVED` /
  `ActionType.SEND_MESSAGE` were renamed (not just superseded) to
  `CHANNEL_MESSAGE` / `SEND_CHANNEL_MESSAGE` in PR 4a-ii-α
  (RFC 0011, v0.3.0). The chat-as-DM façade lands in PR 4b.
- **Example:** "Under chat-as-DM, the chat REST handler is a thin
  publish-and-await wrapper over `ChannelRouter.Publish` — no separate
  ingest path."

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

## Storage Architecture Roadmap

Terms introduced by [docs/storage-architecture-roadmap.md](storage-architecture-roadmap.md). Glossary entries are stable; per-tier names (Scratchpad, Bonds, etc.) become physical store names when SA-1's RFC adopts them.

### Personal/Society Storage Boundary
- **Aliases:** "the storage split", "personal/society split"
- **Disallowed:** "agent/world split", "private/shared boundary"
- **Definition:** The architectural rule that data with one logical writer and one logical owner (a single agent's memory) lives in per-agent SQLite, while data requiring cross-agent consistency or external query (the *society state*) lives in shared Postgres. Established by [storage-architecture-roadmap.md](storage-architecture-roadmap.md) (SA-1) for v0.4.0.
- **Example:** "Channel messages cross the personal/society storage boundary into Postgres; per-agent episodes do not."

### Society State
- **Aliases:** —
- **Disallowed:** "shared state", "global state", "world state"
- **Definition:** Cross-agent persistent state that no single agent owns: channels, org topology, decision audit, HITL approvals, shared facts, procedural patterns, the audit chain. Backed by Postgres in the SA-1 target picture. Distinct from a single agent's personal memory.
- **Example:** "The decision audit chain is society state and lives in the society Postgres."

### Vectors-as-Accelerator-Only
- **Aliases:** —
- **Disallowed:** "vectors as primary store", "vector-first recall"
- **Definition:** The policy that vector indexes never own facts; they are recall accelerators on top of structured stores that already hold the truth. If a fact only exists as a high-similarity hit, it does not exist. Established by [storage-architecture-roadmap.md](storage-architecture-roadmap.md) (SA-3); enforced via `MemoryStore.recall_by_similarity()` returning row IDs only.
- **Example:** "RFC 0024 is vectors-as-accelerator-only: deletions in `facts.db` are authoritative; the vector index is purged on next rebuild."

### Scratchpad (memory tier)
- **Aliases:** —
- **Disallowed:** "working memory" (when meaning the bridged tier; see Memory),
  "conversational working memory" (which means the live `messages`-array
  transcript — see `Conversation Window`)
- **Definition:** The proposed v0.4 successor name for the working-memory tier: volatile in-RAM context with a small SQLite snapshot that bridges across exactly one prior interaction-close boundary. Replaces today's purely-volatile working memory ([memory-quality-roadmap.md §B](memory-quality-roadmap.md#b-continuity-bridge-across-interaction-close)). Tracked as SA-2.
- **Example:** "The Scratchpad survives interaction close once; after the next close it is overwritten."

### Bonds (memory tier)
- **Aliases:** —
- **Disallowed:** "relationship table" (when meaning the tier), "rapport store"
- **Definition:** The proposed v0.4 successor name for the relationship-memory tier; per-pair trust and interaction texture. Renamed from "relationship" to avoid collision with the relational *database*. Tracked as SA-2.
- **Example:** "Bonds decay slowly between interactions; trust deltas land here at interaction close."

### Procedural Memory
- **Aliases:** —
- **Disallowed:** "skill memory" (when meaning the tier; see Skill)
- **Definition:** A society-shared memory tier holding extracted patterns of *how to do things*, populated by [RFC 0015](rfcs/0015-process-automation-pattern-extraction.md). Distinct from declarative facts and from skills in the Skill Registry; the open question of whether it collapses into RFC 0014's catalogue is tracked in SA-2 ([storage-architecture-roadmap.md OQ #5](storage-architecture-roadmap.md#open-questions)).
- **Example:** "Procedural memory holds the recipe for `triage-a-bug-report`; skills register the tool capability that runs it."

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



## RFC 0008 — Delegation Contract (PR 3)

Terms from [RFC 0008](rfcs/0008-agent-memory-context-optimization.md) PR 3.

### DelegationRequest
- **Definition:** Frozen dataclass
  `agents.sub_agents.delegation.DelegationRequest` — caller-to-sub-agent
  envelope (objective, acceptance criteria, context package, budget,
  allowed tools, output schema, trust ceiling, max memory writes).
  Travels under reserved `_delegation_request` `TaskInput.context` key.

### DelegationResult
- **Definition:** Frozen dataclass
  `agents.sub_agents.delegation.DelegationResult` — sub-agent reply
  (summary, status, artifacts, decisions, memory_writes, risks).
  `status ∈ {completed, partial, failed}`. Travels under reserved
  `_delegation_result` `TaskOutput.metadata` key.

### MemoryWriteEntry
- **Definition:** Frozen dataclass
  `agents.sub_agents.delegation.MemoryWriteEntry` — a single proposed
  memory write inside a `DelegationResult`. `tier ∈ {episodic, notes}`
  (procedural intentionally excluded). `source_agent` is
  framework-injected; caller-set values are rejected.

### MergeEngine
- **Definition:** `agents.sub_agents.merge.MergeEngine` — applies the
  deterministic 6-step pipeline (schema → source-agent inject → cap →
  trust-ceiling → per-entry strategy → metrics) to a `DelegationResult`.
  Strategies: `replace`, `append`, `patch` (RFC 7396), `reject_on_conflict`.

### BudgetEnvelope
- **Definition:** Frozen dataclass
  `agents.sub_agents.delegation.BudgetEnvelope` — `tokens`,
  `timeout_seconds`, `max_llm_calls` caps on a sub-agent invocation.
  `0` means unbounded on that axis.

### DelegationContractError
- **Definition:** `agents.sub_agents.delegation.DelegationContractError`
  — raised when a `DelegationRequest` or `DelegationResult` violates
  the schema (missing required field, disallowed `tier`, malformed
  `MemoryWriteEntry`, etc.). Subclasses :class:`ValueError` so
  existing `try / except ValueError` paths in agent code continue to
  catch it.

### DelegationFailure
- **Definition:** `agents.sub_agents.delegation.DelegationFailure`
  — raised when a sub-agent dispatch fails outright and no merge is
  attempted (e.g. `schema_invalid` at step 1 of the deterministic
  merge order). Per-entry rejections are not fatal — they are logged,
  metrics are emitted via `delegation_metric`, and the surviving
  entries still merge. Subclasses :class:`RuntimeError`; intentionally
  retained over an `…Error` suffix to mirror RFC 0008 §E vocabulary
  (`# noqa: N818`).

### delegation_metric
- **Definition:** Structured-log message name emitted by
  `agents.sub_agents.merge.MergeEngine` for every per-entry merge
  outcome (admitted, rejected, downscaled, conflict-rejected). Carries
  `metric`, `labels`, `value` in the log `extra` dict. Back-fill to
  Go-side counters lands in the delegation-metrics follow-on PR
  (sizing-risk split). The single source of structured truth for
  delegation observability prior to that back-fill.

### _bounded (alias of `bounded`)
- **Definition:** `agents.sub_agents._log_safety.bounded` — sanitises
  attacker-influenceable text before it is interpolated into orchestrator
  log lines or `DelegationFailure` messages. Two defences (CWE-117 /
  OWASP A09 / LLM01): (1) strip every C0 control character (0x00-0x1F)
  plus DEL (0x7F) to U+2424 (`SYMBOL FOR NEWLINE`); (2) cap length to
  200 chars with the canonical `… (truncated)` marker. `_bounded` is a
  backwards-compat private alias retained through v0.3.x; remove in v0.4.0
  once the public `bounded` name has been documented for a full release
  cycle.



## RFC 0008 — Shared Memory Pools (PR 4)

From [RFC 0008](rfcs/0008-agent-memory-context-optimization.md) §H.

- **SharedMemoryPool:** `agents.memory.shared_pool` — named cross-agent pool
  with reader/writer ACL, framework-injected `source_agent`,
  `min_confidence` filter, FIFO eviction. Wraps `EpisodicMemory` under
  `pool-{name}`.
- **SharedPoolEntry / SharedPoolConfig / SharedPoolRegistry:** Read result,
  ACL+retention (from `shared_memory_pools`), and per-process owner.
  `Registry.get` raises `unknown_pool` when missing.
- **SharedMemoryPermissionError:** `reason` ∈ {`not_in_readers`,
  `not_in_writers`, `sensitive_pool_isolation`, `unknown_pool`}.
- **publish_to_pool / read_from_pool:** `MemoryFacade` methods for
  isolated→shared; `publish_to_pool` rejects `sensitive` pools.
- **shared_memory_pools:** `config/agents.yaml` section.

## RFC 0020 — Interaction Lifecycle (PR 4)

Terms from [RFC 0020](rfcs/0020-interaction-lifecycle.md) PR 4.

### Closing-state interaction
- **Disallowed:** "pending interaction", "in-flight interaction".
- **Definition:** Episode row with `closed_at` set whose `summary`
  still carries the **summary-pending sentinel**. Phase 1 of the
  close-path two-phase write produces it; the background summariser
  or **interaction janitor** resolves it. See PR #229 Must-Fix #1.

### Summary-pending sentinel
- **Disallowed:** "pending marker", "TBD summary".
- **Definition:** Literal `[summary pending]` (`SUMMARY_PENDING_TEXT`)
  written between Phase 1 and Phase 2 of the close-path write.

### Summary-unavailable sentinel
- **Disallowed:** "summary failed", "no summary".
- **Definition:** Literal `[interaction summary unavailable]`
  (`SUMMARY_UNAVAILABLE_TEXT`) written on summariser failure
  (`timeout`, `llm_error`, `empty`) or janitor backfill.

### Interaction janitor
- **Aliases:** "closing-state janitor", "PR 4 janitor".
- **Disallowed:** "garbage collector".
- **Definition:** `cleanup_closing_interactions` — idempotent `UPDATE`
  rewriting closing-state rows past `grace_sec` to the
  summary-unavailable sentinel. Invoked from `on_tick` at most once
  per `JANITOR_INTERVAL_SEC` (default 300 s).

## RFC 0009 — Security & Sandboxing (PR plan)

Terms pinned by [RFC 0009 PR plan](rfcs/0009-pr-plan.md) Open-Question
resolutions (PR #232). The broader Go type names (`AuditLogger`,
`SecretRedactor`, `RateLimiter`, `CircuitBreaker`, `InputSanitizer`,
`ContextItem`) are reserved here and will get full entries in the
glossary update that ships **with** PR 1 / PR 2 / PR 3 — when the types
actually exist in the tree — to avoid documenting a contract before the
code lands. Only the OQ-resolution terms (which this PR commits us to
verbatim) are defined now (PR #232 review SF-5).

### `_provenance` sidecar
- **Disallowed:** "context provenance proto field", "provenance map".
- **Definition:** Reserved key under `TaskRequest.context` (a
  `map<string, string>` in the existing proto) whose value is a JSON
  string mapping each other context key to
  `{"source", "sanitized", "flagged", "flags"}`. Avoids a v0.3.0 proto
  regen cycle; promoted to a typed proto in v0.4.0 alongside the
  Phase 4 token field. Reserved-key precedent: RFC 0008 `_budget`.

### `chain.restart` / `chain.bootstrap` / `chain.recovered`
- **Disallowed:** "audit chain reset", "checksum break event".
- **Definition:** Three security-class audit events emitted at
  `AuditLogger` startup based on the state of `audit.jsonl`:
  - `chain.bootstrap` — file missing or zero-length; chain seeded from
    `sha256("")`.
  - `chain.restart` — tail line parses and its checksum recomputes;
    event carries the prior tail checksum so external tooling can
    detect the process-boundary discontinuity.
  - `chain.recovered` — tail line is unparseable / truncated /
    checksum-mismatch; carries `Detail.prior_tail = "unknown"` and a
    WARN log. Operators must acknowledge — the log is **not** silently
    continued from a fresh chain.

### `SanitizerAction.Passthrough` / `SanitizerAction.Quarantine`
- **Disallowed:** "sanitizer mode", "drop-on-flag".
- **Definition:** Enum binding `security.sanitizer_action`.
  `Passthrough` (v0.3.0 default) wraps flagged tool results in a
  `<external_data flagged="true">` envelope and delivers them to the
  agent. `Quarantine` returns the structured agent error
  `{"error": "tool_result_quarantined", "flags": [...]}` and drops the
  content. Silent strip (option *c* in OQ 2) is rejected.

### `tool_result_quarantined`
- **Aliases:** none — the literal string is the contract.
- **Disallowed:** "blocked tool result", "sanitizer error".
- **Definition:** Agent-facing error string returned by the tool layer
  when `SanitizerAction.Quarantine` drops a flagged result. Documented
  in `prompts/system/external_data_handling.txt` so personas can
  recognise and back off without re-deriving the shape.

### `ContextSource.channel_message`
- **Disallowed:** "channel-sourced input", "RFC-0011 message source".
- **Definition:** New `ContextSource` enum variant (Phase 2) tagging
  inputs that arrive through the RFC 0011 channel-publish path.
  Treated as `external`-equivalent for sanitization but kept distinct
  in the audit trail so forensics can distinguish "agent posted to
  channel" from "scraped webpage". The orchestrator is the authority on
  this tagging — agents cannot self-report `source` values.

### Audit `CorrelationID` (4-segment form)
- **Disallowed:** "correlation tuple", "audit trace ID".
- **Definition:** Colon-delimited string
  `WorkflowRunID:StepID:AgentID:InteractionID?` written into
  `AuditEvent.CorrelationID`. The fourth segment is empty (`run:step:agent:`
  — trailing colon **kept**) when the event was emitted outside an open
  RFC 0020 interaction. The fixed-4-field shape is a parse contract for
  downstream tooling.


### `AuditLogger` (PR 1 / PR 1b)
- **Aliases:** "audit sink"; **Disallowed:** "security log writer".
- **Definition:** Interface in `internal/security/audit.go` writing
  `AuditEvent` records to a durable JSONL sink with SHA-256 chained
  tamper evidence. The Phase 1 implementation (`fileAuditLogger`) is
  file-backed; the interface is small so future SIEM / write-once
  transports can drop in without churning call sites. `Path()` is
  hoisted onto the interface so callers can surface the resolved sink
  in startup logs without depending on the concrete type
  (PR #233 review Should-Fix #4).

### `SecretRedactor` (PR 1)
- **Aliases:** none; **Disallowed:** "secret scrubber", "PII filter"
  (PII is a separate v0.4.0 concern).
- **Definition:** `Redactor` implementation in
  `internal/security/redactor.go` carrying the five default patterns
  from RFC 0009 §I (`anthropic-api-key`, `openai-api-key`,
  `bearer-token`, `aws-access-key`, `generic-secret`). Installed by
  default in `NewFileAuditLogger` (PR #233 review Should-Fix #3) so any
  caller embedding a secret in `AuditEvent.Detail` / `Action` /
  `Resource` sees it scrubbed before the canonical-JSON encode.

### Tamper evidence
- **Aliases:** "audit chain integrity"; **Disallowed:** "audit signing"
  (no signature is computed — only a content-addressed hash chain).
- **Definition:** Property of the audit log whereby every record
  carries `Checksum = sha256(prevChecksum || canonicalJSON(event))`.
  Any in-place mutation of a prior record invalidates every subsequent
  checksum, making post-hoc redaction observable. Operator-verifiable
  with a 50-line script; not a defence against append-only-after-the-fact
  attackers — see RFC 0009 §G for threat-model bounds.
