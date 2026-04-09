# RFC 0003 — PR Implementation Plan

**RFC**: [0003-scheduler-executor.md](0003-scheduler-executor.md)
**Created**: 2026-04-09
**Branch prefix**: `feature/v01-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)

---

## Overview

RFC 0003 defines ~900 LOC across 5 phases (excluding generated proto output). The project's PR size limit is <500 lines of meaningful change. This plan splits the work into **5 PRs**, one per phase. Each PR is independently mergeable and leaves the codebase in a compilable, test-passing state.

> **Estimate calibration**: RFC 0001 PRs consistently exceeded estimates by 73–138%. Sizes in this plan are calibrated to ~1.7× of naive estimates.

**Prerequisite**: All RFC 0001 PRs merged (state, registry, planner) ✅. RFC 0002 PRs merged (server, workflow/agent handlers, wiring) — required for pending runs to exist as the scheduler's input queue.

---

## PR Sequence

### PR 1: `feature/v01-proto-gen` — Protobuf Go Code Generation

**Depends on**: Nothing (proto files exist)
**Branch**: `feature/v01-proto-gen`
**Estimated size**: ~50 lines config + generated output (generated code excluded from review)

#### Scope

| File | Change |
|------|--------|
| `proto/task.proto` | Add `option go_package = "github.com/orchestr8/orchestr8/internal/proto/gen";` if absent |
| `internal/proto/gen/task.pb.go` | Generated — protobuf message types |
| `internal/proto/gen/task_grpc.pb.go` | Generated — gRPC service client/server stubs |
| `Makefile` | Add `proto-go` target calling `protoc` with `--go_out` and `--go-grpc_out` |
| `go.mod` / `go.sum` | Add `google.golang.org/grpc`, `google.golang.org/protobuf` |

#### Key implementation details

- `protoc` with `protoc-gen-go` and `protoc-gen-go-grpc` plugins.
- Generated output in `internal/proto/gen/` — not alongside `.proto` source.
- `.gitignore` does NOT exclude generated files — they are committed for reproducible builds without requiring `protoc` toolchain.
- `make proto-go` must be idempotent.

#### PR checklist

- [ ] `make proto-go` succeeds
- [ ] `go build ./internal/proto/gen/...` compiles
- [ ] Generated files committed to repo
- [ ] `go vet ./internal/proto/gen/...` clean

---

### PR 2: `feature/v01-executor` — GRPCExecutor

**Depends on**: PR 1 merged (generated stubs)
**Branch**: `feature/v01-executor`
**Estimated size**: ~500–700 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `internal/executor/executor.go` | Replace TODO stubs — `Executor` interface, `ExecuteRequest`, `ExecuteResult`, `GRPCExecutor`, `isTransient`, `NewGRPCExecutor`, functional options |
| `internal/executor/executor_test.go` | New — unit tests with mock gRPC server via `google.golang.org/grpc/test/bufconn` |

#### Key implementation details

- `Executor` interface with `ExecuteTask(ctx, ExecuteRequest) (*ExecuteResult, error)` and `Close() error`.
- `GRPCExecutor` creates a per-task gRPC connection (no pooling in v0.1). `// TODO(v0.2): connection pooling` comment.
- Retry loop with exponential backoff: `100ms * 2^attempt`, max 3 retries.
- `isTransient` classifies gRPC status codes: `Unavailable`, `DeadlineExceeded`, `ResourceExhausted`, `Aborted` → transient; all others → permanent.
- Functional options: `WithTimeout(d)`, `WithMaxRetries(n)`.
- `grpc.WithTransportCredentials(insecure.NewCredentials())` with `// TODO(security): enable mTLS` comment.
- Agent health status check before dial: `StatusHealthy` required, else error.

#### Tests

