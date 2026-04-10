# RFC 0003 — PR Implementation Plan

**RFC**: [0003-scheduler-executor.md](0003-scheduler-executor.md)
**Created**: 2026-04-09
**Branch prefix**: `feature/v01-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)

---

## Overview

RFC 0003 defines ~900 LOC across 5 phases (excluding generated proto output). The project's PR size limit is <500 lines of meaningful change. This plan splits the work into **7 PRs** (one per phase, with both the executor and scheduler phases pre-split into a/b per calibration data showing RFC 0001 PRs exceeded estimates by 73–138%). Each PR is independently mergeable and leaves the codebase in a compilable, test-passing state.

> **Estimate calibration**: RFC 0001 PRs consistently exceeded estimates by 73–138%. Sizes in this plan are calibrated to ~1.7× of naive estimates.

**Prerequisite**: All RFC 0001 PRs merged (state, registry, planner) ✅. RFC 0002 PRs merged (server, workflow/agent handlers, wiring) — required for pending runs to exist as the scheduler's input queue.

> **⚠️ RFC 0002 merge gate:** All RFC 0002 implementation PRs must be merged before PR 5 (wiring) can proceed. PRs 1–4 are independent of RFC 0002 and can begin immediately. If RFC 0002 wiring has not landed by the time PR 5 is ready, PR 5 declares `--workflows-dir` locally (see PR 5 scope).

**Recommended merge order:** **PR 1 ‖ PR 4** (parallel) → **PR 2a** → **PR 2b** → **PR 3a** → **PR 3b** → **PR 5**. PR numbering does not imply execution order — see [Dependency Graph](#dependency-graph) for details.

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
| `proto/agent_message.proto` | `go_package` already set to `"github.com/orchestr8/orchestr8/internal/generated/msgpb"` — no change needed |
| `internal/generated/taskpb/task.pb.go` | Generated — protobuf message types |
| `internal/generated/taskpb/task_grpc.pb.go` | Generated — gRPC service client/server stubs |
| `internal/generated/msgpb/agent_message.pb.go` | Generated — protobuf message types (ChannelService, v0.2 schema; generated now for consistency since `make proto` compiles all `.proto` files) |
| `internal/generated/msgpb/agent_message_grpc.pb.go` | Generated — gRPC service client/server stubs (ChannelService) |
| `Makefile` | Existing `proto` target already generates Go stubs via `PROTO_GO_OUT := internal/generated` — no change needed |
| `go.mod` / `go.sum` | Add `google.golang.org/grpc`, `google.golang.org/protobuf` |

#### Key implementation details

- `protoc` with `protoc-gen-go` and `protoc-gen-go-grpc` plugins.
- Generated output in `internal/generated/taskpb/` — matching the existing `go_package` option and `PROTO_GO_OUT` Makefile variable.
- `.gitignore` does NOT exclude generated files — they are committed for reproducible builds without requiring `protoc` toolchain.
- `make proto` already handles Go stub generation — no new Makefile target needed.

#### PR checklist

- [x] `make proto` succeeds
- [x] `go build ./internal/generated/...` compiles
- [x] Generated files committed to repo
- [x] `go mod tidy` run and `go.sum` clean (review N-10)
- [x] `go vet ./internal/generated/...` clean

#### Post-merge findings

- **N-01 (Python stub generation)**: `make proto` includes `--python_out` and `--grpc_python_out` flags (Makefile line 26). Python stubs are gitignored (`agents/generated/`). Running `make proto` without the Python gRPC plugin (`grpcio-tools`) installed will fail. RFC 0004 PR 1 should decide whether to (a) un-gitignore and commit Python stubs, or (b) generate them at `pip install` time. Consider adding a `make proto-go` target for Go-only generation until RFC 0004 lands.
- **N-02 (CI staleness check)**: No automated check verifies that generated Go stubs stay in sync with `.proto` files. Add a CI step: `make proto && git diff --exit-code internal/generated/` to detect when `.proto` changes are committed without regenerating stubs. Prevents silent proto↔stub drift. *(Review pr-021, Should Fix #1)*
- **N-03 (`make proto-go` target)**: Split `make proto` into `make proto-go` (Go stubs only) and `make proto-python` (Python stubs only), with `make proto` calling both. Decouples Go development from the Python gRPC toolchain. Aligns with N-01 disposition. *(Review pr-021, Should Fix #2)*
- **N-04 (Proto linting)**: Consider adding `buf lint` or `protoc-gen-validate` to CI for enforcing naming conventions, field numbering rules, and style consistency. Low priority with only 2 proto files. *(Review pr-021, Nice to Have)*
- **N-05 (Proto breaking change detection)**: `buf breaking` can detect backward-incompatible proto changes (removed fields, changed types). Useful as more services are defined. *(Review pr-021, Nice to Have)*

---

### PR 2a: `feature/v01-executor-core` — GRPCExecutor Core

**Depends on**: PR 1 merged (generated stubs)
**Branch**: `feature/v01-executor-core`
**Estimated size**: ~350–500 lines (implementation + core tests)

> **Note (review S-01)**: RFC 0001 PRs consistently exceeded estimates by 73–138%. An original single-PR estimate of 500–700 lines would likely land at 850–1000 lines, well above the 500-line limit. Pre-splitting into 2a/2b is the default plan (not a fallback), mirroring the successful PR 3a/3b pre-split strategy.

#### Scope

| File | Change |
|------|--------|
| `internal/executor/executor.go` | Replace TODO stubs — `Executor` interface, `ExecuteRequest`, `ExecuteResult`, `GRPCExecutor`, `isTransient`, `NewGRPCExecutor`, functional options |
| `internal/executor/executor_test.go` | New — core tests with mock gRPC server via `google.golang.org/grpc/test/bufconn` |

#### Key implementation details

- `Executor` interface with `ExecuteTask(ctx, ExecuteRequest) (*ExecuteResult, error)` and `Close() error`.
- `GRPCExecutor.Close()` returns `nil` in v0.1 (no persistent connections). Wired for interface compliance and forward compatibility with connection pooling.
- `GRPCExecutor` creates a per-task gRPC connection (no pooling in v0.1). `// TODO(v0.2): connection pooling` comment.
- Retry loop with exponential backoff + ±25% jitter: `base = 100ms * 2^attempt`, `jitter ∈ [0.75, 1.25)`, max 3 retries.
- `isTransient` classifies gRPC status codes: `Unavailable`, `ResourceExhausted`, `Aborted` → transient; `DeadlineExceeded` and all others → permanent (review S-04: retrying after timeout with the same timeout is unlikely to succeed). Non-gRPC errors (DNS, connection refused) default to transient (review B-03: these are the most common transient failures for per-task connections).
- Functional options: `WithTimeout(d)`, `WithMaxRetries(n)`.
- `grpc.WithTransportCredentials(insecure.NewCredentials())` with `// TODO(security): enable mTLS` comment.
- Agent health status check before dial: `StatusHealthy` required, else error.
- Uses existing `github.com/google/uuid` dependency for task ID generation (no new dependency required).

