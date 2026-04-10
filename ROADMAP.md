# Orchestr8 Roadmap

> **Last updated**: 2026-04-10  
> **Current phase**: v0.1 (MVP) — ~60% complete

This document tracks development progress across all phases. Update it when merging PRs or completing milestones.

---

## Phase Overview

| Version | Scope | Status |
|---------|-------|--------|
| **v0.1** | Core engine: orchestrator, task agents, workflows, REST API, gRPC dispatch, tools | 🚧 In Progress |
| **v0.2** | Agent societies: personas, channels, protocols, bridges, memory, sub-agents | 📋 Planned |
| **v0.3** | Distributed mesh: multi-node, A2A protocol, platform integrations | 📋 Planned |
| **v0.4+** | Autonomous agents, simulation controls, web dashboard | 📋 Future |

---

## v0.1 — MVP Core Engine

**Goal**: End-to-end workflow execution — submit a YAML workflow via CLI/REST, orchestrator plans and schedules stages, dispatches tasks to Python agents over gRPC, agents call LLMs and tools, results flow back.

### RFC Tracker

| RFC | Title | Status | PRs | Merged |
|-----|-------|--------|-----|--------|
| [0001](docs/rfcs/0001-core-orchestration-pipeline.md) | Core Orchestration Pipeline (Planner + State + Registry) | ✅ Implemented | 6 | 6/6 |
| [0002](docs/rfcs/0002-rest-api-server.md) | REST API Server (HTTP Layer + Workflow Submission) | ✅ Implemented | 4 | 4/4 |
| [0003](docs/rfcs/0003-scheduler-executor.md) | Scheduler & Executor (Parallel Stage Execution + gRPC Dispatch) | 🚧 Implementing | 7 | 5/7 |
| [0004](docs/rfcs/0004-python-agent-grpc-server.md) | Python Agent gRPC Server (AgentService Implementation) | 📋 Proposed | 7 | 0/7 |

### Dependency Chain

```
RFC 0001 (State, Registry, Planner)           ✅ Done
    ↓
RFC 0002 (REST API Server)                    ✅ Done
    ↓
RFC 0003 (Scheduler + Executor + gRPC)        🚧 In Progress
    ↓
RFC 0004 (Python Agent Server + Tools)        📋 Blocked on RFC 0003
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
| `internal/scheduler/` | Workflow scheduling (pick up pending runs, drive stages) | � In Progress (RFC 0003) |
| `internal/executor/` | gRPC task dispatch to agents | ✅ Complete (96.1% coverage) |
| `internal/generated/` | Protobuf/gRPC generated code | ✅ Complete (generated stubs) |
| `internal/resilience/` | Circuit breaker, dead letter queue | 🔲 TODO stub (post-v0.1) |
| `internal/security/` | Permission gates, rate limiting, audit logging | 🔲 TODO stub (v0.2+) |
| `internal/telemetry/` | OTEL span instrumentation | 🔲 TODO stub (v0.2+) |
| `internal/cost/` | Token/cost tracking aggregation | 🔲 TODO stub (v0.2+) |

#### Python Agents (`agents/`)

| Module | Purpose | Status |
|--------|---------|--------|
| `agents/base.py` | BaseAgent ABC + dataclasses | ✅ Complete (interface) |
| `agents/server.py` | gRPC service entry point | 🔲 TODO (RFC 0004 PR 5a) |
| `agents/coder.py` | Code generation task agent | 🔲 TODO (RFC 0004 PR 4b) |
| `agents/reviewer.py` | Code review task agent | 🔲 TODO (RFC 0004 PR 4b) |
| `agents/planner_agent.py` | Task decomposition agent | 🔲 TODO (RFC 0004 PR 4b) |
| `agents/validate.py` | Config validation | 🔲 TODO (RFC 0004) |
| `agents/tools/registry.py` | Tool discovery and registration | 🔲 TODO (RFC 0004 PR 3) |
| `agents/tools/builtin.py` | Built-in tools (file_read, file_write, shell_exec, http_request) | 🔲 TODO (RFC 0004 PR 3) |
| `agents/tools/permissions.py` | Deny-by-default permission gate | 🔲 TODO (RFC 0004 PR 2) |
| `agents/tools/sandbox.py` | Filesystem path restriction | 🔲 TODO (RFC 0004 PR 2) |
| `agents/generated/` | Python gRPC generated stubs | 🔲 TODO (RFC 0004 PR 1) |

#### Rust CLI (`cli/`)

| Module | Purpose | Status |
|--------|---------|--------|
| `cli/src/main.rs` | CLI commands (run, status, agents) | ✅ Functional (submits workflows via REST) |

### What Works Today

1. Submit a workflow via CLI → `POST /api/v1/workflows/run`
2. Orchestrator receives request, planner parses YAML, validates DAG, generates execution plan
3. Server creates `WorkflowRun` in state store → status = Pending
4. Poll `GET /api/v1/workflows/{id}/status` → returns Pending
5. CRUD operations on agents via REST API

### What's Missing for v0.1

1. **Scheduler** — pick up pending runs, transition to Running, drive stage execution
2. **Executor** — dispatch tasks to agents via gRPC `ExecuteTask`
3. **Proto generation** — Go and Python gRPC stubs from `proto/task.proto`
4. **Python agent server** — receive `TaskRequest`, call LLM, use tools, return `TaskResponse`
5. **Three task agents** — CoderAgent, ReviewerAgent, PlannerAgent with LLM integration
6. **Tool system** — permission gate, path validator, tool registry, 4 built-in tools

> Currently, submitted runs stay Pending forever after creation.

---

## v0.2 — Agent Societies

**Goal**: Persona-driven agents with autonomous behavior, multi-channel communication, organizational hierarchy, and persistent memory.

**Design**: Fully specified in [orchestr8-extension-spec.md](docs/orchestr8-extension-spec.md). No RFCs written yet.

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
