# Persatrix Roadmap

> **Last updated**: 2026-04-16  
> **Current phase**: v0.2 (Agent Societies) — 🚧 In Progress  
> **Next milestone**: RFC 0006 (Efficiency & Execution Limits) + RFC 0009 (Security & Sandboxing, Phases 1–2 concurrent)

This document tracks development progress across all phases. Update it when merging PRs or completing milestones.

---

## Phase Overview

| Version | Scope | Status |
|---------|-------|--------|
| **v0.1** | Core engine: orchestrator, task agents, workflows, REST API, gRPC dispatch, tools | ✅ Complete (internal milestone — not publicly released; project renamed to Persatrix before first public release) |
| **v0.2** | Agent societies: personas, channels, protocols, bridges, memory, sub-agents | � In Progress |
| **v0.3** | Distributed mesh: multi-node, A2A protocol, platform integrations | 📋 Planned |
| **v0.4+** | Autonomous agents, simulation controls, web dashboard | 📋 Future |

---

## v0.1 — MVP Core Engine

**Goal**: End-to-end workflow execution — submit a YAML workflow via CLI/REST, orchestrator plans and schedules stages, dispatches tasks to Python agents over gRPC, agents call LLMs and tools, results flow back.

### RFC Tracker

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
v0.1 Complete ─ end-to-end execution working
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
| `internal/security/` | Permission gates, rate limiting, audit logging | 🔲 TODO stub (v0.2+) |
| `internal/telemetry/` | OTEL span instrumentation | 🔲 TODO stub (v0.2+) |
| `internal/cost/` | Token/cost tracking aggregation | 🔲 TODO stub (v0.2+) |

#### Python Agents (`agents/`)

| Module | Purpose | Status |
|--------|---------|--------|
| `agents/base.py` | BaseAgent ABC + dataclasses + LLM loop | ✅ Complete (RFC 0004 PR 4a) |
| `agents/llm_client.py` | Multi-provider LLM client (Anthropic + OpenAI) | ✅ Complete (RFC 0004 PR 4a) |
| `agents/server.py` | gRPC service entry point + self-registration | ✅ Complete (RFC 0004 PR 5a+5b) |
| `agents/task_agent.py` | Data-driven task agent (replaces CoderAgent, ReviewerAgent, PlannerAgent) | ✅ Complete (RFC 0005 PR 1a) |
| `agents/memory/working.py` | Working memory (context window management, priority retention, compression) | ✅ Complete (RFC 0005 PR 2) |
| `agents/memory/episodic.py` | Episodic memory (SQLite, FTS5, episode CRUD, recall, summarization, agent state persistence, delegates notes to NoteStore) | ✅ Complete (RFC 0005 PR 3a+3b+3c, split in PR 8b) |
| `agents/memory/notes.py` | Agent-initiated note storage (NoteStore class, note CRUD, FTS5/LIKE search, pruning) | ✅ Complete (RFC 0005 PR 8b) |
| `agents/memory/migrations.py` | Schema migrations, FTS5 DDL, scoring SQL constants | ✅ Complete (RFC 0005 PR 8b) |
| `agents/memory/relationship.py` | Relationship memory (trust tracking, interaction history, bidirectional decay) | ✅ Complete (RFC 0005 PR 4) |
| `agents/persona.py` | PersonaAgent ABC, create_persona_agent() factory, re-exports | ✅ Complete (RFC 0005 PR 5a+5b, split in PR 8a+8d) |
| `agents/persona_runtime.py` | _LLMPersonaAgent concrete class (LLM-powered event loop, memory injection, tool use) | ✅ Complete (RFC 0005 PR 8d) |
| `agents/persona_types.py` | Persona type definitions (PersonaState, Mood, AgentEvent, EventType, AgentAction, ActionType) | ✅ Complete (RFC 0005 PR 8a) |
| `agents/persona_behavior.py` | Behavioral dimension rendering (render_behavior, DIMENSION_DESCRIPTIONS) | ✅ Complete (RFC 0005 PR 8a) |
| `agents/dispatch.py` | Event dispatch and action execution (EventDispatcher, ActionExecutor) | ✅ Complete (RFC 0005 PR 8a) |
| `agents/tick.py` | Autonomous tick scheduler (TickScheduler) | ✅ Complete (RFC 0005 PR 8a) |
| `agents/validate.py` | Config validation (JSON Schema) | ✅ Complete (RFC 0005 PR 6a) |
| `agents/tools/registry.py` | Tool discovery and registration | ✅ Complete (decorator + registry) |
| `agents/tools/builtin.py` | Built-in tools (file_read, file_write, shell_exec, http_request, memory tools) | ✅ Complete (RFC 0004 PR 3, RFC 0005 PR 3b) |
| `agents/tools/permissions.py` | Deny-by-default permission gate | ✅ Complete (97% coverage) |
| `agents/tools/sandbox.py` | Filesystem path restriction (PathValidator) | ✅ Complete (100% coverage) |
| `agents/generated/` | Python gRPC generated stubs | ✅ Complete (RFC 0004 PR 5a) |