- **Successful dispatch**: mock gRPC server → `COMPLETED` response → `ExecuteResult`.
- **Agent not found**: `ErrAgentNotFound` → error.
- **Agent unhealthy**: `StatusOffline` → error before dial.
- **Transient retry success**: `Unavailable` × 2 → success on 3rd → result returned.
- **Permanent failure**: `InvalidArgument` → no retry, immediate error.
- **Retry exhaustion**: `Unavailable` × (maxRetries+1) → error.
- **Timeout**: blocking mock → `DeadlineExceeded`.
- **FAILED status**: `TaskResponse{Status: FAILED}` → error.
- **Context cancellation**: cancel mid-retry → `context.Canceled`.
- **`isTransient` table-driven**: every `codes.*` value → expected bool.
- Race detector (`-race`).

#### PR checklist

- [ ] `go test ./internal/executor/... -v -cover` passes
- [ ] Coverage ≥ 80%
- [ ] `go vet ./internal/executor/...` clean
- [ ] No real network connections in tests (bufconn only)

---

### PR 3: `feature/v01-scheduler` — WorkflowScheduler

**Depends on**: PR 2 merged (Executor interface)
**Branch**: `feature/v01-scheduler`
**Estimated size**: ~600–900 lines (implementation + tests)

> **Note**: If this PR exceeds 500 lines, the step-level execution and template resolution tests can be split into a follow-up PR.

#### Scope

| File | Change |
|------|--------|
| `internal/scheduler/scheduler.go` | Replace TODO stubs — `Scheduler` interface, `WorkflowScheduler`, polling loop, stage/step execution, outputs map with mutex, state transitions |
| `internal/scheduler/scheduler_test.go` | New — unit tests with mock Executor, Store, Planner |

#### Key implementation details

- `Scheduler` interface with `Run(ctx) error`.
- `WorkflowScheduler` constructor takes `store`, `registry`, `planner`, `executor`, `logger`, `workflowsDir`.
- Polling loop: `time.Ticker` with configurable `pollInterval` (default 1s).
- Semaphore for max concurrent runs (default 10): `chan struct{}`.
- `resolveWorkflowPath`: simple `filepath.Join(workflowsDir, id+".yaml")` — no traversal check (pre-validated by REST API).
- `executeRun`: Parse → ValidateDAG → Plan → stages loop → state transitions.
- `executeStage`: `sync.WaitGroup` + error channel for parallel fan-out/barrier.
- `outputs` map protected by `sync.Mutex` for concurrent step writes.
- `executeStep`: `planner.ResolveInputs` → `executor.ExecuteTask` → update `StepState`.
- All steps execute unconditionally in v0.1. `// TODO(v0.2): evaluate step conditions` comment.
- `failRun` helper: sets `RunFailed` + error message + `FinishedAt`.

#### Tests

- **Single-step end-to-end**: pending run → completed.
- **Multi-stage sequential**: 3 stages → correct order.
- **Parallel stage**: 2 concurrent steps → both dispatched.
- **Step failure fails run**: executor error → `RunFailed`.
- **Template resolution**: `{{ user_request }}` and `{{ steps.<id>.output }}` resolved.
- **Resolution failure**: missing var → `RunFailed`.
- **Poll loop**: pending run picked up within 2× interval.
- **Graceful shutdown**: cancel ctx → `context.Canceled`.
- **Concurrent run limit**: excess runs queued.
- **Empty poll cycle**: no pending runs → no-op.
- **Parse failure**: bad YAML → `RunFailed`.
- **DAG failure**: cycle → `RunFailed`.
- Race detector (`-race`).

#### PR checklist

- [ ] `go test ./internal/scheduler/... -v -cover` passes
- [ ] Coverage ≥ 80%
- [ ] `go vet ./internal/scheduler/...` clean
- [ ] Parallel step execution verified (not just sequential)
- [ ] `outputs` map access is mutex-protected

---

### PR 4: `feature/v01-state-extension` — Store Extension + RunRetrying

**Depends on**: PR 3 merged (scheduler uses new state methods)
**Branch**: `feature/v01-state-extension`
**Estimated size**: ~200–300 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `internal/state/state.go` | Add `RunRetrying RunStatus = 5`, `SetRunTimestamps` to `Store` interface, implement in `InMemoryStore`, add `String()` method on `RunStatus` |
| `internal/state/state_test.go` | Extended — tests for `SetRunTimestamps`, `RunRetrying`, `String()` |

