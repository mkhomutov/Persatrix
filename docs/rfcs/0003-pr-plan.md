# RFC 0003 — PR Implementation Plan

**RFC**: [0003-scheduler-executor.md](0003-scheduler-executor.md)
**Created**: 2026-04-09
**Branch prefix**: `feature/v01-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)

---

## Overview

RFC 0003 defines ~900 LOC across 5 phases (excluding generated proto output). The project's PR size limit is <500 lines of meaningful change. This plan splits the work into **6 PRs** (one per phase, with the scheduler phase split into 3a/3b per review finding B-05). Each PR is independently mergeable and leaves the codebase in a compilable, test-passing state.

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
| `proto/task.proto` | `go_package` already set to `"github.com/orchestr8/orchestr8/internal/generated/taskpb"` — no change needed |
| `internal/generated/taskpb/task.pb.go` | Generated — protobuf message types |
| `internal/generated/taskpb/task_grpc.pb.go` | Generated — gRPC service client/server stubs |
| `Makefile` | Existing `proto` target already generates Go stubs via `PROTO_GO_OUT := internal/generated` — no change needed |
| `go.mod` / `go.sum` | Add `google.golang.org/grpc`, `google.golang.org/protobuf` |

#### Key implementation details

- `protoc` with `protoc-gen-go` and `protoc-gen-go-grpc` plugins.
- Generated output in `internal/generated/taskpb/` — matching the existing `go_package` option and `PROTO_GO_OUT` Makefile variable.
- `.gitignore` does NOT exclude generated files — they are committed for reproducible builds without requiring `protoc` toolchain.
- `make proto` already handles Go stub generation — no new Makefile target needed.

#### PR checklist

- [ ] `make proto` succeeds
- [ ] `go build ./internal/generated/...` compiles
- [ ] Generated files committed to repo
- [ ] `go mod tidy` run and `go.sum` clean (review N-10)
- [ ] `go vet ./internal/generated/...` clean

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
- `isTransient` classifies gRPC status codes: `Unavailable`, `ResourceExhausted`, `Aborted` → transient; `DeadlineExceeded` and all others → permanent (review S-04: retrying after timeout with the same timeout is unlikely to succeed). Non-gRPC errors (DNS, connection refused) default to transient (review B-03: these are the most common transient failures for per-task connections).
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

### PR 3a: `feature/v01-scheduler-core` — WorkflowScheduler Core

**Depends on**: PR 2 merged (Executor interface), PR 4 merged (`SetRunTimestamps`, `RunRetrying`)
**Branch**: `feature/v01-scheduler-core`
**Estimated size**: ~400–550 lines (implementation + core tests)

> **Note (review B-05)**: RFC 0001 PRs consistently exceeded estimates by 73–138%. An original single-PR estimate of 600–900 lines would likely land at 1000–1200 lines, well above the 500-line limit. Pre-splitting into 3a/3b is the default plan (not a fallback), mirroring RFC 0001’s successful PR 3a/3b split.

#### Scope

| File | Change |
|------|--------|
| `internal/scheduler/scheduler.go` | Replace TODO stubs — `Scheduler` interface, `WorkflowScheduler`, polling loop, stage execution (parallel fan-out/barrier), `executeRun`, `failRun`, outputs map with mutex, state transitions |
| `internal/scheduler/scheduler_test.go` | Core tests: single-step e2e, multi-stage sequential, parallel stage, step failure, poll loop, graceful shutdown, concurrent run limit, empty poll, parse failure, DAG failure |

#### Key implementation details

- `Scheduler` interface with `Run(ctx) error`.
- `WorkflowScheduler` constructor takes `store`, `registry`, `planner`, `executor`, `logger`, `workflowsDir`.
- Polling loop: `time.Ticker` with configurable `pollInterval` (default 1s).
- Semaphore for max concurrent runs (default 10): `chan struct{}`. Acquisition inside spawned goroutine (review S-07) with `select`/`ctx.Done()` to prevent goroutine leaks on shutdown (review B-01).
- In-flight run tracking via `sync.Map` to prevent duplicate execution across poll cycles (review B-02).
- `resolveWorkflowPath`: simple `filepath.Join(workflowsDir, id+".yaml")` — no traversal check (pre-validated by REST API).
- `executeRun`: Parse → ValidateDAG → Plan → set `StartedAt` via `SetRunTimestamps` when transitioning to `RunRunning` (review B-06) → stages loop with `ctx.Done()` check between stages (review B-04) → state transitions.
- `executeStage`: `sync.WaitGroup` + `sync.Mutex` + error channel for parallel fan-out/barrier. Mutex protects `outputs` map writes (review B-02).
- `executeStep`: `planner.ResolveInputs` → `executor.ExecuteTask` → update `StepState`. All `UpdateStepState` errors logged (review B-03).
- All steps execute unconditionally in v0.1. `// TODO(v0.2): evaluate step conditions` comment.
- `failRun` helper: sets `RunFailed` + error message + `FinishedAt` via `SetRunTimestamps`.

#### Tests

- **Single-step end-to-end**: pending run → completed.
- **Multi-stage sequential**: 3 stages → correct order.
- **Parallel stage**: 2 concurrent steps → both dispatched.
- **Step failure fails run**: executor error → `RunFailed`.
- **Poll loop**: pending run picked up within 2× interval.
- **Graceful shutdown**: cancel ctx → `context.Canceled`.
- **Graceful shutdown with blocked goroutines**: cancel ctx while goroutines wait on semaphore → goroutines exit cleanly (review B-01).
- **Concurrent run limit**: excess runs queued.
- **Duplicate run prevention**: same pending run across two poll cycles → executed once (review B-02).
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
- [ ] Semaphore acquisition uses `select`/`ctx.Done()` (review B-01)
- [ ] In-flight run deduplication verified (review B-02)
- [ ] `StartedAt` set when transitioning to `RunRunning` (review B-06)
- [ ] Cancellation check present between stages (review B-04)