#### Rust CLI (`cli/`)

| Module | Purpose | Status |
|--------|---------|--------|
| `cli/src/main.rs` | CLI entry point, clap definitions, command dispatch | ✅ Functional (RFC 0005 PR 8c split) |
| `cli/src/types.rs` | API request/response types, shared validation helpers | ✅ Complete (RFC 0005 PR 8c) |
| `cli/src/commands/workflow.rs` | Workflow commands (run, status) | ✅ Complete (RFC 0005 PR 8c) |
| `cli/src/commands/agent.rs` | Agent commands (list, info, reload, test persona) | ✅ Complete (RFC 0005 PR 8c) |
| `cli/src/commands/logs.rs` | Execution log viewing | ✅ Complete (RFC 0005 PR 8c) |
| `cli/src/commands/validate.rs` | Config validation (Python subprocess) | ✅ Complete (RFC 0005 PR 8c) |

### What Works Today

1. Submit a workflow via CLI → `POST /api/v1/workflows/run`
2. Orchestrator receives request, planner parses YAML, validates DAG, generates execution plan
3. Server creates `WorkflowRun` in state store → status = Pending
4. Scheduler polls for pending runs, transitions to Running, drives parallel stage execution
5. Executor dispatches tasks to agents via gRPC `ExecuteTask` with retry logic
6. Step outputs resolve across stages via `{{ steps.<key>.output }}` templates
7. Poll `GET /api/v1/workflows/{id}/status` → returns Running/Completed/Failed with step details
8. CRUD operations on agents via REST API

### What's Missing for v0.1

Nothing — all RFC 0004 PRs (7/7) are merged. v0.1 MVP is feature-complete.

> Submitted runs are picked up by the scheduler and driven to completion. All three task agents (CoderAgent, ReviewerAgent, PlannerAgent) are implemented with LLM integration. The gRPC server (AgentServiceServicer) is fully wired with agent loading from YAML config. Agents self-register with the orchestrator at startup and de-register on shutdown. End-to-end gRPC integration tests pass with mock LLM.

---

## v0.2 — Agent Societies

**Goal**: Persona-driven agents with autonomous behavior, multi-channel communication, organizational hierarchy, and persistent memory.

**Design**: Fully specified in [persatrix-extension-spec.md](docs/persatrix-extension-spec.md).

### RFC Tracker

| RFC | Title | Status | PRs | Merged |
|-----|-------|--------|-----|--------|
| [0005](docs/rfcs/0005-persona-agent-memory.md) | Persona Agent & Memory System | ✅ Implemented | 20 | 20/20 |
| [0006](docs/rfcs/0006-efficiency-execution-limits.md) | Efficiency & Execution Limits | 🚧 Implementing | 10 | 0/10 |
| [0007](docs/rfcs/0007-conditional-looped-workflow-control-flow.md) | Conditional & Looped Workflow Control Flow | 📋 Proposed | 0 | 0/0 |
| [0008](docs/rfcs/0008-agent-memory-context-optimization.md) | Agent Memory & Context Optimization | � Accepted | 0 | 0/0 |
| [0009](docs/rfcs/0009-security-sandboxing.md) | Agent Identity, Security & Sandboxing | 📋 Proposed | 0 | 0/0 |
| 0010 | Sub-Agent Spawning | Not yet written | — | — |
| 0011 | Channels + Bridges | Not yet written | — | — |
| 0012 | Protocols + Organizations | Not yet written | — | — |
| [0013](docs/rfcs/0013-legal-ethical-compliance.md) | Legal, Ethical & Regulatory Compliance | 📋 Proposed | 0 | 0/0 |