#### Key implementation details

- `RunRetrying RunStatus = 5` — explicit integer, aligned with `proto/task.proto` `RETRYING = 5`.
- `SetRunTimestamps(ctx, runID, startedAt, finishedAt *time.Time)` — nil pointer means "leave unchanged".
- `RunStatus.String()` method: maps each constant to lowercase string (`"pending"`, `"running"`, `"completed"`, `"failed"`, `"cancelled"`, `"retrying"`).
- Deep-copy semantics maintained for timestamp fields.

#### Tests

- `SetRunTimestamps` with both timestamps → verify via `GetRun`.
- `SetRunTimestamps` with only `StartedAt` → `FinishedAt` unchanged.
- `SetRunTimestamps` with only `FinishedAt` → `StartedAt` unchanged.
- `SetRunTimestamps` on nonexistent run → `ErrRunNotFound`.
- `RunRetrying` stored and retrieved correctly.
- `RunStatus.String()` for all 6 values.
- Race detector (`-race`).

#### PR checklist

- [ ] `go test ./internal/state/... -v -cover` passes
- [ ] Coverage ≥ 80%
- [ ] `RunRetrying = 5` (explicit, no `iota`)
- [ ] `go vet ./internal/state/...` clean

---

### PR 5: `feature/v01-scheduler-wiring` — Wire into main.go

**Depends on**: PRs 1–4 merged
**Branch**: `feature/v01-scheduler-wiring`
**Estimated size**: ~100–150 lines

#### Scope

| File | Change |
|------|--------|
| `cmd/orchestrator/main.go` | Import `internal/executor`, `internal/scheduler`. Create `GRPCExecutor` and `WorkflowScheduler`. Launch `sched.Run(ctx)` in goroutine. Remove `_ = store`, `_ = reg`, `_ = plan` placeholders. Add structured log messages. |

#### Key implementation details

- `executor.NewGRPCExecutor(reg, logger)` — default timeout and retries.
- `scheduler.NewWorkflowScheduler(store, reg, plan, exec, logger)` with `*workflowsDir`.
- Goroutine: `go func() { if err := sched.Run(ctx); err != nil && !errors.Is(err, context.Canceled) { logger.Error(...); cancel() } }()`.
- Log `"scheduler started"` and `"executor initialized"`.
- Remove all `_ = ...` suppression lines — variables now consumed.
- Uses structured zap logger (not sugar) for new code per project convention.

#### PR checklist

- [ ] `go build ./cmd/orchestrator` succeeds
- [ ] `go vet ./cmd/orchestrator/...` clean
- [ ] No `_ = ...` unused-variable suppressions remain
- [ ] Binary starts cleanly with `--workflows-dir workflows/`
- [ ] Graceful shutdown via SIGINT

---

## Risk Mitigation

| Risk | Impact | Mitigation |
|------|--------|------------|
| PR 3 (scheduler) exceeds 500 lines | Review burden | Split step-level tests into follow-up PR |
| Proto generation toolchain issues | Blocks all PRs | PR 1 validates toolchain early; generated files committed |
| gRPC dependency tree large | `go.mod` bloat | Acceptable for v0.1; prune unused transitive deps in cleanup PR |
| Mock gRPC server complexity | Test maintenance | Use `bufconn` for in-process server; keep mock implementations minimal |
| Condition evaluation omission | Incomplete feature-builder execution | Document that `revise` step always executes in v0.1; add TODO |

---

## Dependency Graph

```
PR 1 (proto-gen)
    │
    ▼
PR 2 (executor) ──────► PR 3 (scheduler) ──────► PR 5 (wiring)
                                                      ▲
PR 4 (state-extension) ──────────────────────────────┘
```

PR 4 (state extension) can proceed in parallel with PRs 2–3 since it modifies `internal/state/` independently. However, PR 5 (wiring) depends on all prior PRs.
