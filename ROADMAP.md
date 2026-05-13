# Persatrix Roadmap

> **Last updated**: 2026-05-13 (RFC 0031 PR plan PR 2 opened — sessions table + Go orchestrator migrations + `PERSATRIX_SESSION_ID` env-var threading; RFC 0031 row → 🚧 Implementing per [v0.3.1-plan §ROADMAP hygiene](docs/v0.3.1-plan.md#roadmap-hygiene))
> **Current phase**: v0.3.0 (Agent Conversations — RFCs 0008, 0009 P1–2, 0011 internal, 0020, 0021 P1, 0022) — ✅ Released
> **Current milestone**: v0.3.0 released ([tag v0.3.0](https://github.com/mkhomutov/Persatrix/releases/tag/v0.3.0) pushed 2026-05-12, GitHub Release "Agent Conversations" published the same day); v0.3.x planning next (RFCs 0023 / 0024 / 0026 / 0029 sequenced per [v0.3.x-sequencing.md](docs/v0.3.x-sequencing.md); F-3 root-cause fix tracked in [RFC 0031](docs/rfcs/0031-per-session-namespacing-channels.md)).

This document tracks development progress across all versions. Update it when merging PRs or completing milestones.

---

## Version Map

A version is ready when a developer can do something meaningful they could not do before. Versions are defined by what a user **can do** — not by which RFCs are internally complete.

| Version | What a user can do | Status |
|---------|-------------------|--------|
| **v0.1.0** | Submit YAML workflows, orchestrate task agents via gRPC, poll status via REST | ✅ Complete — internal baseline |
| **v0.2.0** ⭐ | Run persistent AI agents with personalities, memory, and evolving relationships from a terminal | ✅ Complete — first public release |
| **v0.2.1** | Talk to a persona agent from your terminal — the agent remembers you and responds in character | ✅ Complete — released |
| **v0.2.2** | Bounded, predictable per-event memory injection for persona agents — structural fix unblocking RFC 0008 | ✅ Complete — released |
| **v0.2.3** | Observability Foundation — logs + traces + metrics + correlation shipped together: structured JSON logs across Go/Python/CLI on a versioned schema, working `persatrix logs` CLI (with `--follow` and server-side filters), end-to-end OpenTelemetry traces from REST handler to LLM call (with OTEL Gen-AI semantic conventions), OTLP metrics with exemplars, W3C Baggage propagation, and a tail-sampling Collector pipeline. Combined deliverable of RFCs 0018 + 0019. | ✅ Complete — released |
| **v0.3.0** | Give agents a shared channel and watch them talk, negotiate, and form opinions over time | ✅ Complete — released |
| **v0.4.0** | Define a team, lab, or company with roles and hierarchy — and let it run | 📋 Planned |
| **v0.5.0** | Bridge your agent society into Slack, Discord, or email | 📋 Planned |
| **v0.6.0** | Run agent societies across multiple nodes and networks | 📋 Planned |

---

## RFC Master Index

Internal RFCs are the engineering planning tool. They do not drive version numbers. The table below shows each RFC's target public version.

| RFC | Title | Target Version | Status |
|-----|-------|----------------|--------|
| [0001](docs/rfcs/0001-core-orchestration-pipeline.md) | Core Orchestration Pipeline | v0.1.0 | ✅ Implemented |
| [0002](docs/rfcs/0002-rest-api-server.md) | REST API Server | v0.1.0 | ✅ Implemented |
| [0003](docs/rfcs/0003-scheduler-executor.md) | Scheduler & Executor | v0.1.0 | ✅ Implemented |
| [0004](docs/rfcs/0004-python-agent-grpc-server.md) | Python Agent gRPC Server | v0.1.0 | ✅ Implemented |
| [0005](docs/rfcs/0005-persona-agent-memory.md) | Persona Agent & Memory System | v0.2.0 | ✅ Implemented |
| [0006](docs/rfcs/0006-efficiency-execution-limits.md) | Efficiency & Execution Limits | v0.2.0 | ✅ Implemented |
| [0007](docs/rfcs/0007-conditional-looped-workflow-control-flow.md) | Conditional & Looped Workflow Control Flow | v0.4.0 | 📋 Proposed |
| [0008](docs/rfcs/0008-agent-memory-context-optimization.md) | Agent Memory & Context Optimization | v0.3.0 | ✅ Implemented (PRs 1 [#218](https://github.com/mkhomutov/Persatrix/pull/218), 1b [#219](https://github.com/mkhomutov/Persatrix/pull/219), 2 [#220](https://github.com/mkhomutov/Persatrix/pull/220), 2a [#221](https://github.com/mkhomutov/Persatrix/pull/221), 3 [#222](https://github.com/mkhomutov/Persatrix/pull/222), 3a [#224](https://github.com/mkhomutov/Persatrix/pull/224), 4 [#223](https://github.com/mkhomutov/Persatrix/pull/223), 5 [#225](https://github.com/mkhomutov/Persatrix/pull/225), 6a [#227](https://github.com/mkhomutov/Persatrix/pull/227), 6b [#228](https://github.com/mkhomutov/Persatrix/pull/228), 6 (this PR) merged. OQ #12 calibration-window gate walked back 2026-05-10 — eviction-parameter calibration deferred to a v0.3.x follow-up that fires when observed-workload telemetry exists; instrumentation from PR 5 ships unchanged.) |
| [0009](docs/rfcs/0009-security-sandboxing.md) | Agent Identity, Security & Sandboxing | v0.3.0 (Phases 1–2) + v0.4.0 (Phases 3–4) | ⚠️ Partially Implemented (Phases 1–2; PRs 1 [#233](https://github.com/mkhomutov/Persatrix/pull/233), 1b [#234](https://github.com/mkhomutov/Persatrix/pull/234), 1c [#236](https://github.com/mkhomutov/Persatrix/pull/236), 2 [#244](https://github.com/mkhomutov/Persatrix/pull/244), 3 [#253](https://github.com/mkhomutov/Persatrix/pull/253), 4 (this PR) merged — audit logger + secret redactor + orchestrator wiring + RedactStruct hardening + audit metrics + RateLimiter + CircuitBreaker + REST/gRPC middleware + unquarantine endpoint + InputSanitizer + Go canonical patterns + Python mirror + `<external_data>` envelope wrapping + tag-escape hardening + PR 4 review follow-ups (typed depth-marker sentinel; deterministic ticker test seam; `VerifyChain` exported helper; `looksLikeSHA256` → `hex.DecodeString`; `Emit` write-alloc reduction; generic-secret trailing-quote nit; GitHub/GCP/Slack/Stripe redactor patterns; `RedactStruct` benchmark; coverage-gap tests; PR #234 N-1/N-2/N-3 + PR #236 L-1/L-2/L-3/L-5 dispatched). Phases 3–4 deferred to v0.4.0 — see [RFC 0009 Implementation Notes (v0.3.0)](docs/rfcs/0009-security-sandboxing.md#implementation-notes-v030) for v0.3.0 deviations.) |
| 0010 | Sub-Agent Spawning | v0.4.0 | Not yet written |
| [0011](docs/rfcs/0011-channels-bridges.md) | Channels + Bridges | v0.3.0 (internal) + v0.5.0 (external) | ⚠️ Partially Implemented (internal channels — external bridges deferred to v0.5.0) |
| 0012 | Protocols + Organizations | v0.4.0 (partial) + v0.5.0 (remainder) | Not yet written |
| [0013](docs/rfcs/0013-legal-ethical-compliance.md) | Legal, Ethical & Regulatory Compliance | v0.5.0 | 📋 Proposed |
| [0014](docs/rfcs/0014-agent-skill-registry-lifecycle.md) | Agent Skill Registry & Lifecycle | v0.4.0 | 📋 Proposed |
| [0015](docs/rfcs/0015-process-automation-pattern-extraction.md) | Process Automation & Pattern Extraction | v0.5.0 | 📋 Proposed |
| [0016](docs/rfcs/0016-human-participant-chat-interface.md) | Human Participant & Chat Interface | v0.2.1 | ✅ Implemented (Amended 2026-05-12 — wire-field rename `session_id` → `chat_session_id` per [RFC 0031 §OQ #8](docs/rfcs/0031-per-session-namespacing-channels.md#open-questions); see [RFC 0016 §Amendments](docs/rfcs/0016-human-participant-chat-interface.md#amendments)) |
| [0017](docs/rfcs/0017-persona-memory-injection-budget.md) | Persona Memory Injection Token Budget | v0.2.2 | ✅ Implemented (7/7) |
| [0018](docs/rfcs/0018-structured-logging-framework.md) | Structured Logging Framework | v0.2.3 | ✅ Implemented |
| [0019](docs/rfcs/0019-opentelemetry-completion.md) | OpenTelemetry Completion | v0.2.3 | ✅ Implemented |
| [0020](docs/rfcs/0020-interaction-lifecycle.md) | Interaction Lifecycle: Dialogue Boundaries & Episode Granularity | v0.3.0 | ✅ Implemented |
| [0021](docs/rfcs/0021-persona-temporal-awareness.md) | Persona Temporal Awareness | v0.3.0 (Phase 1) + v0.4.0 (Phases 2–4) | ⚠️ Partially Implemented (Phase 1) |
| [0022](docs/rfcs/0022-persona-prompt-section-templating.md) | Persona Prompt Section Templating | v0.3.0 | ✅ Implemented |
| 0023 | Episodic Memory Quality (JSON summary schema only — narrowed scope per [memory-quality-roadmap.md](docs/memory-quality-roadmap.md)) | v0.3.x | Reserved (narrowed) |
| 0024 | Episodic Vector Recall — deferred, gated on [MT-MEMORY-005](docs/manual-tests/MT-MEMORY-005-dementia-test.md) data | v0.3.x or v0.4.0 | Reserved (deferred) |
| 0025 | Thematic Episode Clustering — superseded by RFC 0027 per [memory-quality-roadmap.md](docs/memory-quality-roadmap.md) | superseded | Reserved (superseded by 0027) |
| [0026](docs/rfcs/0026-declarative-facts-tier.md) | Declarative Facts Tier | v0.3.x | 📋 Proposed |
| [0027](docs/rfcs/0027-reflection-driven-consolidation.md) | Reflection-Driven Consolidation | v0.4.0 | 📋 Proposed |
| [0028](docs/rfcs/0028-agent-decision-policy-engine.md) | Agent Decision Policy Engine | v0.4.0 | 📋 Proposed |
| [0029](docs/rfcs/0029-personal-society-storage-split.md) | Personal/Society Storage Split (SA-1 from [storage-architecture-roadmap.md](docs/storage-architecture-roadmap.md); originally filed as 0025, renumbered to preserve the 0025→0027 supersession edge) | v0.3.x (Phase 1) + v0.4.0 (Phases 2–6) | 📋 Proposed |
| [0030](docs/rfcs/0030-multi-agent-conversation-governance.md) | Multi-Agent Conversation Governance — layered termination + cost + reply-budget + moderator over the v0.3.0 channels stack; composes RFC 0011 amendment / RFC 0020 / RFC 0023 / RFC 0028. Motivated by the v0.3.0 F-1 finding tail (cost ceiling and productive-termination beyond cascade_depth). | v0.3.x (Phase 1 — deterministic layers) + v0.4.0 (Phase 2 — moderator) + v0.5.0+ (Phase 3 — declarative types + topic-drift) | 📋 Proposed (Draft) |
| [0031](docs/rfcs/0031-per-session-namespacing-channels.md) | Per-Session Namespacing for Channels and Persona Memory — first-class Session primitive scoping `channels.db` and per-persona `memory.db`, with an operator-visible `persatrix session …` CLI; F-3 root-cause fix, succeeds the `make reset` workaround from PR 6 of the v0.3.0 channel test-findings plan. Spawned from [ISSUE-0051](docs/issues/ISSUE-0051-per-session-memory-namespacing-channels.md). | v0.3.1 (Phase 1) + v0.3.x (Phases 2–4) | 🚧 Implementing |

---

## v0.1.0 — Core Engine

**What a user can do**: Submit YAML workflows via CLI, orchestrator plans and schedules stages, dispatches tasks to Python agents over gRPC, agents call LLMs and tools, results flow back.

**Status**: ✅ Complete — internal baseline. Not publicly released; project was renamed to Persatrix before first public release.

### RFC Scope

| RFC | Title | Status | PRs | Merged |
|-----|-------|--------|-----|--------|
| [0001](docs/rfcs/0001-core-orchestration-pipeline.md) | Core Orchestration Pipeline (Planner + State + Registry) | ✅ Implemented | 6+1 | 7/7 |
| [0002](docs/rfcs/0002-rest-api-server.md) | REST API Server (HTTP Layer + Workflow Submission) | ✅ Implemented | 4 | 4/4 |
| [0003](docs/rfcs/0003-scheduler-executor.md) | Scheduler & Executor (Parallel Stage Execution + gRPC Dispatch) | ✅ Implemented | 7+4 | 11/11 |
| [0004](docs/rfcs/0004-python-agent-grpc-server.md) | Python Agent gRPC Server (AgentService Implementation) | ✅ Implemented | 7 | 7/7 |

### Dependency Chain

```
RFC 0001 (State, Registry, Planner)           ✅ Done (6 core + 1 follow-up = 7/7)
    ↓
RFC 0002 (REST API Server)                    ✅ Done
    ↓
RFC 0003 (Scheduler + Executor + gRPC)        ✅ Done (7 core + 4 follow-up = 11/11)
    ↓
RFC 0004 (Python Agent Server + Tools)        ✅ Done (7/7)
    ↓
v0.1.0 complete — end-to-end execution working
```

### Component Status

#### Go Orchestrator (`internal/`)

| Package | Purpose | Status |
|---------|---------|--------|
| `internal/state/` | Workflow/step state tracking | ✅ Complete (100% coverage) |
| `internal/registry/` | Agent registration and lookup | ✅ Complete (~95% coverage) |
| `internal/planner/` | YAML parsing, DAG validation, topological sort | ✅ Complete (100% coverage) |
| `internal/server/` | REST API (11 endpoints, middleware, graceful shutdown) | ✅ Complete (86.5% coverage) |
| `internal/scheduler/` | Workflow scheduling (pick up pending runs, drive stages) | ✅ Complete (87.3% coverage) |
| `internal/executor/` | gRPC task dispatch to agents | ✅ Complete (96.1% coverage) |
| `internal/generated/` | Protobuf/gRPC generated code | ✅ Complete (generated stubs) |
| `internal/resilience/` | Circuit breaker, dead letter queue | 🔲 TODO stub (post-v0.1) |
| `internal/security/` | Audit logger, redactor, rate limiter, circuit breaker, REST/gRPC middleware, input sanitizer | 🚧 In progress (v0.3.0 — RFC 0009 PRs 1/1b/1c/2/3) |
| `internal/observability/` (renamed from `internal/telemetry/`) | OTEL span instrumentation + structured logging encoder + log buffer + LogService | ✅ Complete (RFC 0018 + RFC 0019, shipped in v0.2.3) |
| `internal/cost/` | Token/cost tracking aggregation | ✅ Complete (RFC 0006) |

#### Python Agents (`agents/`)

| Module | Purpose | Status |
|--------|---------|--------|
| `agents/base.py` | BaseAgent ABC + dataclasses + LLM loop | ✅ Complete (RFC 0004 PR 4a) |
| `agents/llm_client.py` | Multi-provider LLM client (Anthropic + OpenAI) | ✅ Complete (RFC 0004 PR 4a) |
| `agents/server.py` | gRPC service entry point + self-registration | ✅ Complete (RFC 0004 PR 5a+5b) |
| `agents/task_agent.py` | Data-driven task agent (replaces CoderAgent, ReviewerAgent, PlannerAgent) | ✅ Complete (RFC 0005 PR 1a) |
| `agents/tools/registry.py` | Tool discovery and registration | ✅ Complete (decorator + registry) |
| `agents/tools/builtin.py` | Built-in tools (file_read, file_write, shell_exec, http_request, memory tools) | ✅ Complete (RFC 0004 PR 3, RFC 0005 PR 3b) |
| `agents/tools/permissions.py` | Deny-by-default permission gate | ✅ Complete (97% coverage) |
| `agents/tools/sandbox.py` | Filesystem path restriction (PathValidator) | ✅ Complete (100% coverage) |
| `agents/generated/` | Python gRPC generated stubs | ✅ Complete (RFC 0004 PR 5a) |

#### Rust CLI (`cli/`)

| Module | Purpose | Status |
|--------|---------|--------|
| `cli/src/main.rs` | CLI entry point, clap definitions, command dispatch | ✅ Functional |
| `cli/src/types.rs` | API request/response types, shared validation helpers | ✅ Complete |
| `cli/src/commands/workflow.rs` | Workflow commands (run, status) | ✅ Complete |
| `cli/src/commands/agent.rs` | Agent commands (list, info, reload, test persona) | ✅ Complete |
| `cli/src/commands/logs.rs` | Execution log viewing | ✅ Complete |
| `cli/src/commands/validate.rs` | Config validation (Python subprocess) | ✅ Complete |

### What Works in v0.1.0

1. Submit a workflow via CLI → `POST /api/v1/workflows/run`
2. Orchestrator receives request, planner parses YAML, validates DAG, generates execution plan
3. Server creates `WorkflowRun` in state store → status = Pending
4. Scheduler polls for pending runs, transitions to Running, drives parallel stage execution
5. Executor dispatches tasks to agents via gRPC `ExecuteTask` with retry logic
6. Step outputs resolve across stages via `{{ steps.<key>.output }}` templates
7. Poll `GET /api/v1/workflows/{id}/status` → returns Running/Completed/Failed with step details
8. CRUD operations on agents via REST API

---

## v0.2.0 — Persona Core ⭐ First Public Release

**What a user can do**: Run persistent AI agents with real personalities, memory, and evolving relationships from a terminal.

### What ships in v0.2.0

- **PersonaAgent** — full behavioral model: personality dimensions, mood, stress, goals (RFC 0005)
- **Three-tier memory** — episodic (SQLite + FTS5), relationship (trust + interaction history), working (context window management) (RFC 0005)
- **Autonomous tick loop** — agents act without being prompted (RFC 0005)
- **Relationship dynamics** — trust scores that evolve based on interactions (RFC 0005)
- **Budget controls** — token caps, spend limits, deadline enforcement, pre-dispatch budget gating (RFC 0006)
- **Execution observability** — per-step token usage, LLM call count, retry count, estimated cost in API responses (RFC 0006)
- **CLI** — `run`, `inspect`, and `observe` persona agents from the terminal (RFC 0005)

### What does not ship in v0.2.0

- Agent-to-agent conversations and channels (RFC 0011) → v0.3.0
- Conditional and looped workflow control flow (RFC 0007) → v0.4.0
- Agent memory and context optimization for non-persona agents (RFC 0008) → v0.3.0
- Security hardening beyond existing deny-by-default tool gates (RFC 0009) → v0.3.0
- Sub-agent spawning (RFC 0010) → v0.4.0
- Organizational hierarchy, roles, escalation (RFC 0012) → v0.4.0
- Skill registry and lifecycle governance (RFC 0014) → v0.4.0
- External bridges — Slack, Discord, Telegram, email (RFC 0011) → v0.5.0
- Compliance and privacy layer (RFC 0013) → v0.5.0
- Process automation and pattern extraction (RFC 0015) → v0.5.0
- Distributed mesh (v0.6.0)
- Web dashboard

### RFC Scope

| RFC | Title | Status | PRs | Merged |
|-----|-------|--------|-----|--------|
| [0005](docs/rfcs/0005-persona-agent-memory.md) | Persona Agent & Memory System | ✅ Implemented | 20 | 20/20 |
| [0006](docs/rfcs/0006-efficiency-execution-limits.md) | Efficiency & Execution Limits | ✅ Implemented | 12 | 12/12 |

### RFC 0006 — Execution Progress

```
RFC 0005 (PersonaAgent + Memory + TaskAgent)              ✅ Done (20/20)
    ↓
RFC 0006 (Efficiency & Execution Limits)                  ✅ Done (12/12)
    PR 1a — defaults package + Step limits + schema       ✅ #79
    PR 1b — executor + scheduler limit wiring             ✅ #81
    PR 1c — Python defaults + validation                  ✅ #83
    PR 2  — deadline derivation + retry budget            ✅ #84
    PR 3a — TokenCounter + BudgetEnforcer                 ✅ #85
    PR 3b — CostReporter + scheduler budget integration   ✅ #86
    PR 4a — StepExecutionMetadata + observability         ✅ #87
    PR 4b — response cache + cost endpoint                ✅ #88
    PR 5a — executor + scheduler + state follow-ups       ✅ #90
    PR 5b — cost package hardening                        ✅ #91
    PR 5c — planner/schema + Python fixes                 ✅ #92
    PR 6  — PR 5c follow-ups + RFC close                  ✅ #93
    ↓
v0.2.0 complete
```

> All 12 PRs merged. RFC 0006 closed.

### Component Status

#### Go Orchestrator (`internal/`) — v0.2.0 additions

| Package | Purpose | Status |
|---------|---------|--------|
| `internal/defaults/` | Centralized execution limit constants | ✅ Complete (RFC 0006 PR 1a) |
| `internal/planner/` | Step-level limit fields (`TimeoutSeconds`, `MaxLLMCalls`, `MaxTokens`, `ContextBudget`) | ✅ Updated (RFC 0006 PR 1a) |
| `internal/executor/` | Full `TaskConfig` population, derived deadlines, shared-deadline retry, response cache | ✅ Complete (RFC 0006 PRs 1b+2+4a+4b) |
| `internal/scheduler/` | Limit cascade (step → agent → defaults), pre-dispatch budget gate, token recording, metadata | ✅ Complete (RFC 0006 PRs 1b+3b+4a) |
| `internal/cost/` | `TokenCounter`, `BudgetEnforcer`, `CostReporter`, response cache | ✅ Complete (RFC 0006 PRs 3a+3b+4b) |
| `internal/state/` | `StepExecutionMetadata` (tokens, LLM calls, retries, cache hit, cost, wall time) | ✅ Complete (RFC 0006 PR 4a) |
| `internal/server/` | Cost summary endpoint (`GET /api/v1/cost/summary`) | ✅ Complete (RFC 0006 PR 4b) |
| `internal/observability/` (renamed from `internal/telemetry/`) | OTEL span instrumentation + structured logging encoder + log buffer + LogService | ✅ Complete (RFC 0018 + RFC 0019, shipped in v0.2.3) |

#### Python Agents (`agents/`) — v0.2.0 additions

| Module | Purpose | Status |
|--------|---------|--------|
| `agents/memory/working.py` | Working memory (context window management, priority retention, compression) | ✅ Complete (RFC 0005 PR 2) |
| `agents/memory/episodic.py` | Episodic memory (SQLite, FTS5, episode CRUD, recall, summarization, delegates notes to NoteStore) | ✅ Complete (RFC 0005 PR 3a+3b+3c, refactored PR 8b) |
| `agents/memory/notes.py` | Agent-initiated note storage (NoteStore, CRUD, FTS5/LIKE search, pruning) | ✅ Complete (RFC 0005 PR 8b) |
| `agents/memory/migrations.py` | Schema migrations, FTS5 DDL, scoring SQL constants | ✅ Complete (RFC 0005 PR 8b) |
| `agents/memory/relationship.py` | Relationship memory (trust tracking, interaction history, bidirectional decay) | ✅ Complete (RFC 0005 PR 4) |
| `agents/persona.py` | PersonaAgent ABC, `create_persona_agent()` factory, re-exports | ✅ Complete (RFC 0005 PR 5a+5b, refactored PR 8a+8d) |
| `agents/persona_runtime.py` | `_LLMPersonaAgent` concrete class (LLM-powered event loop, memory injection, tool use) | ✅ Complete (RFC 0005 PR 8d) |
| `agents/persona_types.py` | Persona type definitions (`PersonaState`, `Mood`, `AgentEvent`, `EventType`, `AgentAction`, `ActionType`) | ✅ Complete (RFC 0005 PR 8a) |
| `agents/persona_behavior.py` | Behavioral dimension rendering (`render_behavior`, `DIMENSION_DESCRIPTIONS`) | ✅ Complete (RFC 0005 PR 8a) |
| `agents/dispatch.py` | Event dispatch and action execution (`EventDispatcher`, `ActionExecutor`) | ✅ Complete (RFC 0005 PR 8a) |
| `agents/tick.py` | Autonomous tick scheduler (`TickScheduler`) | ✅ Complete (RFC 0005 PR 8a) |
| `agents/validate.py` | Config validation (JSON Schema) | ✅ Complete (RFC 0005 PR 6a) |
| `agents/defaults.py` | Python execution limit constants (centralizes magic numbers from `base.py`) | ✅ Complete (RFC 0006 PR 1c) |

### What Works in v0.2.0 (RFC 0005 complete)

1. Configure a persona agent in `config/agents.yaml` with personality dimensions, mood, goals, and memory settings
2. Start the agent gRPC server — agent self-registers with the orchestrator
3. Agent's autonomous tick loop fires on a configurable interval — it generates actions and events without external prompts
4. Events (user messages, tick events, relationship events) are dispatched to `on_event()`
5. Each interaction persists to episodic memory (SQLite) and updates the relationship trust score for the sender
6. Working memory manages the context window — high-priority items are retained, excess is summarized
7. CLI commands: `persatrix agent test-persona`, `persatrix agent info`, `persatrix agent list`
8. Relationships evolve over time: trust decays when agents don't interact, grows with positive interactions

---

## v0.2.1 — Talk to Your Agents ✅ Complete

**What a user can do**: Open a terminal, type `persatrix chat <agent_id>`, and have a conversation with a persona agent. The agent remembers you and builds a relationship with you over time.

### What ships in v0.2.1

- **`Participant` abstraction** — `Participant` Protocol generalising agents, users, and future system actors (RFC 0016)
- **`UserParticipant`** — persistent user identity stored in the agent SQLite database (RFC 0016)
- **Memory generalization** — `RelationshipMemory` extended to track trust and interactions with human users; `EpisodicMemory` records user-agent exchanges (RFC 0016)
- **`persatrix chat` CLI command** — interactive REPL for conversations with persona agents (RFC 0016)
- **Chat REST endpoint** — `POST /api/v1/agents/{id}/chat` for synchronous message-response round-trips (RFC 0016)
- **`SendChatMessage` gRPC RPC** — new `AgentService` method for orchestrator→agent chat routing (RFC 0016)

### What does not ship in v0.2.1

- Multi-user support — single `UserParticipant` per session; multi-user support is RFC 0011 (v0.3.0)
- Authentication — sessions are local and caller-supplied; auth is RFC 0009 (v0.3.0)
- Agent-initiated messages to users — notification infrastructure deferred
- Streaming chat responses — synchronous request-response only for v0.2.1
- Channel routing for user messages — channels are RFC 0011 (v0.3.0)

### RFC Scope

| RFC | Title | Status | PRs | Merged |
|-----|-------|--------|-----|--------|
| [0016](docs/rfcs/0016-human-participant-chat-interface.md) | Human Participant & Chat Interface | ✅ Implemented | 7 | 7/7 |

### Dependency Chain (v0.2.1)

```
v0.2.0 complete (RFC 0005 ✅, RFC 0006 ✅)
    ↓
RFC 0016 Phase 1 (Participant abstraction + memory generalization)
    ↓
RFC 0016 Phase 2 (proto + gRPC + REST wiring)
    ↓
RFC 0016 Phase 3 (persatrix chat CLI command)
    ↓
v0.2.1 complete
```

> **Why this is a minor release and not part of v0.3.0**: The human participation primitive is architecturally independent of channels (RFC 0011). It reuses the RFC 0005 memory and dispatch system without modification. Shipping it as v0.2.1 gives v0.2.0 users something immediately useful and generates real-world feedback on persona behavior before the larger v0.3.0 channel work begins.

### Planned Components (v0.2.1)

| Component | Go Package | Python Module | Target RFC |
|-----------|-----------|---------------|------------|
| `Participant` Protocol + `UserParticipant` | — | `agents/participant.py` | 0016 | ✅ Complete (PR #119) |
| Memory generalization | — | `agents/memory/relationship.py`, `agents/memory/migrations.py` | 0016 | ✅ Complete (PR #120) |
| Chat REST endpoint | `internal/server/` | — | 0016 | ✅ Complete (PR #123) |
| Chat gRPC dispatch | `internal/executor/` | `agents/server_servicers.py` | 0016 | ✅ Complete (PR #121) |
| `persatrix chat` CLI | — | — | 0016 (`cli/src/commands/chat.rs`) | ✅ Complete (PR #125) |

---

## v0.2.2 — Bounded Persona Memory ✅ Complete

**What a user can do**: Persona agents now operate with a deterministic per-event memory token budget — predictable context size, lower per-tick cost, and no more silent spending when nothing is happening.

### What ships in v0.2.2

- **`MemoryBudget` allocator** — per-event token ceiling distributing the available budget across episodic, relationship, and notes tiers (RFC 0017 §B)
- **`_inject_memory_context` rewrite** — allocate-loop replaces ad-hoc injection; budget tracked uniformly across all memory types (RFC 0017 §C)
- **`min_score` relevance threshold** — `EpisodicMemory.recall` / `recall_notes` accept `min_score` and filter out low-scoring matches before injection; legacy opaque gates removed (RFC 0017 §D)
- **Empty-context TICK short-circuit** — when a TICK fires with no admitted memory, no active goal, and no pending conversation turn, the LLM call is skipped and `idle_count` is incremented (RFC 0017 §F)

### What does not ship in v0.2.2

- Operator-facing config for `_MEMORY_BUDGET_TOKENS` — per-event budget constant is not yet exposed as a per-agent config field; deferred pending demand
- RFC 0008 (Memory & Context Optimization) — structural prerequisite now met; RFC 0008 implementation planned for v0.3.0

### RFC Scope

| RFC | Title | Status | PRs | Merged |
|-----|-------|--------|-----|--------|
| [0017](docs/rfcs/0017-persona-memory-injection-budget.md) | Persona Memory Injection Token Budget | ✅ Implemented | 7 | 7/7 |

### Dependency Chain (v0.2.2)

```
v0.2.1 complete (RFC 0016 ✅)
    ↓
RFC 0017 Phase B (MemoryBudget allocator + allocate-loop rewrite)
    ↓
RFC 0017 Phase D (min_score relevance threshold)
    ↓
RFC 0017 Phase F (empty-context TICK short-circuit)
    ↓
v0.2.2 complete
```

---

## v0.2.3 — Observability Foundation ✅ Complete

**What a user can do**: Observe your agent society end-to-end — structured JSON logs on a versioned schema across Go, Python, and CLI; distributed traces from REST handler to LLM call with OTEL Gen-AI semantic conventions; OTLP metrics with histogram exemplars; W3C Baggage propagation across the gRPC boundary; a tail-sampling Collector pipeline. Combined deliverable of RFCs 0018 + 0019.

### What ships in v0.2.3

- **`internal/observability/` Go package** — `internal/telemetry/` renamed verbatim; all OTEL instrumentation consolidated under the new name (RFC 0019 PR 1)
- **Python OTEL initialisation** — `agents/observability/tracing.py` with `init_tracing()` / `shutdown()`, Resource attributes, `BatchSpanProcessor`, and a `CompositePropagator(TraceContext + Baggage)` registered globally (RFC 0019 PR 1)
- **gRPC trace + baggage propagation** — Go executor injects `otelgrpc` client handler; Python server registers `GrpcInstrumentorServer`; baggage entries readable inside handlers (RFC 0019 PR 1)
- **otelhttp handler wrap** — orchestrator HTTP handler wrapped with `otelhttp.NewHandler` (RFC 0019 PR 1)
- **Semantic spans** — tick loop, event dispatch, memory ops, LLM calls (Gen-AI conventions), tool execution (RFC 0019 PR 2)
- **Span Links** — A2A and sub-agent causality (RFC 0019 PR 2)
- **OTLP metrics** — counters, histograms, gauges with exemplars on both Go and Python sides (RFC 0019 PR 3)
- **Structured JSON logs** — Go (zap) and Python (structlog) on a versioned schema with a log-record redactor surface (RFC 0018 PRs 1–2)
- **Log↔trace correlation** — structlog/zap enricher writes `trace_id` + `span_id` + known baggage entries into every log record (RFC 0018 PR 3)
- **Collector tail-sampling pipeline** — reference `config/observability/otel-collector.yaml`; docker-compose adds Collector, Prometheus, Loki (dev) (RFC 0019 PR 4)
- **`persatrix logs` CLI rewrite** — `--follow`, server-side filters, `--trace <id>` correlation (RFC 0018 PR 6)

### What does not ship in v0.2.3

- Distributed mesh telemetry (v0.6.0)
- Per-agent operator dashboard / alerting rules

### RFC Scope

| RFC | Title | Status | PRs | Merged |
|-----|-------|--------|-----|--------|
| [0019](docs/rfcs/0019-opentelemetry-completion.md) | OpenTelemetry Completion | ✅ Implemented | 5 | 5/5 |
| [0018](docs/rfcs/0018-structured-logging-framework.md) | Structured Logging Framework | ✅ Implemented | 7 | 7/7 |

### Joint Merge Order (RFCs 0018 + 0019)

```
0019 PR 1 (Phase 1 — telemetry→observability rename + Python OTEL init + gRPC + Baggage)  ✅ #163
  ↓
0018 PR 1 (Phase 1 — Python structlog + schema doc + redactor surface)  ✅ #164
  ↓
0018 PR 2 (Phase 2 — Go zap rename + pretty + redactor wired + source)  ✅ #165
  ↓
0019 PR 2 (Phase 2 — semantic spans + Span Links)  ✅ #167
  ↓
0018 PR 3 (Phase 3 — cross-process correlation + OTEL trace IDs on logs)  ✅ #168
  ↓
0019 PR 3 (Phase 3a — metrics)  ✅ #170
  ↓
0019 PR 4 (Phase 3b — Collector + docker-compose + E2E + schema-parity test)  ✅ #171
  ↓
0018 PR 4 (Phase 4a — proto/log_service.proto + ring buffer + disk store)  ✅ #172
  ↓
0018 PR 5 (Phase 4b — LogService server + agent shipper + REST + SSE)  ✅ #173
  ↓
0018 PR 6 (Phase 4c — CLI rewrite + E2E)  ✅ #174
  ↓
0018 PR 8 + 0019 PR 6 (post-merge polish — logbuffer/shipper + tracing/spans cluster)  ✅ #177 (0018 PR 8) + ✅ #176 (0019 PR 6)
  ↓
0018 PR 7 + 0019 PR 5 (review follow-ups + RFC close, opened together as a paired closeout)  ✅ #180 (0018 PR 7) + ✅ #181 (0019 PR 5)
```

### Planned Components (v0.2.3)

| Component | Go Package | Python Module | Target RFC |
|-----------|-----------|---------------|------------|
| OTEL traces + gRPC propagation | `internal/observability/` | `agents/observability/tracing.py` | 0019 PR 1 ✅ |
| Semantic spans + Span Links | `internal/observability/` | `agents/observability/` | 0019 PR 2 ✅ |
| OTLP metrics | `internal/observability/metrics/` | `agents/observability/metrics.py` | 0019 PR 3 ✅ |
| Collector pipeline | `config/observability/` | — | 0019 PR 4 ✅ |
| Structured logs (Python) | — | `agents/observability/logging.py` | 0018 PR 1 ✅ |
| Structured logs (Go) | `internal/observability/zapenc/` | — | 0018 PR 2 ✅ |
| Log↔trace correlation | `internal/observability/` | `agents/observability/` | 0018 PR 3 ✅ |
| Log storage + shipper | `internal/observability/` | `agents/observability/` | 0018 PR 4 ✅ / PR 5 ✅ |
| LogService REST + SSE endpoints | `internal/server/` | `agents/observability/log_shipper.py` | 0018 PR 5 ✅ |
| `persatrix logs` CLI rewrite | `cli/src/commands/logs.rs` | — | 0018 PR 6 ✅ |

---

## v0.3.0 — Agent Conversations ✅ Complete

**What a user can do**: Give agents a shared channel and watch them talk, negotiate, and form opinions about each other over time.

### What ships in v0.3.0

- **Internal channels** — group messages, DMs, threads; agents can address each other and reply (RFC 0011, internal part)
- **Channel history** visible to agents via memory integration
- **Multi-agent conversation routing** — message delivery, acknowledgement, threading
- **Channels CLI + human participation** — `persatrix channel list/join/send/reply/history/watch`; human operators can join channels and observe agent traffic (RFC 0011 Phase 4)
- **Interaction lifecycle** — dialogues (not individual messages) become the unit of episodic memory and summarization; structural + idle-gap boundary detection; per-channel scoping (RFC 0020)
- **Persona temporal awareness — Phase 1** — now-anchor in every prompt, recency-rendered episode recall, last-seen rendering on relationships (RFC 0021 Phase 1)
- **Agent memory and context optimization** — per-step context budget allocation, caller-prepared context packaging, delegation result merge contracts (RFC 0008)
- **Security hardening Phases 1–2** — audit logging, rate limiting, input sanitization (RFC 0009)

### Memory Quality Roadmap

The persona-memory subsystem failed a qualitative review on 2026-05-01 (the "dementia test" — see [memory-quality-roadmap.md](docs/memory-quality-roadmap.md)). The ratified follow-up plan rides v0.3.x and v0.4.0 alongside the six in-flight RFCs *without* expanding v0.3.0 scope. Tracked deliverables:

- **§A — Declarative Facts Tier** ([RFC 0026](docs/rfcs/0026-declarative-facts-tier.md)) — v0.3.x, new RFC.
- **§B — Continuity bridge across interaction close** — v0.3.x, no RFC, single PR.
- **§C — Salience score with use-based reinforcement** — v0.3.x, folded into the [RFC 0008 calibration review](docs/rfcs/0008-calibration-review.md).
- **§D — Outcome-tagged importance** — v0.3.x, resolves [RFC 0020 OQ #6](docs/rfcs/0020-interaction-lifecycle.md#open-questions); pinned in [`0020-pr-plan.md`](docs/rfcs/0020-pr-plan.md).
- **§E — Reflection-driven consolidation** ([RFC 0027](docs/rfcs/0027-reflection-driven-consolidation.md)) — v0.4.0, supersedes draft RFC 0025.
- **§F — Structured "since we last spoke" prompt header** — v0.3.x, single PR following [RFC 0021 P1](docs/rfcs/0021-persona-temporal-awareness.md).
- **§G — Dementia-test manual artifact** ([MT-MEMORY-005](docs/manual-tests/MT-MEMORY-005-dementia-test.md)) — v0.3.0 release-prep gate.

Sequencing and rationale live in [v0.3.0-plan.md §Memory Quality Follow-Ups](docs/v0.3.0-plan.md#memory-quality-follow-ups-v03x-and-beyond).

### RFC Scope

| RFC | Title | Target scope | Status |
|-----|-------|--------------|--------|
| [0008](docs/rfcs/0008-agent-memory-context-optimization.md) | Agent Memory & Context Optimization | Full RFC | ✅ Implemented |
| [0009](docs/rfcs/0009-security-sandboxing.md) | Security & Sandboxing | Phases 1–2 (audit, rate limiting, sanitization) | 🚧 Implementing |
| [0011](docs/rfcs/0011-channels-bridges.md) | Channels + Bridges | Internal channels (Phases 1–4: routing, history, memory integration, CLI/human participation) | ⚠️ Partially Implemented (internal channels — external bridges deferred to v0.5.0) |
| [0020](docs/rfcs/0020-interaction-lifecycle.md) | Interaction Lifecycle | Phases 1–3 (P4 topic-shift deferred) | ✅ Implemented |
| [0021](docs/rfcs/0021-persona-temporal-awareness.md) | Persona Temporal Awareness | Phase 1 only (now-anchor + recency rendering) | ⚠️ Partially Implemented (Phase 1) |

> **Note (2026-05-06)**: RFC 0007 (Conditional & Looped Workflow Control Flow) was originally scoped to v0.3.0 and has been retargeted to v0.4.0. v0.3.0's user-facing promise — *agents talk, negotiate, form opinions* — is conversation infrastructure; conditional/looped workflow control flow is workflow-engine plumbing that pairs with v0.4.0's sub-agent spawning (RFC 0010) and skill-registry (RFC 0014) work, where iterative refinement and branching on child-agent outputs are the load-bearing cases. RFC 0008 (the prerequisite) ships fully in v0.3.0, so the dep is satisfied at v0.4.0-start.

### Dependency Chain (v0.3.0)

```
v0.2.3 complete
    │
    ├── RFC 0020 P1 (Interaction tracker + additive schema)    [no v0.3.0 deps; starts immediately]
    │       │
    │       ├── RFC 0021 P1 (now-anchor + recency rendering)   [consumes 0020 P1's started_at/closed_at; independent of 0008/0011]
    │       │
    │       └── RFC 0020 P2 (summarize-on-close + janitor)     [pairs with RFC 0008 §D — interaction-bounded summarization]
    │
    ├── RFC 0008 (Memory & Context Optimization)               [prerequisite for RFC 0011 P3; coordinates with RFC 0020 P2]
    │       │
    │       └── RFC 0011 — internal channels only              [parallel workstream; P1–2 independent; P3 needs RFC 0008 P2]
    │              │
    │              └── RFC 0011 P3 + RFC 0020 P3 (joint)       [channel memory becomes interaction-scoped]
    │
    └── RFC 0009 P1–2 (Audit, Rate Limiting, Input Sanitization)  [runs throughout — no blocking dependency on 0011/0020]
            ↓
v0.3.0 complete (all five RFC scopes delivered: 0008, 0009 P1–2, 0011 internal, 0020, 0021 P1)
```

#### Why RFC 0020 Phase 1 starts immediately, ahead of RFC 0008 §D

Interactions are the *unit* RFC 0008 will summarize and RFC 0011 will store as channel history. Landing the tracker + schema (Phase 1) first means every multi-turn dialogue is bounded correctly from day one — no per-message episode debt that has to be migrated later. Phase 1 is pure scaffolding (no LLM, no behavior change), so it carries minimal risk and unblocks both RFC 0008's compression pipeline and RFC 0011's memory integration.

#### Why RFC 0020 P2 pairs with RFC 0008 §D

The summarize-on-close hook calls into RFC 0008's compression pipeline. Coordinating delivery avoids an awkward window where RFC 0008 ships per-message summarization that RFC 0020 then has to displace. The interface is small (RFC 0020 emits "interaction closed" events; RFC 0008 §D consumes them as the trigger to compress).

#### Why RFC 0020 P3 is jointly delivered with RFC 0011 P3

Channels multiply the per-message-episode problem by N participants. Per-channel scoping (DM = pair, thread = thread, group = rolling per-channel-per-agent) must land *with* channel memory integration, not after — otherwise the first cut of channel history would inherit the wrong episode granularity.

#### Why RFC 0009 Phases 1–2 run alongside, not before

Audit logging and rate limiting are foundational safety infrastructure with no RFC 0011/0020 dependency. They can develop concurrently and are integrated progressively (rate limiting into channel REST endpoints in RFC 0011 Phase 1; input sanitization into channel message storage in Phase 3). Phases 3–4 (identity tokens, HITL gates) are prerequisites for sub-agent spawning and are deferred to v0.4.0.

#### Why RFC 0021 Phase 1 lands in v0.3.0, with Phases 2–4 deferred to v0.4.0

Phase 1 is small, self-contained, and high-leverage — a now-anchor in the system prompt and recency-rendered recall make every channel conversation under RFC 0011 carry temporal annotation from day one. Without it, RFC 0011 ships a channel-history experience where agents cannot tell whether a recalled exchange happened minutes or weeks ago. Phase 1 depends only on RFC 0020 Phase 1's `started_at` / `closed_at` columns; no other v0.3.0 RFC blocks or is blocked by it. Phases 2–4 (commitments, REMINDER event, duration calibration) are a coherent forward-memory + estimation surface that pairs naturally with v0.4.0's organizational and skill-registry work — agents that can plan are also agents that can hold roles.

### Planned Components (v0.3.0)

| Component | Go Package | Python Module | Target RFC |
|-----------|-----------|---------------|------------|
| Agent Memory & Context Optimization | `internal/scheduler/`, `internal/executor/` | `agents/memory/`, `agents/task_agent.py` | 0008 |
| Security & Sandboxing (P1–2) | `internal/security/` | `agents/security.py` | 0009 |
| Internal Channels | `internal/channels/`, `internal/executor/` | `agents/server_servicers.py`, `agents/dispatch.py`, `agents/persona_types.py`, `agents/memory/` | 0011 |
| Channels CLI (Rust) | `cli/src/commands/channel.rs`, `cli/src/main.rs` | — | 0011 |
| Interaction Lifecycle | — | `agents/memory/interactions.py`, `agents/memory/episodic.py`, `agents/memory/relationship.py`, `agents/memory/relationship_mutations.py`, `agents/persona_runtime/`, `agents/dispatch.py` | 0020 |
| Persona Temporal Awareness (P1) | — | `agents/clock.py`, `agents/temporal/`, `agents/persona_runtime/prompt_assembly.py`, `agents/persona_runtime/memory_context.py`, `agents/memory/relationship.py` | 0021 |
| Observability (spans + metrics) | `internal/observability/` | `agents/observability/` | 0019 |

---

## v0.4.0 — Agent Organizations

**What a user can do**: Define a company, research lab, or team with roles and hierarchy — and let it run.

### What ships in v0.4.0

- **Organizational topologies** — hierarchy, flat, matrix; authority rules and escalation paths (RFC 0012 partial)
- **Sub-agent spawning** — ephemeral agents with narrowed, orchestrator-issued permission tokens (RFC 0010)
- **Security Phases 3–4** — tool validation, agent identity tokens, HITL gates (RFC 0009)
- **Skill Registry** — `SkillSpec` model, `SkillCatalogue`, skill validation, failure modes, fallback chains (RFC 0014)
- **Meeting and negotiation protocol scaffolding** (RFC 0012 partial)
- **Persona temporal awareness — Phases 2–4** — commitments memory class with `due_at` lifecycle, `REMINDER` tick-loop event, time-tool surface (`get_current_time`, `time_since`, `time_until`, `set_reminder`), duration calibration store with `recall_typical_duration` (RFC 0021 Phases 2–4)
- **Conditional and looped workflow control flow** — skip semantics, bounded repeat-until, for-each (RFC 0007) — retargeted from v0.3.0; pairs with sub-agent spawning and skill-registry work where iterative refinement and conditional branching on child-agent outputs are the load-bearing cases

### RFC Scope

| RFC | Title | Target scope | Status |
|-----|-------|--------------|--------|
| [0007](docs/rfcs/0007-conditional-looped-workflow-control-flow.md) | Conditional & Looped Workflow Control Flow | Full RFC (retargeted from v0.3.0 on 2026-05-06) | 📋 Proposed |
| [0009](docs/rfcs/0009-security-sandboxing.md) | Security & Sandboxing | Phases 3–4 (identity tokens, HITL gates) | 📋 Proposed |
| 0010 | Sub-Agent Spawning | Full RFC | Not yet written |
| 0012 | Protocols + Organizations | Partial: org topologies, authority, spawning | Not yet written |
| [0014](docs/rfcs/0014-agent-skill-registry-lifecycle.md) | Agent Skill Registry & Lifecycle | Full RFC | 📋 Proposed |
| [0021](docs/rfcs/0021-persona-temporal-awareness.md) | Persona Temporal Awareness | Phases 2–4 (commitments, REMINDER event, duration calibration) | 📋 Proposed |
| [0027](docs/rfcs/0027-reflection-driven-consolidation.md) | Reflection-Driven Consolidation | Full RFC (supersedes draft RFC 0025) | 📋 Proposed |

### Dependency Chain (v0.4.0)

```
v0.3.0 complete (RFC 0008 fully delivered)
    │
    ├── RFC 0009 Phases 3–4 (identity tokens, HITL)       [builds on P1–2 from v0.3.0]
    │       │
    │       ├── RFC 0014 Phases 1–2 (skill registry + validation)  [depends on RFC 0009 P1; runs alongside P3–4]
    │       │       ↓
    │       │   RFC 0014 Phase 3 (SkillGrant + lifecycle)           [prerequisite for RFC 0010]
    │       │       ↓
    │       │   RFC 0010 (Sub-Agent Spawning)                       [depends on RFC 0008, RFC 0009 all phases, RFC 0014]
    │       │       ↓
    │       │   RFC 0012 partial (org topologies + authority)       [depends on RFC 0010]
    │
    └── RFC 0007 (Conditional & Looped Control Flow)        [parallel workstream; depends on RFC 0008 from v0.3.0; pairs with RFC 0010/0014 use cases]
            ↓
v0.4.0 complete
```

> **Why RFC 0014 before RFC 0010**: The skill registry is the capability-management layer RFC 0010 depends on for routing. When spawning a sub-agent the orchestrator uses `SkillCatalogue` to select and narrow the child's capabilities via `SkillGrant` records. RFC 0014 Phase 3 must land before RFC 0010's dynamic skill injection semantics are implemented.

> **Why RFC 0009 Phases 3–4 before RFC 0010**: Sub-agent spawning creates recursive execution paths. The capability token model (RFC 0009 Phase 4) ensures spawned agents receive narrowed, orchestrator-issued tokens rather than inheriting parent capabilities — a hard prerequisite for safe sub-agent scoping.

> **Why RFC 0007 lands in v0.4.0 (retargeted from v0.3.0 on 2026-05-06)**: v0.3.0's user-facing promise is *agents talk, negotiate, and form opinions over time* — channel infrastructure, conversation routing, persona memory. Conditional/looped workflow control flow does not serve that promise; it is workflow-engine plumbing. v0.4.0 is where it earns rent: sub-agent spawning (RFC 0010) introduces parent → child orchestration patterns where iterative refinement (`repeat_until` until child output passes review), branching on child status (`condition` on child results), and parallel fan-out (`for_each` over a child population) are the load-bearing primitives. RFC 0008 is the only hard dep and ships fully in v0.3.0 — by v0.4.0-start the prerequisite is satisfied. Parallel to the RFC 0009 → 0014 → 0010 chain; no blocking edge into RFC 0010.

### Planned Components (v0.4.0)

| Component | Go Package | Python Module | Target RFC |
|-----------|-----------|---------------|------------|
| Condition Evaluation | `internal/scheduler/` | — | 0007 |
| Workflow Loops | `internal/scheduler/`, `internal/planner/` | — | 0007 |
| Security & Sandboxing (P3–4) | `internal/security/` | `agents/security.py` | 0009 |
| Skill Registry & Lifecycle | `internal/registry/` | `agents/skills/` | 0014 |
| Sub-agents | — | `agents/sub_agents/` | 0010 |
| MCP Tools | `internal/mcp/` | `agents/tools/mcp_bridge.py` | 0010 |
| Organizations (partial) | `internal/protocols/` | — | 0012 |
| Persona Temporal Awareness (P2–4) | — | `agents/memory/commitments.py`, `agents/memory/duration.py`, `agents/persona_runtime/__init__.py`, `agents/persona_types.py`, `agents/tools/builtin.py` | 0021 |

---

## v0.5.0 — Connected Agents

**What a user can do**: Bridge your agent society into Slack, Discord, or email — agents receive and send real messages.

### What ships in v0.5.0

- **External bridges** — Slack, Discord, Telegram, email connectors (RFC 0011, external part)
- **Full compliance and privacy layer** — data classification, consent tracking, PII detection, right to erasure, ethical guardrails (RFC 0013)
- **RFC 0012 remainder** — meeting and negotiation protocol completion, advanced organizational features
- **Process automation & pattern extraction** — detect repeated reasoning patterns from telemetry, promote them to tested, sandboxed deterministic skills via human review (RFC 0015)

### RFC Scope

| RFC | Title | Target scope | Status |
|-----|-------|--------------|--------|
| 0011 | Channels + Bridges | External bridges | Not yet written |
| 0012 | Protocols + Organizations | Remainder (meeting/negotiation protocols) | Not yet written |
| [0013](docs/rfcs/0013-legal-ethical-compliance.md) | Legal, Ethical & Regulatory Compliance | Full RFC | 📋 Proposed |
| [0015](docs/rfcs/0015-process-automation-pattern-extraction.md) | Process Automation & Pattern Extraction | Full RFC | 📋 Proposed |

> **Why RFC 0013 lands here and not earlier**: Phases 1–2 of RFC 0013 (risk taxonomy, data classification, PII detection) have no RFC 0009 dependency and can develop in parallel with v0.4.0 work. Phases 3–5 (erasure, consent enforcement, audit extensions) depend on RFC 0009's `AuditLogger` and HITL gates. RFC 0013 must be substantially complete before external bridges ship — bridge inputs are the primary vector for external user data entering the system.

> **Why RFC 0015 lands here and not earlier**: RFC 0015 is the learned-skill extraction pipeline deferred by RFC 0014 Open Question 4. It depends on the RFC 0014 Skill Registry (v0.4.0), RFC 0009 sandbox Phases 3–4 (v0.4.0), and RFC 0013 Phase 1 PII detection (v0.5.0) — PII redaction is a hard blocker because candidate records persist representative inputs. v0.5.0 is also when external bridges produce the high-repetition traffic patterns that make automation economically worthwhile.

### Dependency Chain (v0.5.0)

```
v0.4.0 complete (RFC 0014 Skill Registry + RFC 0009 sandbox + RFC 0010 sub-agents)
    ↓
RFC 0013 Phases 1–2 (risk taxonomy, PII detection)   [parallel with v0.4.0]
    ↓
RFC 0013 Phases 3–5 (erasure, consent, audit)        [depends on RFC 0009 P3–4]
    │
RFC 0015 Phase 1 (detection + candidate store)       [depends on RFC 0013 P1, RFC 0014 P4]
    ↓
RFC 0015 Phase 2 (drafter + registration gate)       [depends on RFC 0014 P1]
    ↓
RFC 0015 Phase 3 (deterministic dispatch + sandbox)  [depends on RFC 0009 P3–4, RFC 0014 P2–3]
    │
RFC 0011 external bridges + RFC 0012 remainder       [parallel with RFC 0015 P2–3]
    ↓
RFC 0015 Phase 4 (lifecycle governance + audit)      [depends on RFC 0009 all phases]
    ↓
v0.5.0 complete
```

### Planned Components (v0.5.0)

| Component | Go Package | Python Module | Target RFC |
|-----------|-----------|---------------|------------|
| External Bridges | `internal/bridges/` | — | 0011 |
| Compliance & Privacy | `internal/security/` | `agents/compliance.py` | 0013 |
| Automation Pipeline | `internal/automation/` | `agents/automation/` | 0015 |
| Organizations (remainder) | `internal/protocols/` | — | 0012 |
| Pattern Detection & Candidates | `internal/automation/` | — | 0015 |
| Deterministic Skill Dispatch | — | `agents/automation/`, `agents/skills/executor.py` | 0015 |

---

## v0.6.0 — Distributed Mesh

**What a user can do**: Run agent societies across multiple nodes and networks.

**Design**: Architecture sketched in [persatrix-extension-spec.md](docs/persatrix-extension-spec.md). No RFCs written yet.

### Planned Components (v0.6.0)

| Component | Package | Description |
|-----------|---------|-------------|
| Mesh Networking | `internal/mesh/` | Multi-node peer discovery and communication |
| A2A Protocol | `internal/a2a/` | Agent-to-agent networking across nodes |
| Agent Migration | — | Move agents between nodes for load balancing |
| Data Residency | — | Per-node data controls |

---

## Merged PR History

| PR | Title | RFC | Date |
|----|-------|-----|------|
| [#6](https://github.com/mkhomutov/Persatrix/pull/6) | feat(state): implement InMemoryStateStore | 0001 (1/6) | 2026-04-08 |
| [#7](https://github.com/mkhomutov/Persatrix/pull/7) | feat(registry): implement InMemoryRegistry | 0001 (2/6) | 2026-04-08 |
| [#8](https://github.com/mkhomutov/Persatrix/pull/8) | feat(planner): YAMLPlanner Parse+DAG+Plan | 0001 (3/6) | 2026-04-08 |
| [#9](https://github.com/mkhomutov/Persatrix/pull/9) | feat(planner): ResolveInputs template resolution | 0001 (4/6) | 2026-04-08 |
| [#10](https://github.com/mkhomutov/Persatrix/pull/10) | feat(orchestrator): wire into main.go | 0001 (5/6) | 2026-04-08 |
| [#12](https://github.com/mkhomutov/Persatrix/pull/12) | fix: review findings follow-up | 0001 (6/6) | 2026-04-09 |
| [#14](https://github.com/mkhomutov/Persatrix/pull/14) | feat(server): HTTP scaffolding + workflow handlers | 0002 (1/4) | 2026-04-09 |
| [#16](https://github.com/mkhomutov/Persatrix/pull/16) | feat(server): agent registry endpoints | 0002 (2/4) | 2026-04-09 |
| [#17](https://github.com/mkhomutov/Persatrix/pull/17) | feat(server): stub endpoints + main.go wiring | 0002 (3/4) | 2026-04-09 |
| [#18](https://github.com/mkhomutov/Persatrix/pull/18) | fix: review findings follow-up | 0002 (4/4) | 2026-04-10 |
| [#21](https://github.com/mkhomutov/Persatrix/pull/21) | feat(generated): protobuf Go code generation | 0003 (1/7) | 2026-04-10 |
| [#22](https://github.com/mkhomutov/Persatrix/pull/22) | feat(executor): GRPCExecutor core with retry logic | 0003 (2/7) | 2026-04-10 |
| [#23](https://github.com/mkhomutov/Persatrix/pull/23) | test(executor): retry logic & error classification tests | 0003 (3/7) | 2026-04-10 |
| [#24](https://github.com/mkhomutov/Persatrix/pull/24) | feat(state): RunRetrying, SetRunTimestamps, SetRunError | 0003 (4/7) | 2026-04-10 |
| [#25](https://github.com/mkhomutov/Persatrix/pull/25) | feat(scheduler): WorkflowScheduler core with polling, parallel stages, dedup | 0003 (5/7) | 2026-04-10 |
| [#26](https://github.com/mkhomutov/Persatrix/pull/26) | test(scheduler): step execution, template resolution, error path coverage | 0003 (6/7) | 2026-04-10 |
| [#27](https://github.com/mkhomutov/Persatrix/pull/27) | feat(orchestrator): wire scheduler + executor into main.go | 0003 (7/7) | 2026-04-10 |
| [#28](https://github.com/mkhomutov/Persatrix/pull/28) | docs: add follow-up PRs 6-9 to RFC 0003 PR plan | 0003 docs | 2026-04-10 |
| [#29](https://github.com/mkhomutov/Persatrix/pull/29) | docs(rfc0001): complete PR 6 follow-up scope with carry-forward findings | 0001 docs | 2026-04-10 |
| [#30](https://github.com/mkhomutov/Persatrix/pull/30) | fix(state): replace rune-based test IDs (RFC 0001, F-06) | 0001 follow-up (1/1) | 2026-04-10 |
| [#31](https://github.com/mkhomutov/Persatrix/pull/31) | fix(executor): additive dial options, cancellation & concurrent retry tests | 0003 follow-up (1/4) | 2026-04-10 |
| [#32](https://github.com/mkhomutov/Persatrix/pull/32) | test: observability improvements — concurrent race tests, log assertions, zaptest logger | 0003 follow-up (2/4) | 2026-04-11 |
| [#33](https://github.com/mkhomutov/Persatrix/pull/33) | fix(orchestrator): graceful shutdown drain + absolute workflowsDir | 0003 follow-up (3/4) | 2026-04-11 |
| [#34](https://github.com/mkhomutov/Persatrix/pull/34) | build(proto): split make proto into go/python targets + CI staleness check | 0003 follow-up (4/4) | 2026-04-11 |
| [#35](https://github.com/mkhomutov/Persatrix/pull/35) | docs: RFC 0003/0004 status updates, multi-provider LLM design, v0.2 deferrals | cross-RFC docs | 2026-04-11 |
| [#36](https://github.com/mkhomutov/Persatrix/pull/36) | feat(agents): PermissionGate + PathValidator | 0004 (2/7) | 2026-04-11 |
| [#37](https://github.com/mkhomutov/Persatrix/pull/37) | feat(agents): built-in tools + PR 2 follow-up fixes | 0004 (3/7) | 2026-04-11 |
| [#38](https://github.com/mkhomutov/Persatrix/pull/38) | feat(agents): LLM client + TaskInputConfig + base handle loop | 0004 (4a/7) | 2026-04-11 |
| [#39](https://github.com/mkhomutov/Persatrix/pull/39) | feat(agents): CoderAgent, ReviewerAgent, PlannerAgent | 0004 (4b/7) | 2026-04-11 |
| [#40](https://github.com/mkhomutov/Persatrix/pull/40) | feat(agents): gRPC server + agent loading + proto stubs + follow-up fixes | 0004 (5a/7) | 2026-04-11 |
| [#41](https://github.com/mkhomutov/Persatrix/pull/41) | feat(agents): self-registration + integration tests + follow-up fixes | 0004 (5b/7) | 2026-04-11 |
| [#42](https://github.com/mkhomutov/Persatrix/pull/42) | fix(agents): registration follow-ups + RFC 0004 close | 0004 (6/7) | 2026-04-11 |
| [#44](https://github.com/mkhomutov/Persatrix/pull/44) | fix(lint): resolve all golangci-lint, ruff, mypy, clippy warnings | v0.1 release prep | 2026-04-11 |
| [#45](https://github.com/mkhomutov/Persatrix/pull/45) | docs(rfc): RFC 0005 — Persona Agent & Memory System | 0005 (RFC) | 2026-04-12 |
| [#46](https://github.com/mkhomutov/Persatrix/pull/46) | docs(rfc0005): add PR implementation plan | 0005 (PR plan) | 2026-04-12 |
| [#47](https://github.com/mkhomutov/Persatrix/pull/47) | feat(agents): data-driven TaskAgent + agent type system | 0005 (1a/20) | 2026-04-12 |
| [#48](https://github.com/mkhomutov/Persatrix/pull/48) | feat(cli): wire v0.1 REST endpoints | 0005 (1b/20) | 2026-04-12 |
| [#49](https://github.com/mkhomutov/Persatrix/pull/49) | feat(memory): working memory + token estimation | 0005 (2/20) | 2026-04-12 |
| [#50](https://github.com/mkhomutov/Persatrix/pull/50) | feat(memory): schema migration + episodic memory core | 0005 (3a/20) | 2026-04-12 |
| [#51](https://github.com/mkhomutov/Persatrix/pull/51) | feat(memory): agent-initiated memory tools | 0005 (3b/20) | 2026-04-12 |
| [#52](https://github.com/mkhomutov/Persatrix/pull/52) | feat(memory): episode auto-summarization | 0005 (3c/20) | 2026-04-12 |
| [#53](https://github.com/mkhomutov/Persatrix/pull/53) | feat(memory): relationship memory | 0005 (4/20) | 2026-04-12 |
| [#54](https://github.com/mkhomutov/Persatrix/pull/54) | feat(agents): PersonaAgent runtime core | 0005 (5a/20) | 2026-04-13 |
| [#55](https://github.com/mkhomutov/Persatrix/pull/55) | feat(agents): event dispatch + tick loop integration | 0005 (5b/20) | 2026-04-13 |
| [#56](https://github.com/mkhomutov/Persatrix/pull/56) | feat(agents): config validation + schema wiring | 0005 (6a/20) | 2026-04-13 |
| [#57](https://github.com/mkhomutov/Persatrix/pull/57) | feat(cli): CLI persona commands | 0005 (6b/20) | 2026-04-13 |
| [#58](https://github.com/mkhomutov/Persatrix/pull/58) | docs(rfc0005): split PR 7 into 4 sub-PRs (7a-7d) | 0005 docs | 2026-04-13 |
| [#59](https://github.com/mkhomutov/Persatrix/pull/59) | fix(memory): memory tier review fixes (RFC 0005, PR 7a) | 0005 (7a/20) | 2026-04-13 |
| [#60](https://github.com/mkhomutov/Persatrix/pull/60) | feat(persona,validate): persona + validation review fixes (PR 7b) | 0005 (7b/20) | 2026-04-14 |
| [#61](https://github.com/mkhomutov/Persatrix/pull/61) | docs: add development workflow lifecycle guide | cross-RFC docs | 2026-04-13 |
| [#62](https://github.com/mkhomutov/Persatrix/pull/62) | fix(cli): Rust CLI review fixes (RFC 0005, PR 7c) | 0005 (7c/20) | 2026-04-14 |
| [#63](https://github.com/mkhomutov/Persatrix/pull/63) | license: move repository to BUSL 1.1 | cross-RFC license | 2026-04-14 |
| [#64](https://github.com/mkhomutov/Persatrix/pull/64) | refactor(persona): split persona.py into submodules (RFC 0005, PR 8a) | 0005 (8a/20) | 2026-04-14 |
| [#65](https://github.com/mkhomutov/Persatrix/pull/65) | refactor(persona): extract _LLMPersonaAgent to persona_runtime.py (RFC 0005, PR 8d) | 0005 (8d/20) | 2026-04-14 |
| [#66](https://github.com/mkhomutov/Persatrix/pull/66) | refactor(memory): split episodic.py into focused modules (RFC 0005, PR 8b) | 0005 (8b/20) | 2026-04-14 |
| [#67](https://github.com/mkhomutov/Persatrix/pull/67) | refactor(cli): split main.rs into modules (RFC 0005, PR 8c) | 0005 (8c/20) | 2026-04-14 |
| [#68](https://github.com/mkhomutov/Persatrix/pull/68) | docs: add documentation & diagrams phase to workflow and PR plan (RFC 0005, PR 9) | 0005 (9/20) | 2026-04-14 |
| [#69](https://github.com/mkhomutov/Persatrix/pull/69) | docs: close RFC 0005 — Persona Agent & Memory System (PR 7d, 20/20) | 0005 (7d/20) | 2026-04-14 |
| [#70](https://github.com/mkhomutov/Persatrix/pull/70) | refactor: rename project from Orchestr8 to Persatrix | cross-project rename | 2026-04-14 |
| [#71](https://github.com/mkhomutov/Persatrix/pull/71) | fix: resolve Windows setup, Docker service discovery, and tool schema bugs | cross-RFC bugfix | 2026-04-15 |
| [#72](https://github.com/mkhomutov/Persatrix/pull/72) | docs(rfc): propose RFC 0006 (Efficiency & Execution Limits) and RFC 0007 (Conditional & Looped Control Flow) | 0006+0007 (RFC) | 2026-04-15 |
| [#73](https://github.com/mkhomutov/Persatrix/pull/73) | docs(rfc): add RFC 0008 — Agent Memory & Context Optimization | 0008 (RFC) | 2026-04-15 |
| [#74](https://github.com/mkhomutov/Persatrix/pull/74) | docs(rfc): add RFC 0009 — Agent Identity, Security & Sandboxing | 0009 (RFC) | 2026-04-15 |
| [#75](https://github.com/mkhomutov/Persatrix/pull/75) | docs(rfc0006): resolve open questions, accept RFC | 0006 accept | 2026-04-15 |
| [#76](https://github.com/mkhomutov/Persatrix/pull/76) | docs(rfc0008): resolve open questions and accept RFC | 0008 accept | 2026-04-15 |
| [#77](https://github.com/mkhomutov/Persatrix/pull/77) | docs(rfc): RFC 0013 — Legal, Ethical & Regulatory Compliance Framework | 0013 (RFC) | 2026-04-15 |
| [#78](https://github.com/mkhomutov/Persatrix/pull/78) | docs(rfc0006): add PR implementation plan for Efficiency & Execution Limits | 0006 (PR plan) | 2026-04-16 |
| [#79](https://github.com/mkhomutov/Persatrix/pull/79) | feat: add defaults package, step limit fields, and schema updates (RFC 0006 PR 1a) | 0006 (1a/12) | 2026-04-16 |
| [#80](https://github.com/mkhomutov/Persatrix/pull/80) | docs(rfc): RFC 0014 — Agent Skill Registry & Lifecycle | 0014 (RFC) | 2026-04-16 |
| [#81](https://github.com/mkhomutov/Persatrix/pull/81) | feat: wire execution limits through executor and scheduler (RFC 0006 PR 1b) | 0006 (1b/12) | 2026-04-16 |
| [#82](https://github.com/mkhomutov/Persatrix/pull/82) | docs(roadmap): restructure versioning strategy for release velocity | cross-RFC docs | 2026-04-16 |
| [#83](https://github.com/mkhomutov/Persatrix/pull/83) | feat: implement Python defaults and limit validation (RFC 0006 PR 1c) | 0006 (1c/12) | 2026-04-16 |
| [#84](https://github.com/mkhomutov/Persatrix/pull/84) | feat(executor): derived deadline mode with shared retry budget (RFC 0006 PR 2) | 0006 (2/12) | 2026-04-17 |
| [#85](https://github.com/mkhomutov/Persatrix/pull/85) | feat(cost): implement TokenCounter and BudgetEnforcer (RFC 0006 PR 3a) | 0006 (3a/12) | 2026-04-17 |
| [#86](https://github.com/mkhomutov/Persatrix/pull/86) | feat(cost): CostReporter + scheduler budget integration (RFC 0006 PR 3b) | 0006 (3b/12) | 2026-04-17 |
| [#87](https://github.com/mkhomutov/Persatrix/pull/87) | feat(state): StepExecutionMetadata + observability (RFC 0006 PR 4a) | 0006 (4a/12) | 2026-04-17 |
| [#88](https://github.com/mkhomutov/Persatrix/pull/88) | feat(cost): response cache + cost summary endpoint (RFC 0006 PR 4b) | 0006 (4b/12) | 2026-04-17 |
| [#90](https://github.com/mkhomutov/Persatrix/pull/90) | fix(executor,scheduler,state): RFC 0006 PR 5a — execution follow-up fixes | 0006 (5a/12) | 2026-04-17 |
| [#91](https://github.com/mkhomutov/Persatrix/pull/91) | fix(cost): atomic budget snapshot, BudgetError struct, config validation (RFC 0006 PR 5b) | 0006 (5b/12) | 2026-04-17 |
| [#92](https://github.com/mkhomutov/Persatrix/pull/92) | fix(planner,agents): RFC 0006 PR 5c — Planner/Schema + Python Fixes | 0006 (5c/12) | 2026-04-17 |
| [#93](https://github.com/mkhomutov/Persatrix/pull/93) | fix(agents): surface invalid_fields in negative-limit error metadata + RFC 0006 close | 0006 (6/12) | 2026-04-17 |
| [#94](https://github.com/mkhomutov/Persatrix/pull/94) | docs: add v0.2.0 release preparation plan | v0.2 release prep | 2026-04-17 |
| [#95](https://github.com/mkhomutov/Persatrix/pull/95) | refactor(agents): split persona_runtime.py into package | v0.2 release prep (A-1) | 2026-04-17 |
| [#96](https://github.com/mkhomutov/Persatrix/pull/96) | refactor(scheduler): split scheduler.go into stage_runner.go and budget.go | v0.2 release prep (A-2) | 2026-04-18 |
| [#97](https://github.com/mkhomutov/Persatrix/pull/97) | refactor(agents): split episodic.py + server.py | v0.2 release prep (A-3) | 2026-04-18 |
| [#98](https://github.com/mkhomutov/Persatrix/pull/98) | docs(manual-tests): author v0.1 surface tests (MT-WORKFLOW, MT-CLI, MT-CONFIG, MT-AGENT) | v0.2 release prep (C-1) | 2026-04-18 |
| [#99](https://github.com/mkhomutov/Persatrix/pull/99) | docs(manual-tests): author v0.2 surface tests (MT-PERSONA, MT-MEMORY, MT-COST, MT-INTEGRATION) | v0.2 release prep (C-2) | 2026-04-18 |
| [#102](https://github.com/mkhomutov/Persatrix/pull/102) | docs: README overhaul — badges, quickstart, v0.2 features | v0.2 release prep (B-1) | 2026-04-18 |
| [#103](https://github.com/mkhomutov/Persatrix/pull/103) | docs(guides): persona agents and memory user guide | v0.2 release prep (B-2) | 2026-04-18 |
| [#104](https://github.com/mkhomutov/Persatrix/pull/104) | docs(diagrams): system, component, workflow, persona, memory architecture diagrams | v0.2 release prep (B-3) | 2026-04-18 |
| [#105](https://github.com/mkhomutov/Persatrix/pull/105) | docs(changelog): generate v0.2.0 changelog + preserve unreleased section | v0.2 release prep (B-4) | 2026-04-18 |
| [#106](https://github.com/mkhomutov/Persatrix/pull/106) | docs: v0.2.0 release checklist and pre-tag verification procedure | v0.2 release prep (D-1) | 2026-04-18 |
| [#114](https://github.com/mkhomutov/Persatrix/pull/114) | docs(rfc): RFC 0015 — Process Automation & Pattern Extraction | 0015 (RFC) | 2026-04-19 |
| [#115](https://github.com/mkhomutov/Persatrix/pull/115) | docs(rfcs): correct author attribution across all RFCs and add RFC 0015 | 0015 (RFC) + attribution | 2026-04-19 |
| [#116](https://github.com/mkhomutov/Persatrix/pull/116) | docs(rfc): RFC 0016 — Human Participant & Chat Interface | 0016 (RFC) | 2026-04-19 |
| [#118](https://github.com/mkhomutov/Persatrix/pull/118) | docs(rfc): accept RFC 0016 and add PR implementation plan | 0016 accept | 2026-04-19 |
| [#119](https://github.com/mkhomutov/Persatrix/pull/119) | feat(agents): Participant Protocol + UserParticipant + UserStore | 0016 (1/7) | 2026-04-20 |
| [#120](https://github.com/mkhomutov/Persatrix/pull/120) | feat(agents): generalize RelationshipMemory to participant pairs | 0016 (2/7) | 2026-04-20 |
| [#121](https://github.com/mkhomutov/Persatrix/pull/121) | feat(agents): SendChatMessage gRPC servicer + EventDispatcher flag | 0016 (3/7) | 2026-04-20 |
| [#123](https://github.com/mkhomutov/Persatrix/pull/123) | feat(server): add REST chat endpoint and gRPC chat executor | 0016 (4/7) | 2026-04-20 |
| [#124](https://github.com/mkhomutov/Persatrix/pull/124) | refactor(executor): split executor.go into executor.go + dispatch.go | 0016 refactor | 2026-04-20 |
| [#125](https://github.com/mkhomutov/Persatrix/pull/125) | feat(cli): add `persatrix chat` command and rename binary | 0016 (5/7) | 2026-04-20 |
| [#127](https://github.com/mkhomutov/Persatrix/pull/127) | fix(agents,cli): address PR 1–5 review follow-ups | 0016 (6/7) | 2026-04-20 |
| [#128](https://github.com/mkhomutov/Persatrix/pull/128) | docs(rfc): close RFC 0016 — Human Participant & Chat Interface (PR 7/7) | 0016 (7/7) | 2026-04-20 |
| [#130](https://github.com/mkhomutov/Persatrix/pull/130) | docs(manual-tests): author chat surface tests (MT-CHAT-001..004) | v0.2.1 release prep | 2026-04-20 |
| [#131](https://github.com/mkhomutov/Persatrix/pull/131) | docs(manual-tests): execute v0.2.1 manual test suite, record results | v0.2.1 release prep | 2026-04-21 |
| [#132](https://github.com/mkhomutov/Persatrix/pull/132) | docs(diagrams): architecture diagram refresh for v0.2.1 chat surface | v0.2.1 release prep | 2026-04-21 |
| [#133](https://github.com/mkhomutov/Persatrix/pull/133) | fix(persona-runtime): apply PR #131 deep-review follow-ups | 0016 follow-up | 2026-04-21 |
| [#135](https://github.com/mkhomutov/Persatrix/pull/135) | docs(guide): add chat walkthrough to persona-agents guide | v0.2.1 release prep | 2026-04-21 |
| [#136](https://github.com/mkhomutov/Persatrix/pull/136) | docs(readme): refresh README for v0.2.1 chat surface | v0.2.1 release prep | 2026-04-21 |
| [#137](https://github.com/mkhomutov/Persatrix/pull/137) | docs(release): add v0.2.1 release checklist | v0.2.1 release prep | 2026-04-21 |
| [#138](https://github.com/mkhomutov/Persatrix/pull/138) | chore(release): bump version to 0.2.1 and update changelog | v0.2.1 release prep | 2026-04-21 |
| [#139](https://github.com/mkhomutov/Persatrix/pull/139) | chore(deps): bump rustls-webpki from 0.103.10 to 0.103.12 in /cli | security patch | 2026-04-21 |
| [#140](https://github.com/mkhomutov/Persatrix/pull/140) | chore(release): final pre-tag gate — v0.2.1 complete | v0.2.1 release prep | 2026-04-21 |
| [#141](https://github.com/mkhomutov/Persatrix/pull/141) | docs(release): post-release follow-up for v0.2.1 | v0.2.1 release prep | 2026-04-21 |
| [#142](https://github.com/mkhomutov/Persatrix/pull/142) | docs(rfcs): add RFC 0018 (Structured Logging) and RFC 0019 (OTEL Completion) for v0.2.3 | 0018+0019 (RFC) | 2026-04-21 |
| [#143](https://github.com/mkhomutov/Persatrix/pull/143) | docs(rfcs): accept RFC 0017 — persona memory injection token budget | 0017 accept | 2026-04-21 |
| [#144](https://github.com/mkhomutov/Persatrix/pull/144) | docs(rfcs): add PR plan for RFC 0017 — persona memory injection budget | 0017 (PR plan) | 2026-04-21 |
| [#145](https://github.com/mkhomutov/Persatrix/pull/145) | feat(agents): MemoryBudget allocator + token-aware truncation (RFC 0017 PR 1/7) | 0017 (1/7) | 2026-04-21 |
| [#146](https://github.com/mkhomutov/Persatrix/pull/146) | feat(agents): _inject_memory_context allocate-loop rewrite (RFC 0017 PR 2/7) | 0017 (2/7) | 2026-04-21 |
| [#147](https://github.com/mkhomutov/Persatrix/pull/147) | feat(memory): min_score relevance threshold on recall/recall_notes (RFC 0017 PR 3/7) | 0017 (3/7) | 2026-04-21 |
| [#148](https://github.com/mkhomutov/Persatrix/pull/148) | feat(agents): wire min_score and remove legacy gates (RFC 0017 PR 4/7) | 0017 (4/7) | 2026-04-21 |
| [#149](https://github.com/mkhomutov/Persatrix/pull/149) | fix(agents): short-circuit empty-context TICKs (RFC 0017 PR 5/7) | 0017 (5/7) | 2026-04-21 |
| [#152](https://github.com/mkhomutov/Persatrix/pull/152) | fix(agents): RFC 0017 PR 6 review follow-ups (PR 6/7) | 0017 (6/7) | 2026-04-22 |
| [#153](https://github.com/mkhomutov/Persatrix/pull/153) | docs(rfc): close RFC 0017 status and roadmap | 0017 (close) | 2026-04-22 |
| [#154](https://github.com/mkhomutov/Persatrix/pull/154) | docs(manual-tests): add MT-MEMORY-004 and MT-PERSONA-003 for RFC 0017 | 0017 (7/7) | 2026-04-22 |
| [#155](https://github.com/mkhomutov/Persatrix/pull/155) | test(manual): v0.2.2 execution report and integration test fix for RFC 0017 §F | v0.2.2 release prep | 2026-04-22 |
| [#156](https://github.com/mkhomutov/Persatrix/pull/156) | docs(release): v0.2.2 release checklist + prep plan + README/guide refresh | v0.2.2 release prep | 2026-04-22 |
| [#157](https://github.com/mkhomutov/Persatrix/pull/157) | chore(release): bump to v0.2.2 + curated changelog | v0.2.2 release prep | 2026-04-22 |
| [#158](https://github.com/mkhomutov/Persatrix/pull/158) | docs(release): final pre-tag verification — flip README to Released, check off all gates | v0.2.2 release prep | 2026-04-22 |
| [#163](https://github.com/mkhomutov/Persatrix/pull/163) | feat(otel): telemetry→observability rename + Python OTEL init + gRPC + Baggage (RFC 0019 PR 1/5) | 0019 (1/5) | 2026-04-22 |
| [#164](https://github.com/mkhomutov/Persatrix/pull/164) | feat(observability): RFC 0018 PR 1 — schema doc + Python structlog chain + redactor surface | 0018 (1/7) | 2026-04-22 |
| [#161](https://github.com/mkhomutov/Persatrix/pull/161) | docs(rfcs): add joint PR plans for RFC 0018 + RFC 0019 (v0.2.3 Observability Foundation) | 0018+0019 (PR plan) | 2026-04-22 |
| [#165](https://github.com/mkhomutov/Persatrix/pull/165) | feat(observability): RFC 0018 PR 2 — Go zap rename + pretty + redactor wired + source | 0018 (2/7) | 2026-04-23 |
| [#167](https://github.com/mkhomutov/Persatrix/pull/167) | feat(observability): RFC 0019 PR 2 — semantic spans + Span Links + log↔trace coordination | 0019 (2/5) | 2026-04-23 |
| [#168](https://github.com/mkhomutov/Persatrix/pull/168) | feat(observability): RFC 0018 PR 3 — cross-process correlation IDs + OTEL trace IDs on logs | 0018 (3/7) | 2026-04-23 |
| [#170](https://github.com/mkhomutov/Persatrix/pull/170) | feat(observability): RFC 0019 PR 3 — OTEL metrics (Python + Go) | 0019 (3/5) | 2026-04-23 |
| [#171](https://github.com/mkhomutov/Persatrix/pull/171) | feat(observability): RFC 0019 PR 4 — Collector pipeline + docker-compose + E2E + schema-parity test | 0019 (4/5) | 2026-04-23 |
| [#172](https://github.com/mkhomutov/Persatrix/pull/172) | feat(observability): RFC 0018 PR 4 — Phase 4a log_service.proto + ring buffer + disk store + rate limiter | 0018 (4/7) | 2026-04-23 |
| [#173](https://github.com/mkhomutov/Persatrix/pull/173) | feat(observability): RFC 0018 PR 5 — LogService server + agent shipper + REST + SSE | 0018 (5/7) | 2026-04-23 |
| [#174](https://github.com/mkhomutov/Persatrix/pull/174) | feat(cli): RFC 0018 PR 6 — CLI logs rewrite + filters + SSE follow + E2E | 0018 (6/7) | 2026-04-23 |
| [#175](https://github.com/mkhomutov/Persatrix/pull/175) | docs(rfc-0018,0019): describe closeout PR scope in plans | 0018+0019 (closeout prep) | 2026-04-23 |
| [#176](https://github.com/mkhomutov/Persatrix/pull/176) | refactor(observability): RFC 0019 PR 6 — tracing/spans review follow-ups (optional polish) | 0019 (PR 6, polish) | 2026-04-23 |
| [#177](https://github.com/mkhomutov/Persatrix/pull/177) | perf(observability): RFC 0018 PR 8 — log buffer + shipper polish | 0018 (PR 8, polish) | 2026-04-24 |
| [#180](https://github.com/mkhomutov/Persatrix/pull/180) | docs(rfc-0018): closeout — review follow-ups + status flip (RFC 0018 PR 7) | 0018 (7/7, close) | 2026-04-24 |
| [#181](https://github.com/mkhomutov/Persatrix/pull/181) | docs(rfc-0019): closeout — review follow-ups + status flip (RFC 0019 PR 5) | 0019 (5/5, close) | 2026-04-24 |
| [#182](https://github.com/mkhomutov/Persatrix/pull/182) | fix(observability): issue #179 Should-Fix correctness cluster (sentinel collision + timestamp policy + SSE write deadline) | 0018+0019 follow-up | 2026-04-24 |
| [#183](https://github.com/mkhomutov/Persatrix/pull/183) | fix(observability): issue #178 zap encoder correctness cluster (Must-style ctor + reserved-key shadowing) | 0018 follow-up | 2026-04-24 |
| [#184](https://github.com/mkhomutov/Persatrix/pull/184) | fix(observability): tee orchestrator zap entries into log buffer (MT-LOGS-001) | 0018 follow-up | 2026-04-24 |
| [#185](https://github.com/mkhomutov/Persatrix/pull/185) | docs(mt-otel-001): align walkthrough with current stack + surface propagation gap | 0019 follow-up | 2026-04-24 |
| [#186](https://github.com/mkhomutov/Persatrix/pull/186) | docs(release): add v0.2.3 release preparation plan | v0.2.3 release prep | 2026-04-24 |
| [#187](https://github.com/mkhomutov/Persatrix/pull/187) | docs(release): v0.2.3 MT execution report + release-prep fixes (PR 1) | v0.2.3 release prep | 2026-04-24 |
| [#188](https://github.com/mkhomutov/Persatrix/pull/188) | feat(docker): wire persona agent ember-owl into compose stack | cross-RFC docker | 2026-04-24 |
| [#189](https://github.com/mkhomutov/Persatrix/pull/189) | docs(release): v0.2.3 docs refresh + observability diagram + release checklist (PR 2) | v0.2.3 release prep | 2026-04-24 |
| [#190](https://github.com/mkhomutov/Persatrix/pull/190) | chore(release): bump to v0.2.3 + curate changelog (PR 3) | v0.2.3 release prep | 2026-04-24 |
| [#191](https://github.com/mkhomutov/Persatrix/pull/191) | chore(release): v0.2.3 final pre-tag verification (PR 4) | v0.2.3 release prep | 2026-04-24 |
| [#214](https://github.com/mkhomutov/Persatrix/pull/214) | feat(memory): RFC 0020 PR 1 — InteractionTracker + episodes schema v5 | 0020 (1/7) | 2026-04-27 |
| [#215](https://github.com/mkhomutov/Persatrix/pull/215) | feat(memory): RFC 0020 PR 2 — route TICK + tool-only events through InteractionTracker | 0020 (2/7) | 2026-04-27 |
| [#216](https://github.com/mkhomutov/Persatrix/pull/216) | feat(memory): RFC 0020 PR 3 — multi-turn aggregation for human-chat + DM | 0020 (3/7) | 2026-04-27 |
| [#218](https://github.com/mkhomutov/Persatrix/pull/218) | feat(rfc0008): PR 1 — context budget allocator + packaging foundation | 0008 (1/6) | 2026-04-27 |
| [#219](https://github.com/mkhomutov/Persatrix/pull/219) | feat(rfc0008): PR 1b — context metrics emission + remaining-budget persistence | 0008 (1b/6) | 2026-04-27 |
| [#220](https://github.com/mkhomutov/Persatrix/pull/220) | feat(rfc0008): PR 2 — MemoryFacade for task agents | 0008 (2/6) | 2026-04-28 |
| [#221](https://github.com/mkhomutov/Persatrix/pull/221) | feat(rfc0008): PR 2a — episodic-tier eviction + PR 2 follow-up findings | 0008 (2a/6) | 2026-04-28 |
| [#222](https://github.com/mkhomutov/Persatrix/pull/222) | feat(rfc0008): PR 3 — delegation contract + merge engine | 0008 (3/6) | 2026-04-28 |
| [#223](https://github.com/mkhomutov/Persatrix/pull/223) | feat(rfc0008): PR 4 — shared pool ACL + provenance | 0008 (4/6) | 2026-04-28 |
| [#224](https://github.com/mkhomutov/Persatrix/pull/224) | feat(rfc0008): PR 3a — delegation metrics + PR 3 follow-up findings | 0008 (3a/6) | 2026-04-28 |
| [#225](https://github.com/mkhomutov/Persatrix/pull/225) | feat(rfc0008): PR 5 — confidence decay + procedural revalidation | 0008 (5/6) | 2026-04-29 |
| [#226](https://github.com/mkhomutov/Persatrix/pull/226) | docs(rfc0008): triage accumulated PR 1-5 follow-ups before RFC close | 0008 (triage) | 2026-04-29 |
| [#227](https://github.com/mkhomutov/Persatrix/pull/227) | feat(rfc0008): PR 6a — Go scheduler hygiene + sampler bookkeeping | 0008 (6a/6) | 2026-04-29 |
| [#228](https://github.com/mkhomutov/Persatrix/pull/228) | feat(rfc0008): PR 6b — Python procedural memory + log-safety cleanup | 0008 (6b/6) | 2026-04-29 |
| [#313](https://github.com/mkhomutov/Persatrix/pull/313) | docs(rfc0008): PR 6 — review follow-ups absorbed + RFC close | 0008 (6/6) | 2026-05-10 |
| [#229](https://github.com/mkhomutov/Persatrix/pull/229) | feat(rfc0020): PR 4 — summarization-on-close + janitor + record_interaction move | 0020 (4/7) | 2026-04-29 |
| [#231](https://github.com/mkhomutov/Persatrix/pull/231) | feat(rfc0011): PR 1 — channel store + SQLite migration + schema rewrite | 0011 (1/8) | 2026-04-29 |
| [#233](https://github.com/mkhomutov/Persatrix/pull/233) | feat(rfc0009): PR 1 — AuditLogger + SecretRedactor (package + unit tests) | 0009 (1/4) | 2026-04-29 |
| [#234](https://github.com/mkhomutov/Persatrix/pull/234) | feat(rfc0009): PR 1b — orchestrator wiring + integration tests + observability docs | 0009 (1b/4) | 2026-04-30 |
| [#236](https://github.com/mkhomutov/Persatrix/pull/236) | feat(rfc0009): PR 1c — RedactStruct hardening + audit OTEL metrics | 0009 (1c/4) | 2026-05-01 |
| [#244](https://github.com/mkhomutov/Persatrix/pull/244) | feat(rfc0009): PR 2 — RateLimiter + CircuitBreaker + REST/gRPC middleware + unquarantine endpoint | 0009 (2/4) | 2026-05-04 |
| [#245](https://github.com/mkhomutov/Persatrix/pull/245) | feat(rfc0011): PR 2 — channels REST + ChannelRouter + config reconciliation | 0011 (2/8) | 2026-05-04 |
| [#246](https://github.com/mkhomutov/Persatrix/pull/246) | feat(rfc0011): PR 3 — proto + RPC for ChannelMessageEvent | 0011 (3/8) | 2026-05-04 |
| [#247](https://github.com/mkhomutov/Persatrix/pull/247) | docs(rfc): amend RFC 0011 with chat-as-DM unification (RFC 0016 reconciliation) | 0011 docs (amendment) | 2026-05-04 |
| [#248](https://github.com/mkhomutov/Persatrix/pull/248) | feat(rfc0011): PR 4a-i — ReceiveChannelMessage real handler + additive enums | 0011 (4a-i/8) | 2026-05-05 |
| [#249](https://github.com/mkhomutov/Persatrix/pull/249) | feat(rfc0011): PR 4a-ii-α — hard rename CHANNEL_MESSAGE/SEND_CHANNEL_MESSAGE + SF-3 mentions validation | 0011 (4a-ii-α/8) | 2026-05-05 |
| [#250](https://github.com/mkhomutov/Persatrix/pull/250) | feat(rfc0011): PR 4a-ii-β-1 — real Go gRPC MessageDispatcher + Python REST publish rewire | 0011 (4a-ii-β-1/8) | 2026-05-05 |
| [#251](https://github.com/mkhomutov/Persatrix/pull/251) | feat(rfc0011): PR 4a-ii-β-2 — chat-as-DM rewrite (Go-side waiter + PublishAndAwait) | 0011 (4a-ii-β-2/8) | 2026-05-05 |
| [#266](https://github.com/mkhomutov/Persatrix/pull/266) | refactor(rfc0020): PR 6 slice 1 — PR-4 review #20–#30 (Phase-2/janitor write race + janitor.failed counter + assert→guard) | 0020 (6/7 — slice 1) | 2026-05-07 |
| [#296](https://github.com/mkhomutov/Persatrix/pull/296) | feat(rfc0020): PR 6 slice 2 — typed CloseReason + table-driven _emit_closed dispatch | 0020 (6/7 — slice 2) | 2026-05-08 |
| [#297](https://github.com/mkhomutov/Persatrix/pull/297) | refactor(rfc0020): PR 6 slice 3 — migration no-op cleanup + autouse metrics fixture | 0020 (6/7 — slice 3) | 2026-05-08 |
| [#298](https://github.com/mkhomutov/Persatrix/pull/298) | refactor(rfc0020): PR 6 slice 4 — PR-2 review #6/#7/#9/#10/#11 + episode-routing mixin extraction | 0020 (6/7 — slice 4) | 2026-05-08 |
| [#299](https://github.com/mkhomutov/Persatrix/pull/299) | refactor(rfc0020): PR 6 slice 5 — clock seam + cross-scope idle-flush attribution | 0020 (6/7 — slice 5) | 2026-05-08 |
| [#300](https://github.com/mkhomutov/Persatrix/pull/300) | refactor(rfc0020): PR 6 slice 6 — inline MaxTurns cap + multi-turn close-path coverage | 0020 (6/7 — slice 6) | 2026-05-09 |
| [#301](https://github.com/mkhomutov/Persatrix/pull/301) | refactor(rfc0020): PR 6 slice 7 — tighten _llm_client to LLMClient + drop dead silent-drop branches | 0020 (6/7 — slice 7) | 2026-05-09 |
| [#252](https://github.com/mkhomutov/Persatrix/pull/252) | feat(rfc0011): PR 4b — channels response gate + DELETE endpoints | 0011 (4b/8) | 2026-05-05 |
| [#253](https://github.com/mkhomutov/Persatrix/pull/253) | feat(rfc0009): PR 3 — InputSanitizer + ContextItem + external_data envelope | 0009 (3/4) | 2026-05-05 |
| [#256](https://github.com/mkhomutov/Persatrix/pull/256) | feat(rfc0021p1): PR 1 — Clock seam + temporal rendering pure functions | 0021 P1 (1/3) | 2026-05-06 |
| [#260](https://github.com/mkhomutov/Persatrix/pull/260) | feat(rfc0021p1): PR 2 — now-anchor + episode/relationship recency rendering | 0021 P1 (2/3) | 2026-05-06 |
| [#261](https://github.com/mkhomutov/Persatrix/pull/261) | feat(rfc0021p1): PR 3 — review follow-ups + RFC Phase-1 close | 0021 P1 (3/3) | 2026-05-06 |
| [#262](https://github.com/mkhomutov/Persatrix/pull/262) | feat(rfc0020): PR 5 — per-channel scoping + closing-row recall filter | 0020 (5/7) | 2026-05-06 |
| [#263](https://github.com/mkhomutov/Persatrix/pull/263) | feat(rfc0011): PR 5 — channel ingest sanitization + gate-suppress memory | 0011 (5/8) | 2026-05-07 |
| [#264](https://github.com/mkhomutov/Persatrix/pull/264) | feat(rfc0011): PR 5 follow-up — channel-history tier in MemoryBudget | 0011 (5/8 follow-up) | 2026-05-07 |
| [#265](https://github.com/mkhomutov/Persatrix/pull/265) | feat(rfc0011): PR 5 follow-up — on-startup catch-up fetch (OQ #8) | 0011 (5/8 follow-up) | 2026-05-07 |
| [#267](https://github.com/mkhomutov/Persatrix/pull/267) | chore(rfc0021p1): close #261 review follow-ups (ISSUE-0042/0043/0044/0045) | 0021 P1 follow-up | 2026-05-07 |
| [#268](https://github.com/mkhomutov/Persatrix/pull/268) | chore(rfc0011): close ISSUE-0028/0030/0031 — channels dispatcher observability + test gaps | 0011 follow-up | 2026-05-07 |
| [#269](https://github.com/mkhomutov/Persatrix/pull/269) | chore(rfc0011): close ISSUE-0010/0011/0013 — PR #245 review follow-ups | 0011 follow-up | 2026-05-07 |
| [#270](https://github.com/mkhomutov/Persatrix/pull/270) | fix(security): close ISSUE-0001 — CircuitBreaker rejects Window/Count <= 0; add Disabled flag | 0009 follow-up | 2026-05-07 |
| [#271](https://github.com/mkhomutov/Persatrix/pull/271) | chore(rfc0009): close ISSUE-0006 — WARN on invalid SECURITY_RATE_LIMIT_* env values | 0009 follow-up | 2026-05-07 |
| [#272](https://github.com/mkhomutov/Persatrix/pull/272) | fix(security): close ISSUE-0007 — propagate request ctx through RateLimiter/CircuitBreaker audit emits | 0009 follow-up | 2026-05-07 |
| [#273](https://github.com/mkhomutov/Persatrix/pull/273) | docs(security): close ISSUE-0002 — align GRPCRateLimitInterceptor godoc with grpc.SetHeader + add client-side contract test | 0009 follow-up | 2026-05-07 |
| [#274](https://github.com/mkhomutov/Persatrix/pull/274) | perf(security): close ISSUE-0003 — RateLimiter.evictOlderThan in-place compaction | 0009 follow-up | 2026-05-07 |
| [#275](https://github.com/mkhomutov/Persatrix/pull/275) | security(server): close ISSUE-0004 — hash bearer token before constant-time compare | 0009 follow-up | 2026-05-07 |
| [#276](https://github.com/mkhomutov/Persatrix/pull/276) | fix(channels): close ISSUE-0034 — demote chat-DM user to RespondNever | 0011 follow-up | 2026-05-07 |
| [#277](https://github.com/mkhomutov/Persatrix/pull/277) | fix(agents): close ISSUE-0027 — symmetrize SEND_CHANNEL_MESSAGE result dicts | 0011 follow-up | 2026-05-08 |
| [#278](https://github.com/mkhomutov/Persatrix/pull/278) | test(proto): close ISSUE-0021 — pin ChannelMessageEvent + TaskAck wire shape | 0011 follow-up | 2026-05-08 |
| [#279](https://github.com/mkhomutov/Persatrix/pull/279) | fix(docker): close ISSUE-0046 + ISSUE-0047 — get compose stack functional for v0.3.0 | cross-RFC docker | 2026-05-08 |
| [#280](https://github.com/mkhomutov/Persatrix/pull/280) | feat(channels): close ISSUE-0015 — paginate ListChannels via keyset cursor | 0011 follow-up | 2026-05-08 |
| [#281](https://github.com/mkhomutov/Persatrix/pull/281) | fix(agents): close ISSUE-0026 — sticky-disable HTTPChannelPublisher on first 503 | 0011 follow-up | 2026-05-08 |
| [#282](https://github.com/mkhomutov/Persatrix/pull/282) | fix(agents): close ISSUE-0048 — synthesise SEND_CHANNEL_MESSAGE for plain-text persona replies | 0011 follow-up | 2026-05-08 |
| [#283](https://github.com/mkhomutov/Persatrix/pull/283) | perf(channels): close ISSUE-0014 — bounded-concurrency fanout in ChannelRouter | 0011 follow-up | 2026-05-08 |
| [#284](https://github.com/mkhomutov/Persatrix/pull/284) | fix(scripts): close ISSUE-0036 — switch doc_links collector to `git ls-files` | cross-RFC scripts | 2026-05-08 |
| [#285](https://github.com/mkhomutov/Persatrix/pull/285) | security(ratelimit): close ISSUE-0005 — emit rate_limit.reset audit event from RateLimiter.Reset | 0009 follow-up | 2026-05-08 |
| [#286](https://github.com/mkhomutov/Persatrix/pull/286) | feat(channels): ISSUE-0032 — emit channel.dispatch OTel span (Go side) | 0011 follow-up | 2026-05-08 |
| [#287](https://github.com/mkhomutov/Persatrix/pull/287) | feat(agents): close ISSUE-0032 — emit channel.publish OTel span (Python side) | 0011 follow-up | 2026-05-08 |
| [#288](https://github.com/mkhomutov/Persatrix/pull/288) | build(proto): close ISSUE-0017 — auto-generate agents/generated/*.pyi via mypy-protobuf | cross-RFC build | 2026-05-08 |
| [#289](https://github.com/mkhomutov/Persatrix/pull/289) | build(proto): close ISSUE-0023 — gate proto/ source-of-truth (Python freshness + orphan detection) | cross-RFC build | 2026-05-08 |
| [#290](https://github.com/mkhomutov/Persatrix/pull/290) | test(channels): close ISSUE-0025 — full-chain REST→fanout→gRPC integration test | 0011 follow-up | 2026-05-08 |
| [#291](https://github.com/mkhomutov/Persatrix/pull/291) | docs(proto): close ISSUE-0019 + ISSUE-0022 — TaskAck reuse policy + timestamp format cross-reference | cross-RFC docs | 2026-05-08 |
| [#292](https://github.com/mkhomutov/Persatrix/pull/292) | refactor(orchestrator): close ISSUE-0008 — extract startup helpers, drop main.go below 500 lines | cross-RFC refactor | 2026-05-08 |
| [#293](https://github.com/mkhomutov/Persatrix/pull/293) | refactor(memory): drop file-size grandfather entries — split memory_context, episodic + verify facade | cross-RFC refactor | 2026-05-08 |
| [#294](https://github.com/mkhomutov/Persatrix/pull/294) | fix(channels): close ISSUE-0049 — buildDSN merges caller query params instead of double-? concatenation | 0011 follow-up | 2026-05-08 |
| [#295](https://github.com/mkhomutov/Persatrix/pull/295) | fix(channels): close ISSUE-0050 — soft byte cap on msg.Content at the SQLite store boundary | 0011 follow-up | 2026-05-08 |
| [#302](https://github.com/mkhomutov/Persatrix/pull/302) | feat(rfc0011): PR 6 — Rust CLI channel subcommands (list/join/send/reply/history/watch) | 0011 (6/8) | 2026-05-09 |
| [#303](https://github.com/mkhomutov/Persatrix/pull/303) | docs(rfc0011): PR 7 — Phase 4b human participation MTs + channels guide + diagram | 0011 (7/8) | 2026-05-09 |
| [#304](https://github.com/mkhomutov/Persatrix/pull/304) | docs(rfc0011): PR 8 — internal-scope close (NTH dispatch + status flips) | 0011 (8/8) | 2026-05-09 |
| [#305](https://github.com/mkhomutov/Persatrix/pull/305) | docs(rfc0020): PR 7 — RFC close (status flips for v0.3.0 scope) | 0020 (7/7) | 2026-05-09 |
| [#306](https://github.com/mkhomutov/Persatrix/pull/306) | feat(rfc0009): PR 4 — review follow-ups + Phases 1-2 close | 0009 (4/4) | 2026-05-09 |
| [#307](https://github.com/mkhomutov/Persatrix/pull/307) | docs(rfc0023): introduce LLM call leasing RFC | 0023 (RFC) | 2026-05-09 |
| [#308](https://github.com/mkhomutov/Persatrix/pull/308) | docs(rfc0024): propose event-driven agent scheduling | 0024 (RFC) | 2026-05-10 |
| [#309](https://github.com/mkhomutov/Persatrix/pull/309) | docs(rfc0029): propose personal/society storage split | 0029 (RFC) | 2026-05-10 |
| [#310](https://github.com/mkhomutov/Persatrix/pull/310) | docs(rfc0023): review follow-ups — 3 correctness fixes + 8 clarifications | 0023 follow-up | 2026-05-10 |
| [#311](https://github.com/mkhomutov/Persatrix/pull/311) | docs(v0.3.x): sequence RFCs 0023/0024/0026/0029 across v0.3.1-v0.3.3 | cross-RFC docs | 2026-05-10 |
| [#312](https://github.com/mkhomutov/Persatrix/pull/312) | docs(v030): release-prep plan + walk back RFC 0008 OQ #12 calibration-window gate | v0.3.0 release prep | 2026-05-10 |
| [#314](https://github.com/mkhomutov/Persatrix/pull/314) | test(v030): release-prep PR 1 — manual test execution report + 3 release-prep regression fixes | v0.3.0 release prep | 2026-05-10 |
| [#315](https://github.com/mkhomutov/Persatrix/pull/315) | docs(v030): release-prep PR 2 — README + ROADMAP + guide callouts + diagram refresh + release checklist | v0.3.0 release prep | 2026-05-10 |
| [#316](https://github.com/mkhomutov/Persatrix/pull/316) | feat(v030): demo personas + planning channel + walkthrough guide | cross-RFC v030 | 2026-05-12 |
| [#317](https://github.com/mkhomutov/Persatrix/pull/317) | docs(v030): PR plan for v0.3.0 channel test findings | v0.3.0 test findings | 2026-05-11 |
| [#318](https://github.com/mkhomutov/Persatrix/pull/318) | fix(v030): channel cascade-depth wire propagation — amendment + schemas (PR 1) | v0.3.0 test findings (1/6) | 2026-05-11 |
| [#319](https://github.com/mkhomutov/Persatrix/pull/319) | fix(v030): channel cascade-depth Go orchestrator enforcement (PR 2) | v0.3.0 test findings (2/6) | 2026-05-11 |
| [#320](https://github.com/mkhomutov/Persatrix/pull/320) | docs(rfc0030): propose multi-agent conversation governance | 0030 (RFC) | 2026-05-11 |
| [#321](https://github.com/mkhomutov/Persatrix/pull/321) | fix(v030): channel cascade-depth Python round-trip (PR 3) | v0.3.0 test findings (3/6) | 2026-05-11 |
| [#322](https://github.com/mkhomutov/Persatrix/pull/322) | fix(v030): channel cascade-depth cross-process integration pin (PR 4) | v0.3.0 test findings (4/6) | 2026-05-12 |
| [#323](https://github.com/mkhomutov/Persatrix/pull/323) | fix(v030): channel persona impersonation — grounding clause (PR 5) | v0.3.0 test findings (5/6) | 2026-05-12 |
| [#324](https://github.com/mkhomutov/Persatrix/pull/324) | fix(v030): channel state-reset Make target + operator-guide notes (PR 6) | v0.3.0 test findings (6/6) | 2026-05-12 |
| [#325](https://github.com/mkhomutov/Persatrix/pull/325) | docs(rfcs): RFC 0031 — per-session namespacing for channels + persona memory | 0031 (RFC) | 2026-05-12 |
| [#326](https://github.com/mkhomutov/Persatrix/pull/326) | docs(rfcs): YAML front-matter + auto-generated INDEX.md | cross-RFC docs | 2026-05-12 |
| [#327](https://github.com/mkhomutov/Persatrix/pull/327) | feat(persona): reply-discretion + conversational-pacing prompt snippets | cross-RFC persona | 2026-05-12 |
| [#328](https://github.com/mkhomutov/Persatrix/pull/328) | chore(release): v0.3.0 — version bump + curated changelog + PR 4 pre-tag verification | v0.3.0 release prep | 2026-05-12 |

---

## How to Update This File

This file must be reviewed and updated **during every task**, not just at completion.

### On every task (before starting and after finishing)

1. Verify the **RFC Scope** tables match reality — correct status, correct merged count.
2. Verify the **Component Status** tables — any component you touched should reflect current state.
3. Update the **Last updated** date at the top.

### When a PR is merged

1. Add the PR to the **Merged PR History** table.
2. Increment the merged count in the relevant **RFC Scope** table.
3. If all PRs for an RFC are now merged, change its status to `✅ Implemented` here **and** in the RFC file.
4. Move completed components from "TODO stub" / "🔲 pending" → "✅ Complete" in component tables.
5. Update the **RFC Master Index** table status.

### When starting RFC implementation

1. Change the RFC status to `🚧 Implementing` here **and** in the RFC file (`docs/rfcs/NNNN-*.md`).

### When creating a new RFC

1. Add a row to the **RFC Master Index** table with status `📋 Proposed`.
2. Add a row to the relevant version's **RFC Scope** table.

### When a version ships

1. Update the **Version Map** table status from `🚧 In Progress` → `✅ Complete`.
2. Update the header "Current phase" line.

### Status markers (from [RFC README](docs/rfcs/README.md))

| Status | Marker |
|--------|--------|
| Proposed | 📋 Proposed |
| Accepted | 👍 Accepted |
| Implementing | 🚧 Implementing |
| Implemented | ✅ Implemented |
| Partially Implemented | ⚠️ Partially Implemented |
| Rejected | ❌ Rejected |
| Deferred | 🔮 Deferred |
