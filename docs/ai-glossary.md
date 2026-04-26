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
2. **Expand acronyms once.** First occurrence in a document spells out the
   acronym (e.g. "directed acyclic graph (DAG)"); subsequent uses may be short.
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
- **Disallowed:** "pipeline", "job", "DAG" (when meaning workflow), "flow"
- **Definition:** A directed acyclic graph (DAG) of tasks defined in YAML under
  `workflows/`, supporting sequential, parallel, conditional, and looped steps.
- **Example:** "The workflow fans out three review tasks before aggregation."

### Task
- **Aliases:** "step" (only inside a workflow definition)
- **Disallowed:** "job", "unit"
- **Definition:** A single unit of work assigned to an agent, with a status
  lifecycle: `pending → running → completed | failed | cancelled`.
- **Example:** "The task failed after exhausting `max_retries`."

### Tool
- **Aliases:** —
- **Disallowed:** "function", "skill" (RFC 0014 reserves "skill" for a different
  concept), "plugin"
- **Definition:** A typed callable an agent can invoke, declared with the
  `@tool(name=..., permissions=[...])` decorator in `agents/tools/`.
- **Example:** "The `file_read` tool requires the `fs.read` permission."

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

### Bridge
- **Aliases:** —
- **Disallowed:** "connector", "adapter" (when meaning bridge), "integration"
- **Definition:** A v0.5.0 component that connects an internal channel to an
  external service (Slack, Discord, email, Telegram).
- **Example:** "The Slack bridge mirrors `#ops` into a Slack workspace."

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
- **Example:** "Channel turns are written to episodic memory via `InteractionTracker`."

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
- **Aliases:** "observability" (when speaking broadly)
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