### Dependency Chain

```
RFC 0005 (PersonaAgent + Memory + TaskAgent)              ✅ Done (20/20)
    ↓
RFC 0006 (Efficiency & Execution Limits)                  📋 Proposed
    ↓
RFC 0008 (Agent Memory & Context Optimization)            � Accepted  [depends on 0005, 0006]
    ↓
RFC 0007 (Conditional & Looped Workflow Control Flow)     📋 Proposed  [depends on 0006, 0008]
    │
RFC 0009 (Agent Identity, Security & Sandboxing)          📋 Proposed  [depends on 0004, 0005; Phases 1–2 alongside 0006]
    ↓
RFC 0013 (Legal, Ethical & Regulatory Compliance)         📋 Proposed  [depends on 0009; Phases 1–2 alongside 0009]
    ↓
RFC 0010 (Sub-Agent Spawning)                             Not yet written
    ↓
RFC 0011 (Channels + Bridges)                             Not yet written
    ↓
RFC 0012 (Protocols + Organizations)                      Not yet written
```

> **Why RFC 0006 before RFC 0008 and RFC 0007**: Loops, delegation, and sub-agent spawning all amplify every existing execution cost weakness. RFC 0006 hardens budget enforcement, deadline derivation, and execution metadata first so that the control-flow and context constructs introduced by RFC 0007 and RFC 0008 cannot produce runaway spend. See [RFC 0006 Motivation](docs/rfcs/0006-efficiency-execution-limits.md#motivation) for the detailed justification.
>
> **Why RFC 0008 before RFC 0007 implementation**: RFC 0008 introduces per-step context budget allocation, caller-prepared context packages, and memory-aware delegation contracts. Landing these before large-scale loop patterns (RFC 0007 implementation) prevents each loop iteration from carrying unbounded prior-step context — the root cause of hallucination risk and token waste in iterative workflows. RFC 0007's `Depends on` reflects this sequencing.
>
> **Why RFC 0009 (Security) runs alongside RFC 0006 and before RFC 0010**: Agent societies dramatically expand the attack surface. RFC 0009 Phases 1–2 (audit logging, rate limiting, input sanitization) can be developed concurrently with RFC 0006. Phases 3–4 (tool validation, agent identity tokens, HITL gates) are prerequisites for sub-agent spawning (RFC 0010) and channel bridge inputs (RFC 0011), which are high-trust injection vectors.
>
> **Why RFC 0010 (Sub-Agent Spawning) after RFC 0008 and RFC 0009**: Sub-agent spawning creates recursive execution paths. RFC 0008's delegation contract and merge semantics (Phase 3) must be in place before production sub-agent patterns are enabled. RFC 0009's capability token model ensures spawned agents receive narrowed, orchestrator-issued tokens rather than inheriting parent capabilities.
>
> **Why RFC 0013 (Legal, Ethical & Regulatory Compliance) alongside RFC 0009**: RFC 0009 establishes the technical security infrastructure (audit logging, HITL gates, capability tokens). RFC 0013 builds the compliance layer on top: data classification, consent tracking, right to erasure, ethical guardrails, and regulatory audit extensions. Phases 1–2 of RFC 0013 (risk taxonomy, data classification, PII detection) have no RFC 0009 dependency and can develop in parallel. Phases 3–5 (erasure, consent enforcement, audit extensions) depend on RFC 0009's AuditLogger and HITL gates. RFC 0013 must be substantially complete before RFC 0011 (Channels + Bridges) ships, since bridge inputs are the primary vector for external user data entering the system.

### Planned Components

| Component | Go Package | Python Module | Description | Target RFC |
|-----------|-----------|---------------|-------------|------------|
| PersonaAgent | — | `agents/persona.py` | Event-driven `on_event()` + autonomous `on_tick()` loop | ✅ 0005 |
| Execution Limits | `internal/defaults/`, `internal/executor/` | `agents/defaults.py` | End-to-end limit propagation, conservative defaults, derived deadlines | 0006 |
| Cost Tracking & Budget Enforcement | `internal/cost/` | — | Token accounting (TokenCounter), per-workflow/per-agent/global budget gates (BudgetEnforcer), cost reporting (CostReporter) | 0006 |
| Response Caching | `internal/cost/` | — | Exact-match response cache for deterministic tasks | 0006 |
| Execution Observability | `internal/state/` | — | Per-step token usage, LLM call count, retry count, cost metadata | 0006 |
| Agent Memory & Context Optimization | `internal/scheduler/`, `internal/executor/`, `internal/state/` | `agents/memory/`, `agents/task_agent.py`, `agents/sub_agents/` | Memory access for non-persona agents, context budget allocation, caller-prepared context packaging/compression, delegation result merge contracts, shared vs isolated memory policies | 0008 |
| Condition Evaluation | `internal/scheduler/` | — | Step condition expressions, skip semantics | 0007 |
| Workflow Loops | `internal/scheduler/`, `internal/planner/` | — | Bounded repeat-until and for-each with mandatory guardrails | 0007 |
| Security & Sandboxing | `internal/security/` | `agents/tools/sandbox.py`, `agents/security.py` | Agent identity tokens, input sanitization, audit logging, rate limiting, HITL gates, resource limits | 0009 |
| Channels | `internal/channels/` | — | Internal message routing (groups, DMs, threads) | 0011 |
| Bridges | `internal/bridges/` | — | External service connectors (Slack, Discord, email, Telegram) | 0011 |
| Memory | — | `agents/memory/` | Three-tier: episodic (SQLite), relationship (trust/interaction), working (context window) | ✅ 0005 |
| Sub-agents | — | `agents/sub_agents/` | Ephemeral agent spawning with inherited permissions | 0010 |
| Organizations | `internal/protocols/` | — | Hierarchy, roles, meeting/negotiation protocols | 0012 |
| MCP Tools | `internal/mcp/` | `agents/tools/mcp_bridge.py` | External MCP server connections | 0010 |
| Telemetry | `internal/telemetry/` | — | OTEL span instrumentation | 0006+ |
| Compliance & Privacy | `internal/security/` | `agents/compliance.py` | Data classification, consent tracking, erasure, ethical policy, audit extensions | 0013 |

---

## v0.3 — Distributed Mesh

**Goal**: Multi-node deployment with agent-to-agent networking and distributed workflow execution.

**Design**: Architecture sketched in [persatrix-extension-spec.md](docs/persatrix-extension-spec.md). No RFCs written yet.

### Planned Components

| Component | Package | Description |
|-----------|---------|-------------|
| Mesh Networking | `internal/mesh/` | Multi-node peer discovery and communication |
| A2A Protocol | `internal/a2a/` | Agent-to-agent networking across nodes |
| Agent Migration | — | Move agents between nodes for load balancing |

---

## v0.4+ — Future

- Advanced simulation controls and evaluation framework
- Web dashboard for observation and control
- Extended autonomy models
- Platform integrations

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

---

## How to Update This File

This file must be reviewed and updated **during every task**, not just at completion.

### On every task (before starting and after finishing)

1. Verify the **RFC Tracker** table matches reality — correct status, correct merged count.
2. Verify the **Component Status** tables — any component you touched should reflect current state.
3. Update the **Last updated** date at the top.

### When a PR is merged

1. Add the PR to the **Merged PR History** table.
2. Increment the merged count in the **RFC Tracker** table.
3. If all PRs for an RFC are now merged, change its status to `✅ Implemented` here **and** in the RFC file.
4. Move completed components from "TODO stub" → "Complete" in component tables.

### When starting RFC implementation

1. Change the RFC status to `🚧 Implementing` here **and** in the RFC file (`docs/rfcs/NNNN-*.md`).

### When creating a new RFC

1. Add a row to the **RFC Tracker** table with status `📋 Proposed` and PR count `0/N`.

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