#### Tests (core)

- **Successful dispatch**: mock gRPC server → `COMPLETED` response → `ExecuteResult`.
- **Agent not found**: `ErrAgentNotFound` → error.
- **Agent unhealthy**: `StatusOffline` → error before dial.
- **Timeout**: blocking mock → `DeadlineExceeded`.
- **FAILED status**: `TaskResponse{Status: FAILED}` → error.
- **Context cancellation**: cancel mid-retry → `context.Canceled`.
- Race detector (`-race`).

#### PR checklist

- [x] `go test ./internal/executor/... -v -cover` passes
- [x] `go vet ./internal/executor/...` clean
- [x] No real network connections in tests (bufconn only)

#### Post-merge findings

- **N-06 (Dial options bypass)**: When `e.dialOpts` is non-empty, insecure credentials are skipped entirely (`dispatch()` L195–202). Custom dial options must independently include transport credentials. The test `setupTestEnv` correctly passes `insecure.NewCredentials()` in the bufconn dialer, but this coupling is fragile — future callers using `WithDialOptions` for non-test purposes (e.g., custom interceptors) must also remember to include transport credentials. **Should fix**: Make dial options additive — always append insecure creds (or future mTLS creds) as the base, then append caller-provided options. *(Review pr-022, Medium #1)*
- **N-07 (StatusUnknown not tested)**: `StatusUnknown` (the zero value of `AgentStatus`) is not explicitly tested. The health check `agent.Status != registry.StatusHealthy` would reject it, but an explicit test provides regression coverage. Add to PR 2b. *(Review pr-022, Coverage Gap #4)*
- **N-08 (Concurrent dispatch test)**: `ExecuteTask` will be called concurrently by the scheduler. The implementation is stateless per-call so concurrency is inherently safe, but a `sync.WaitGroup` + multiple goroutines test would provide race detector validation. Add to PR 2b. *(Review pr-022, Coverage Gap #5)*
- **N-09 (Per-dispatch timeout divergence)**: `context.WithTimeout` is created inside `dispatch()`, so each retry attempt gets a fresh timeout. This diverges from the RFC (which wraps the entire retry loop in a single timeout). The implementation is **better** — each attempt gets the full timeout window, and the outer `ctx` still governs total lifetime. Documented inline with comment. No action needed. *(Review pr-022, Info)*
- **N-10 (Simplified ExecuteRequest)**: Implementation uses flat fields (`TaskID`, `WorkflowID`, `AgentID`, `Payload`, `Context`) instead of the RFC's embedded `planner.Step`. This **improves** decoupling — the executor is independently testable without importing planner types. The scheduler (PR 3a) maps `planner.Step` → `ExecuteRequest` at the natural boundary. No action needed. *(Review pr-022, Info)*
- **N-11 (Generic Metadata)**: `ExecuteResult.Metadata map[string]string` instead of RFC's typed `TokensUsed int64` and `DurationMs int64`. More flexible and forward-compatible, but consumers must do string parsing. Acceptable for v0.1 where metadata is logged but not aggregated. No action needed. *(Review pr-022, Info)*

---

### PR 2b: `feature/v01-executor-retry` — Retry Logic & Error Classification Tests

**Depends on**: PR 2a merged
**Branch**: `feature/v01-executor-retry`
**Estimated size**: ~200–350 lines (additional tests)

#### Scope

| File | Change |
|------|--------|
| `internal/executor/executor_test.go` | Extended — `isTransient` table-driven tests + retry edge cases |

#### Tests

- **Transient retry success**: `Unavailable` × 2 → success on 3rd → result returned.
- **Permanent failure**: `InvalidArgument` → no retry, immediate error.
- **Retry exhaustion**: `Unavailable` × (maxRetries+1) → error.
- **`isTransient` table-driven**: every `codes.*` value → expected bool.
- **StatusUnknown rejected**: Agent with zero-value status → `ErrAgentNotReady`. *(Review pr-022, N-07)*
- **Concurrent dispatch**: Multiple goroutines call `ExecuteTask` simultaneously → all succeed, race detector clean. *(Review pr-022, N-08)*
- Race detector (`-race`).

#### PR checklist

- [x] `go test ./internal/executor/... -v -cover` passes
- [x] Combined coverage (2a + 2b) ≥ 80%
- [x] `go vet ./internal/executor/...` clean
- [x] No real network connections in tests (bufconn only)
- [x] `StatusUnknown` explicitly tested (N-07)
- [x] Concurrent dispatch race-tested (N-08)

#### Post-merge findings

- **N-12 (Context cancellation mid-dispatch)**: The existing `TestExecuteTask_ContextCancellation` (PR 2a) only covers cancellation during the backoff `select`. If `ctx` is cancelled during `dispatch()`, the gRPC layer returns `codes.Canceled` which `isTransient` classifies as permanent — the error surfaces as `"permanent failure ... Canceled"` rather than `context.Canceled`. A test that cancels mid-dispatch would document this behavior and prevent regressions if `codes.Canceled` is accidentally added to the transient set. **Should fix** in a follow-up. *(Review pr-023, Should Fix #1)*
- **N-13 (Concurrent retry stress test)**: `TestExecuteTask_ConcurrentDispatch` uses `WithMaxRetries(0)`, so the concurrent timer/select/backoff path is untested under concurrency. A variant where multiple goroutines all encounter transient errors and retry would exercise the backoff path under `-race`. Use `sync/atomic` counter per goroutine to track per-goroutine attempt count. **Should fix** in a follow-up. *(Review pr-023, Should Fix #2)*
- **N-14 (`isTransient(nil)` documentation test)**: `status.FromError(nil)` returns `(OK, true)`, so `isTransient(nil)` → `false`. While `nil` never reaches `isTransient` in current code, a one-line assertion documents the behavior defensively. Nice to have. *(Review pr-023, Nice to Have #1)*
- **N-15 (Wrapped non-gRPC error coverage)**: `TestIsTransient_NonGRPCError` only tests a single non-gRPC error string. Adding a wrapped error case (`fmt.Errorf("outer: %w", plainErr)`) would validate that `status.FromError` correctly returns `ok=false` for wrapped non-gRPC chains. Nice to have. *(Review pr-023, Nice to Have #2)*
- **N-16 (`codes.Internal` rationale comment)**: `codes.Internal` is classified as permanent, but some `Internal` errors can be transient (e.g., gRPC transport layer errors sometimes surface as `Internal`). The classification is defensible but a comment in `isTransient()` in `executor.go` would document the rationale. Nice to have — follow-up cleanup PR. *(Review pr-023, Nice to Have #3)*
- **N-17 (Backoff timing validation)**: No test validates actual delay ranges. The jitter formula `0.75 + rand.Float64()*0.5` produces `[0.75, 1.25)` — difficult to test without mocking `rand`. Not needed for v0.1. *(Review pr-023, Nice to Have #4)*

---

### PR 3a: `feature/v01-scheduler-core` — WorkflowScheduler Core

**Depends on**: PR 2a merged (Executor interface), PR 4 merged (`SetRunTimestamps`, `RunRetrying`)
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
- **Duplicate run prevention**: same pending run across two poll cycles → executed once (review B-02). Test by calling `pollAndExecute` twice with the same pending run still in `Pending` status in the store (the goroutine from the first poll hasn't transitioned it yet).
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
| `internal/state/state.go` | Add `RunRetrying RunStatus = 5`, `SetRunTimestamps` and `SetRunError` to `Store` interface, implement in `InMemoryStore`, extend existing `RunStatus.String()` with `RunRetrying` case |
| `internal/state/state_test.go` | Extended — tests for `SetRunTimestamps`, `SetRunError`, `RunRetrying`, `String()` |

#### Key implementation details

- `RunRetrying RunStatus = 5` — explicit integer, aligned with `proto/task.proto` `RETRYING = 5`.
- `SetRunTimestamps(ctx, runID, startedAt, finishedAt *time.Time)` — nil pointer means "leave unchanged".
- `SetRunError(ctx, runID, errMsg string)` — sets `WorkflowRun.Error` field. Used by the scheduler's `failRun` helper to persist failure reasons (review B-02). Without this method, the `Error` field on `WorkflowRun` would be unpopulable through the `Store` interface.
- `RunStatus.String()` method: extend the existing method (covers values 0–4) with `case RunRetrying: return "retrying"`. Do NOT add a new method — one already exists in `state.go`.
- Deep-copy semantics maintained for timestamp fields.

#### Tests

- `SetRunTimestamps` with both timestamps → verify via `GetRun`.
- `SetRunTimestamps` with only `StartedAt` → `FinishedAt` unchanged.
- `SetRunTimestamps` with only `FinishedAt` → `StartedAt` unchanged.
- `SetRunTimestamps` on nonexistent run → `ErrRunNotFound`.
- `SetRunError` sets `WorkflowRun.Error` → verify via `GetRun`.
- `SetRunError` on nonexistent run → `ErrRunNotFound`.
- `RunRetrying` stored and retrieved correctly.
- `RunStatus.String()` for all 6 values.
- Race detector (`-race`).

#### PR checklist

- [x] `go test ./internal/state/... -v -cover` passes
- [x] Coverage ≥ 80%
- [x] `RunRetrying = 5` (explicit, no `iota`)
- [x] `go vet ./internal/state/...` clean

#### Post-merge findings

- **N-18 (`runStatusString()` missing `RunRetrying`)**: `runStatusString()` in `internal/server/workflow_handlers.go` (L229–243) has no `case state.RunRetrying` — the new status renders as `"unknown"` in REST API JSON responses. No v0.1 code path sets `RunRetrying` yet, but **should fix** proactively: add `case state.RunRetrying: return "retrying"` and extend `TestRunStatusString` in `server_test.go`. Can be done in PR 3a (scheduler) or a standalone fix. *(Review pr-024, F-01)*
- **N-19 (`TestDeleteRunAnyStatus` incomplete)**: Existing `TestDeleteRunAnyStatus` (L354–365) iterates `{RunPending, RunRunning, RunCompleted, RunFailed, RunCancelled}` but not `RunRetrying`. Trivial one-line fix: add `RunRetrying` to the `statuses` slice. **Should fix** in PR 3a or standalone. *(Review pr-024, F-02)*
- **N-20 (No concurrent test for new methods)**: `SetRunTimestamps` and `SetRunError` are not exercised under `-race` concurrently. Existing concurrent tests cover `Create/Get/UpdateStatus/UpdateStep/Delete/List` but skip the new methods. Adding a `TestConcurrentTimestampsAndErrors` (~20 lines) would provide race detector validation. **Should fix** in a follow-up. *(Review pr-024, F-03)*
- **N-21 (Both-nil `SetRunTimestamps` untested)**: Calling `SetRunTimestamps` with both pointers `nil` is a no-op. A test documenting this behavior prevents regressions if someone adds nil validation. Nice to have. *(Review pr-024, F-04)*
- **N-22 (Empty-string `SetRunError` untested)**: Calling `SetRunError` with `""` effectively clears the error field. Documenting this expected behavior prevents ambiguity. Nice to have. *(Review pr-024, F-05)*
- **N-23 (`workflowRunResponse` missing `Error` field)**: `workflowRunResponse` DTO in `internal/server/types.go` has no `Error` field. With `SetRunError` now in the `Store` interface, the Scheduler (PR 3a) will persist error messages invisible to REST API consumers. Track for follow-up — likely RFC 0002 patch or PR 3a scope. *(Review pr-024, F-06)*

---

### PR 5: `feature/v01-scheduler-wiring` — Wire into main.go

**Depends on**: PRs 1–4 merged. **Prerequisite (review B-01):** The `--workflows-dir` flag must exist in `main.go`. This flag is specified in RFC 0002 Phase 4 (wiring). If the RFC 0002 wiring PR has not merged by the time this PR is ready, declare `--workflows-dir` (default: `"workflows/"`) directly in this PR with a comment: `// NOTE: may be superseded by RFC 0002 wiring PR if it merges first`.
**Branch**: `feature/v01-scheduler-wiring`
**Estimated size**: ~100–150 lines

#### Scope

| File | Change |
|------|--------|
| `cmd/orchestrator/main.go` | Import `internal/executor`, `internal/scheduler`. Create `GRPCExecutor` and `WorkflowScheduler`. Launch `sched.Run(ctx)` in goroutine. Remove `_ = store`, `_ = reg`, `_ = plan` placeholders. Add structured log messages. |
| `tests/integration/scheduler_executor_test.go` | New — Scheduler + Executor integration test (review P-05): real `InMemoryStore`, real `YAMLPlanner`, in-process mock gRPC server via `bufconn`, `feature-builder.yaml` fixture. Pre-register 3 agents (`planner`, `code-writer`, `code-reviewer`) in the mock registry — the `revise` step reuses `code-writer`. Verifies end-to-end run completion with correct step ordering. |

#### Key implementation details

- `executor.NewGRPCExecutor(reg, logger)` — default timeout and retries.
- `defer exec.Close()` — no-op in v0.1 (no persistent connections), wired for interface compliance and forward compatibility with connection pooling.
- `scheduler.NewWorkflowScheduler(store, reg, plan, exec, logger, workflowsDir)` with `*workflowsDir`.
- Goroutine: `go func() { if err := sched.Run(ctx); err != nil && !errors.Is(err, context.Canceled) { logger.Error(...); cancel() } }()`.
- Log `"scheduler started"` and `"executor initialized"`.
- Remove all `_ = ...` suppression lines — variables now consumed.
- Uses structured zap logger (not sugar) for new code per project convention. Existing `log.Infow` startup messages are not migrated in this PR.

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
| PR 2 (executor) exceeds 500 lines | Review burden | Pre-split into PR 2a (core) + PR 2b (retry tests) per calibration data |
| PR 3 (scheduler) exceeds 500 lines | Review burden | Pre-split into PR 3a (core) + PR 3b (template tests) per B-05 |
| RFC 0002 not merged when PR 5 ready | Blocks wiring | PRs 1–4 independent; PR 5 declares `--workflows-dir` locally if needed |
| Proto generation toolchain issues | Blocks all PRs | PR 1 validates toolchain early; generated files committed |
| gRPC dependency tree large | `go.mod` bloat | Acceptable for v0.1; prune unused transitive deps in cleanup PR |
| Mock gRPC server complexity | Test maintenance | Use `bufconn` for in-process server; keep mock implementations minimal |
| Condition evaluation omission | Incomplete feature-builder execution | Document that `revise` step always executes in v0.1; add TODO |

---

## Dependency Graph

```
PR 1 (proto-gen) ──────┐  PR 4 (state-extension) ──┐
                        ▼                            │
                   PR 2a (executor-core)             │
                        │                            │
                        ▼                            │
                   PR 2b (executor-retry)            │
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

> **⚠️ GATE (review B-05):** PR 3a has a **compile-time** dependency on both PR 2a (Executor interface) and PR 4 (`SetRunTimestamps`, `RunRetrying`). Neither can be skipped. PR 4 must merge **before** PR 3a despite its higher number.
>
> **⚠️ RFC 0002 GATE:** PRs 1–4 are independent of RFC 0002. PR 5 (wiring) requires RFC 0002 implementation PRs to be merged (for `--workflows-dir` flag and HTTP server). If RFC 0002 has not landed, PR 5 declares `--workflows-dir` locally.

PR 4 (state extension) can proceed in parallel with PRs 1–2 since it modifies `internal/state/` independently.

> **Recommended implementation order (review S-08):** PR numbering does not imply execution order. The recommended merge sequence is: **PR 1 ‖ PR 4** (parallel) → **PR 2a** → **PR 2b** → **PR 3a** → **PR 3b** → **PR 5**. PR 4 must merge before PR 3a despite its higher number.
