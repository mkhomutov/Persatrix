# Orchestr8 Roadmap

> **Last updated**: 2026-04-12  
> **Current phase**: v0.2 (Agent Societies) — 🚧 In Progress

This document tracks development progress across all phases. Update it when merging PRs or completing milestones.

---

## Phase Overview

| Version | Scope | Status |
|---------|-------|--------|
| **v0.1** | Core engine: orchestrator, task agents, workflows, REST API, gRPC dispatch, tools | ✅ Complete |
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
| `agents/memory/episodic.py` | Episodic memory (SQLite, FTS5, schema migrations) | ✅ Complete (RFC 0005 PR 3a) |
| `agents/validate.py` | Config validation | 🔲 TODO (RFC 0004) |
| `agents/tools/registry.py` | Tool discovery and registration | ✅ Complete (decorator + registry) |
| `agents/tools/builtin.py` | Built-in tools (file_read, file_write, shell_exec, http_request) | ✅ Complete (RFC 0004 PR 3) |
| `agents/tools/permissions.py` | Deny-by-default permission gate | ✅ Complete (97% coverage) |
| `agents/tools/sandbox.py` | Filesystem path restriction (PathValidator) | ✅ Complete (100% coverage) |
| `agents/generated/` | Python gRPC generated stubs | ✅ Complete (RFC 0004 PR 5a) |

#### Rust CLI (`cli/`)

| Module | Purpose | Status |
|--------|---------|--------|
| `cli/src/main.rs` | CLI commands (run, status, agents) | ✅ Functional (submits workflows via REST) |

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

**Design**: Fully specified in [orchestr8-extension-spec.md](docs/orchestr8-extension-spec.md).

### RFC Tracker

| RFC | Title | Status | PRs | Merged |
|-----|-------|--------|-----|--------|
| [0005](docs/rfcs/0005-persona-agent-memory.md) | Persona Agent & Memory System | 🚧 Implementing | 12 | 4/12 |

### Dependency Chain

```
RFC 0005 (PersonaAgent + Memory + TaskAgent)   � Implementing
    ↓
RFC 0006 (Sub-Agent Spawning)                  Not yet written
    ↓
RFC 0007 (Channels + Bridges)                  Not yet written
    ↓
RFC 0008 (Protocols + Organizations)            Not yet written
```

### Planned Components

| Component | Go Package | Python Module | Description |
|-----------|-----------|---------------|-------------|
| PersonaAgent | — | `agents/persona.py` | Event-driven `on_event()` + autonomous `on_tick()` loop |
| Channels | `internal/channels/` | — | Internal message routing (groups, DMs, threads) |
| Bridges | `internal/bridges/` | — | External service connectors (Slack, Discord, email, Telegram) |
| Memory | — | `agents/memory/` | Three-tier: episodic (SQLite), relationship (trust/interaction), working (context window) |
| Sub-agents | — | `agents/sub_agents/` | Ephemeral agent spawning with inherited permissions |
| Organizations | `internal/protocols/` | — | Hierarchy, roles, meeting/negotiation protocols |
| MCP Tools | `internal/mcp/` | `agents/tools/mcp_bridge.py` | External MCP server connections |
| Telemetry | `internal/telemetry/` | — | OTEL span instrumentation |
| Cost Tracking | `internal/cost/` | — | Token accounting and budget enforcement |

---

## v0.3 — Distributed Mesh

**Goal**: Multi-node deployment with agent-to-agent networking and distributed workflow execution.