---

### PR 3b: `feature/v01-scheduler-templates` — Step Execution & Template Resolution Tests

**Depends on**: PR 3a merged
**Branch**: `feature/v01-scheduler-templates`
**Estimated size**: ~200–350 lines (additional tests + any helper refinements)

#### Scope

| File | Change |
|------|--------|
| `internal/scheduler/scheduler_test.go` | Extended — template resolution tests, resolution failure tests, step-level state transition verification |

#### Tests

- **Template resolution**: `{{ user_request }}` and `{{ steps.<id>.output }}` resolved.
- **Resolution failure**: missing var → `RunFailed`.
- **Step state transitions**: verify `StepState` progression through `Running` → `Completed` and `Running` → `Failed`.
- **Multi-stage template chaining**: output from stage N used as input in stage N+1.
- Race detector (`-race`).

#### PR checklist

- [ ] `go test ./internal/scheduler/... -v -cover` passes
- [ ] Combined coverage (3a + 3b) ≥ 80%
- [ ] `go vet ./internal/scheduler/...` clean

---

### PR 4: `feature/v01-state-extension` — Store Extension + RunRetrying

**Depends on**: RFC 0001 (existing Store implementation) — independent of PRs 2–3
**Branch**: `feature/v01-state-extension`
**Estimated size**: ~200–300 lines (implementation + tests)

> **Note**: PR 3a (scheduler core) has a compile-time dependency on `SetRunTimestamps` and
> `RunRetrying` introduced here. PR 4 must merge **before** PR 3a, not after.

#### Scope

| File | Change |
|------|--------|
| `internal/state/state.go` | Add `RunRetrying RunStatus = 5`, `SetRunTimestamps` to `Store` interface, implement in `InMemoryStore`, extend existing `RunStatus.String()` with `RunRetrying` case |
| `internal/state/state_test.go` | Extended — tests for `SetRunTimestamps`, `RunRetrying`, `String()` |

#### Key implementation details

- `RunRetrying RunStatus = 5` — explicit integer, aligned with `proto/task.proto` `RETRYING = 5`.
- `SetRunTimestamps(ctx, runID, startedAt, finishedAt *time.Time)` — nil pointer means "leave unchanged".
- `RunStatus.String()` method: extend the existing method (covers values 0–4) with `case RunRetrying: return "retrying"`. Do NOT add a new method — one already exists in `state.go`.
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

**Depends on**: PRs 1–4 merged. **Prerequisite (review B-01):** The `--workflows-dir` flag must exist in `main.go`. This flag is specified in RFC 0002 Phase 4 (wiring). If the RFC 0002 wiring PR has not merged by the time this PR is ready, declare `--workflows-dir` (default: `"workflows/"`) directly in this PR with a comment: `// NOTE: may be superseded by RFC 0002 wiring PR if it merges first`.
**Branch**: `feature/v01-scheduler-wiring`
**Estimated size**: ~100–150 lines

#### Scope

| File | Change |
|------|--------|
| `cmd/orchestrator/main.go` | Import `internal/executor`, `internal/scheduler`. Create `GRPCExecutor` and `WorkflowScheduler`. Launch `sched.Run(ctx)` in goroutine. Remove `_ = store`, `_ = reg`, `_ = plan` placeholders. Add structured log messages. |

#### Key implementation details

- `executor.NewGRPCExecutor(reg, logger)` — default timeout and retries.
- `defer exec.Close()` — no-op in v0.1 (no persistent connections), wired for interface compliance and forward compatibility with connection pooling.
- `scheduler.NewWorkflowScheduler(store, reg, plan, exec, logger, workflowsDir)` with `*workflowsDir`.
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
| PR 3 (scheduler) exceeds 500 lines | Review burden | Pre-split into PR 3a (core) + PR 3b (template tests) per B-05 |
| Proto generation toolchain issues | Blocks all PRs | PR 1 validates toolchain early; generated files committed |
| gRPC dependency tree large | `go.mod` bloat | Acceptable for v0.1; prune unused transitive deps in cleanup PR |
| Mock gRPC server complexity | Test maintenance | Use `bufconn` for in-process server; keep mock implementations minimal |
| Condition evaluation omission | Incomplete feature-builder execution | Document that `revise` step always executes in v0.1; add TODO |

---

## Dependency Graph

```
PR 1 (proto-gen) ──────┐  PR 4 (state-extension) ──┐
                        ▼                            │
                   PR 2 (executor)                   │
                        │                            │
                        ▼                            ▼
                   PR 3a (scheduler-core) ◄──── BOTH required
                        │
                        ▼
                   PR 3b (scheduler-templates)
                        │
                        ▼
                   PR 5 (wiring)
```

> **⚠️ GATE (review B-05):** PR 3a has a **compile-time** dependency on both PR 2 (Executor interface) and PR 4 (`SetRunTimestamps`, `RunRetrying`). Neither can be skipped. PR 4 must merge **before** PR 3a despite its higher number.

PR 4 (state extension) can proceed in parallel with PRs 1–2 since it modifies `internal/state/` independently.

> **Recommended implementation order (review S-08):** PR numbering does not imply execution order. The recommended merge sequence is: **PR 1 ‖ PR 4** (parallel) → **PR 2** → **PR 3a** → **PR 3b** → **PR 5**. PR 4 must merge before PR 3a despite its higher number.