**Design**: Architecture sketched in [orchestr8-extension-spec.md](docs/orchestr8-extension-spec.md). No RFCs written yet.

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
| [#6](https://github.com/mkhomutov/Orchestr8/pull/6) | feat(state): implement InMemoryStateStore | 0001 (1/6) | 2026-04-08 |
| [#7](https://github.com/mkhomutov/Orchestr8/pull/7) | feat(registry): implement InMemoryRegistry | 0001 (2/6) | 2026-04-08 |
| [#8](https://github.com/mkhomutov/Orchestr8/pull/8) | feat(planner): YAMLPlanner Parse+DAG+Plan | 0001 (3/6) | 2026-04-08 |
| [#9](https://github.com/mkhomutov/Orchestr8/pull/9) | feat(planner): ResolveInputs template resolution | 0001 (4/6) | 2026-04-08 |
| [#10](https://github.com/mkhomutov/Orchestr8/pull/10) | feat(orchestrator): wire into main.go | 0001 (5/6) | 2026-04-08 |
| [#12](https://github.com/mkhomutov/Orchestr8/pull/12) | fix: review findings follow-up | 0001 (6/6) | 2026-04-09 |
| [#14](https://github.com/mkhomutov/Orchestr8/pull/14) | feat(server): HTTP scaffolding + workflow handlers | 0002 (1/4) | 2026-04-09 |
| [#16](https://github.com/mkhomutov/Orchestr8/pull/16) | feat(server): agent registry endpoints | 0002 (2/4) | 2026-04-09 |
| [#17](https://github.com/mkhomutov/Orchestr8/pull/17) | feat(server): stub endpoints + main.go wiring | 0002 (3/4) | 2026-04-09 |
| [#18](https://github.com/mkhomutov/Orchestr8/pull/18) | fix: review findings follow-up | 0002 (4/4) | 2026-04-10 |
| [#21](https://github.com/mkhomutov/Orchestr8/pull/21) | feat(generated): protobuf Go code generation | 0003 (1/7) | 2026-04-10 |
| [#22](https://github.com/mkhomutov/Orchestr8/pull/22) | feat(executor): GRPCExecutor core with retry logic | 0003 (2/7) | 2026-04-10 |
| [#23](https://github.com/mkhomutov/Orchestr8/pull/23) | test(executor): retry logic & error classification tests | 0003 (3/7) | 2026-04-10 |
| [#24](https://github.com/mkhomutov/Orchestr8/pull/24) | feat(state): RunRetrying, SetRunTimestamps, SetRunError | 0003 (4/7) | 2026-04-10 |
| [#25](https://github.com/mkhomutov/Orchestr8/pull/25) | feat(scheduler): WorkflowScheduler core with polling, parallel stages, dedup | 0003 (5/7) | 2026-04-10 |
| [#26](https://github.com/mkhomutov/Orchestr8/pull/26) | test(scheduler): step execution, template resolution, error path coverage | 0003 (6/7) | 2026-04-10 |
| [#27](https://github.com/mkhomutov/Orchestr8/pull/27) | feat(orchestrator): wire scheduler + executor into main.go | 0003 (7/7) | 2026-04-10 |
| [#28](https://github.com/mkhomutov/Orchestr8/pull/28) | docs: add follow-up PRs 6-9 to RFC 0003 PR plan | 0003 docs | 2026-04-10 |
| [#29](https://github.com/mkhomutov/Orchestr8/pull/29) | docs(rfc0001): complete PR 6 follow-up scope with carry-forward findings | 0001 docs | 2026-04-10 |
| [#30](https://github.com/mkhomutov/Orchestr8/pull/30) | fix(state): replace rune-based test IDs (RFC 0001, F-06) | 0001 follow-up (1/1) | 2026-04-10 |
| [#31](https://github.com/mkhomutov/Orchestr8/pull/31) | fix(executor): additive dial options, cancellation & concurrent retry tests | 0003 follow-up (1/4) | 2026-04-10 |
| [#32](https://github.com/mkhomutov/Orchestr8/pull/32) | test: observability improvements — concurrent race tests, log assertions, zaptest logger | 0003 follow-up (2/4) | 2026-04-11 |
| [#33](https://github.com/mkhomutov/Orchestr8/pull/33) | fix(orchestrator): graceful shutdown drain + absolute workflowsDir | 0003 follow-up (3/4) | 2026-04-11 |
| [#34](https://github.com/mkhomutov/Orchestr8/pull/34) | build(proto): split make proto into go/python targets + CI staleness check | 0003 follow-up (4/4) | 2026-04-11 |
| [#35](https://github.com/mkhomutov/Orchestr8/pull/35) | docs: RFC 0003/0004 status updates, multi-provider LLM design, v0.2 deferrals | cross-RFC docs | 2026-04-11 |
| [#36](https://github.com/mkhomutov/Orchestr8/pull/36) | feat(agents): PermissionGate + PathValidator | 0004 (2/7) | 2026-04-11 |
| [#37](https://github.com/mkhomutov/Orchestr8/pull/37) | feat(agents): built-in tools + PR 2 follow-up fixes | 0004 (3/7) | 2026-04-11 |
| [#38](https://github.com/mkhomutov/Orchestr8/pull/38) | feat(agents): LLM client + TaskInputConfig + base handle loop | 0004 (4a/7) | 2026-04-11 |
| [#39](https://github.com/mkhomutov/Orchestr8/pull/39) | feat(agents): CoderAgent, ReviewerAgent, PlannerAgent | 0004 (4b/7) | 2026-04-11 |
| [#40](https://github.com/mkhomutov/Orchestr8/pull/40) | feat(agents): gRPC server + agent loading + proto stubs + follow-up fixes | 0004 (5a/7) | 2026-04-11 |
| [#41](https://github.com/mkhomutov/Orchestr8/pull/41) | feat(agents): self-registration + integration tests + follow-up fixes | 0004 (5b/7) | 2026-04-11 |
| [#42](https://github.com/mkhomutov/Orchestr8/pull/42) | fix(agents): registration follow-ups + RFC 0004 close | 0004 (6/7) | 2026-04-11 |
| [#44](https://github.com/mkhomutov/Orchestr8/pull/44) | fix(lint): resolve all golangci-lint, ruff, mypy, clippy warnings | v0.1 release prep | 2026-04-11 |
| [#45](https://github.com/mkhomutov/Orchestr8/pull/45) | docs(rfc): RFC 0005 — Persona Agent & Memory System | 0005 (RFC) | 2026-04-12 |
| [#46](https://github.com/mkhomutov/Orchestr8/pull/46) | docs(rfc0005): add PR implementation plan | 0005 (PR plan) | 2026-04-12 |
| [#47](https://github.com/mkhomutov/Orchestr8/pull/47) | feat(agents): data-driven TaskAgent + agent type system | 0005 (1a/12) | 2026-04-12 |
| [#48](https://github.com/mkhomutov/Orchestr8/pull/48) | feat(cli): wire v0.1 REST endpoints | 0005 (1b/12) | 2026-04-12 |
| [#49](https://github.com/mkhomutov/Orchestr8/pull/49) | feat(memory): working memory + token estimation | 0005 (2/12) | 2026-04-12 |
| [#50](https://github.com/mkhomutov/Orchestr8/pull/50) | feat(memory): schema migration + episodic memory core | 0005 (3a/12) | 2026-04-12 |

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
