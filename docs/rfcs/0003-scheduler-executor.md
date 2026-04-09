# RFC 0003 — Scheduler & Executor (Parallel Stage Execution + gRPC Task Dispatch)

**Type**: architecture
**Status**: 📋 Proposed
**Author**: Orchestr8 team
**Date**: 2026-04-09
**Target**: v0.1 (MVP)
**Depends on**: RFC 0001, RFC 0002
**Superseded by**: None

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

Implement the Scheduler and Executor — the two components that bring the orchestrator to life by executing workflow runs end-to-end. The Scheduler picks up pending `WorkflowRun` entries, plans their execution via the Planner, and drives stages through to completion. The Executor handles gRPC communication with Python agent processes, dispatching `TaskRequest` messages and collecting `TaskResponse` results. Together, these components close the loop from `POST /api/v1/workflows/run` (RFC 0002) through to a completed workflow with per-step outputs.

## Motivation

RFC 0001 built the foundational data structures (State, Registry, Planner) and RFC 0002 exposed them through the REST API. However, submitting a workflow run via `POST /api/v1/workflows/run` currently creates a `Pending` run that remains pending indefinitely — no component transitions runs through execution. The dependency chain is now:

```
CLI ──REST──► HTTP Server (RFC 0002)
                   │
                   └─ POST /api/v1/workflows/run  ──►  WorkflowRun{Status: Pending}
                                                              │
                                                              ▼
                                                     Scheduler (this RFC)
                                                              │
                                                 ┌────────────┼────────────┐
                                                 ▼            ▼            ▼
                                            Stage 0      Stage 1      Stage N
                                           [step_a]   [step_b,      [step_d]
                                                        step_c]
                                                 │            │            │
                                                 ▼            ▼            ▼
                                              Executor ──gRPC──► Python Agents
```

Without the Scheduler and Executor, the orchestrator is a well-structured API that accepts and stores workflow submissions but performs no work. The Rust CLI can query run status but will always see `"pending"`. Completing this RFC delivers the first end-to-end workflow execution capability.

If we do nothing, the system remains non-functional from the user's perspective — workflows are accepted but never executed.

## Goals

1. Implement `WorkflowScheduler` in `internal/scheduler/` — pick up pending runs, plan execution, drive stages sequentially, execute steps within each stage in parallel.
2. Implement `GRPCExecutor` in `internal/executor/` — dispatch `TaskRequest` to agents via gRPC, collect `TaskResponse`, enforce per-step timeouts.
3. Update `state.WorkflowRun` step states in real-time as execution progresses (`Pending → Running → Completed|Failed`).
4. Update `state.WorkflowRun` status: `Pending → Running → Completed|Failed|Cancelled`.
5. Resolve template variables (`{{ user_request }}`, `{{ steps.<id>.output }}`) at execution time using `planner.ResolveInputs`.
6. Implement basic retry logic for transient gRPC failures (exponential backoff, configurable max retries).
7. Wire the Scheduler and Executor into `cmd/orchestrator/main.go`.
8. Generate Go gRPC client stubs from `proto/task.proto`.
9. Achieve ≥ 80% test coverage for `internal/scheduler/` and `internal/executor/`.

## Non-Goals

- **Agent-side implementation.** Python agent gRPC server (`agents/server.py`) is a separate RFC. This RFC only implements the Go client side. Tests use a mock gRPC server.
- **Streaming execution (`ExecuteTaskStream`).** v0.1 uses unary `ExecuteTask` only. Streaming progress via `TaskProgress` is deferred to v0.2.
- **SSE streaming to CLI.** `GET /api/v1/stream/events` is v0.2. The CLI polls `GET /api/v1/workflows/{id}/status` in v0.1.
- **Condition evaluation.** Step conditions (`{{ steps.review.output.approved == false }}`) are parsed and stored but deferred to a follow-up. All steps in the DAG execute unconditionally in v0.1. A `// TODO(v0.2): evaluate step conditions before dispatch` comment marks the integration point.
- **Circuit breaker.** `internal/resilience/` is a stub. Basic retry logic is included inline in the Executor; circuit breaker integration is deferred.
- **Dead letter queue.** Failed tasks are recorded in the state store but not queued for retry/inspection beyond the inline retry loop.
- **Cost tracking.** Token usage from `TaskResponse.metadata` is logged but not aggregated. `internal/cost/` integration is deferred.
- **Health check polling.** The Executor calls `HealthCheck` only as a connection verification before task dispatch, not as a periodic background poll.
- **Connection pooling.** v0.1 creates a new gRPC connection per task dispatch. Connection reuse/pooling is deferred to a performance optimization RFC.
- **Approval gates.** `approval_required` and `approval_timeout` step fields are not evaluated.
- **State transition validation.** `UpdateRunStatus` accepts any transition in v0.1 (per RFC 0001). A formal state machine is deferred.
- **Cancellation propagation.** When a run is cancelled (e.g., via future `POST /api/v1/workflows/{id}/cancel`), in-flight gRPC calls are not interrupted in v0.1. The Scheduler checks for cancellation between stages only.
- **Multi-node distribution.** v0.3 scope. Single-node only in v0.1.
- **Persistent state.** In-memory only. Process restart loses all state (per RFC 0001).

## Design / Implementation

### Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                  WorkflowScheduler                   │
│                                                     │
│  1. Poll for Pending runs (from State Store)        │
│  2. Call Planner.Plan() → ExecutionPlan             │
│  3. Transition run to Running                       │
│  4. For each Stage:                                 │
│     a. Fan-out: dispatch steps in parallel          │
│     b. Barrier: wait for all steps to complete      │
│     c. Collect outputs for template resolution      │
│  5. Transition run to Completed or Failed           │
│                                                     │
│  Uses: Store, Registry, Planner, Executor           │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│                   GRPCExecutor                       │
│                                                     │
│  1. Look up agent address in Registry               │
│  2. Dial gRPC connection                            │
│  3. Build TaskRequest (resolved input + context)    │
│  4. Call AgentService.ExecuteTask()                  │
│  5. Handle response or error (retry on transient)   │
│  6. Return result to Scheduler                      │
│                                                     │
│  Uses: Registry, generated gRPC stubs               │
└─────────────────────────────────────────────────────┘
```

### Scheduler Interface

**Package:** `internal/scheduler/`

```go
// Scheduler executes workflow runs by driving the Planner's ExecutionPlan
// through the Executor, updating state at each step.
type Scheduler interface {
    // Run starts the scheduler loop. It blocks until ctx is cancelled.
    // The scheduler polls for pending runs and executes them.
    Run(ctx context.Context) error
}
```

#### WorkflowScheduler Implementation

```go
type WorkflowScheduler struct {
    store     state.Store
    registry  registry.Registry
    planner   planner.Planner
    executor  Executor
    logger    *zap.Logger
    pollInterval time.Duration // default: 1 second
}

func NewWorkflowScheduler(
    store state.Store,
    registry registry.Registry,
    planner planner.Planner,
    executor Executor,
    logger *zap.Logger,
    workflowsDir string,
) *WorkflowScheduler
```

The constructor accepts all dependencies via interfaces, enabling test injection with mocks.

#### Scheduler Loop

The scheduler runs a polling loop that:

1. Calls `store.ListRuns(ctx)` and filters for `RunPending` status.
2. For each pending run, spawns a goroutine to execute the run (bounded by a semaphore to prevent unbounded concurrency).
3. Sleeps for `pollInterval` before the next poll.

```go
func (s *WorkflowScheduler) Run(ctx context.Context) error {
    ticker := time.NewTicker(s.pollInterval)
    defer ticker.Stop()
    for {
        select {
        case <-ctx.Done():
            return ctx.Err()
        case <-ticker.C:
            s.pollAndExecute(ctx)
        }
    }
}
```

The maximum concurrent workflow runs is capped via a semaphore (`chan struct{}`), defaulting to 10. This prevents a burst of pending runs from overwhelming the executor or agents.

> **Note:** The polling model is chosen for v0.1 simplicity. A future RFC may replace polling with an event-driven approach (e.g., the REST API handler signals the scheduler directly via a channel when a new run is created). The `Store` interface would need a `Watch` method or the server would push to a scheduler channel.

#### Run Execution Flow

```go
func (s *WorkflowScheduler) executeRun(ctx context.Context, run *state.WorkflowRun) {
    // 1. Parse the workflow YAML from the stored WorkflowID.
    //    The workflow file path is resolved using the same workflowsDir
    //    that the server uses. The scheduler receives workflowsDir at construction.
    workflowPath := s.resolveWorkflowPath(run.WorkflowID)
    workflow, err := s.planner.Parse(ctx, workflowPath)
    if err != nil {
        s.failRun(ctx, run.ID, "workflow parse error: "+err.Error())
        return
    }

    // 2. Validate DAG.
    if err := s.planner.ValidateDAG(ctx, workflow); err != nil {
        s.failRun(ctx, run.ID, "DAG validation error: "+err.Error())
        return
    }

    // 3. Compute execution plan.
    plan, err := s.planner.Plan(ctx, workflow)
    if err != nil {
        s.failRun(ctx, run.ID, "planning error: "+err.Error())
        return
    }

    // 4. Transition to Running.
    if err := s.store.UpdateRunStatus(ctx, run.ID, state.RunRunning); err != nil {
        s.logger.Error("failed to update run status", zap.String("run_id", run.ID), zap.Error(err))
        return
    }

    // 5. Execute stages sequentially.
    outputs := make(map[string]string) // step ID → output
    for stageIdx, stage := range plan.Stages {
        if err := s.executeStage(ctx, run, stageIdx, stage, outputs); err != nil {
            s.failRun(ctx, run.ID, fmt.Sprintf("stage %d failed: %s", stageIdx, err.Error()))
            return
        }
    }

    // 6. Transition to Completed.
    if err := s.store.UpdateRunStatus(ctx, run.ID, state.RunCompleted); err != nil {
        s.logger.Error("failed to update run status to completed",
            zap.String("run_id", run.ID), zap.Error(err))
    }
    now := time.Now()
    _ = now // TODO(phase 4): use SetRunTimestamps to record FinishedAt
}
```

> **Note (F-05 from RFC 0001 review):** The current `Store` interface has no method to set `WorkflowRun.FinishedAt`. This RFC adds `UpdateRunTimestamps(ctx, runID, startedAt, finishedAt *time.Time)` to the `Store` interface, or extends `UpdateRunStatus` with optional timestamp parameters. See Phase 1 for details.

#### Stage Execution (Parallel Fan-Out / Barrier)

Each stage contains one or more steps that can execute in parallel. The scheduler uses a `sync.WaitGroup` and an error channel for fan-out/fan-in:

```go
func (s *WorkflowScheduler) executeStage(
    ctx context.Context,
    run *state.WorkflowRun,
    stageIdx int,
    steps []planner.Step,
    outputs map[string]string,
) error {
    var wg sync.WaitGroup
    errCh := make(chan error, len(steps))

    for _, step := range steps {
        wg.Add(1)
        go func(st planner.Step) {
            defer wg.Done()
            if err := s.executeStep(ctx, run, st, outputs); err != nil {
                errCh <- fmt.Errorf("step %s: %w", st.ID, err)
            }
        }(step)
    }

    wg.Wait()
    close(errCh)

    // Collect errors. Fail the stage on first error.
    // In v0.1, any step failure fails the entire stage (and thus the run).
    // TODO(v0.2): support partial failure with optional steps.
    var errs []error
    for err := range errCh {
        errs = append(errs, err)
    }
    if len(errs) > 0 {
        return errors.Join(errs...)
    }
    return nil
}
```

> **Concurrency safety for `outputs` map:** The `outputs` map is written by `executeStep` after each step completes. Steps within a stage execute in parallel, but each step writes a **unique** key (its own step ID). Concurrent writes to distinct keys in a Go map are still a data race. The fix is to use a `sync.Mutex` (not `sync.RWMutex` — writes only during execution, reads only after the stage barrier) protecting `outputs` writes, or collect results via a result channel and merge after the barrier.

#### Step Execution

```go
func (s *WorkflowScheduler) executeStep(
    ctx context.Context,
    run *state.WorkflowRun,
    step planner.Step,
    outputs map[string]string,
) error {
    // 1. Update step state to Running.
    s.store.UpdateStepState(ctx, run.ID, state.StepState{
        StepID:    step.ID,
        Status:    state.RunRunning,
        StartedAt: time.Now(),
    })

    // 2. Resolve template variables in step input.
    //    Prerequisite: WorkflowRun.Inputs must be populated by RFC 0002's
    //    handleRunWorkflow from the request body's "inputs" field.
    //    If Inputs is nil, ResolveInputs will fail on {{ user_request }} templates.
    resolvedInput, err := planner.ResolveInputs(step, outputs, run.Inputs, s.logger)
    if err != nil {
        s.store.UpdateStepState(ctx, run.ID, state.StepState{
            StepID:     step.ID,
            Status:     state.RunFailed,
            Error:      err.Error(),
            FinishedAt: time.Now(),
        })
        return fmt.Errorf("resolve inputs: %w", err)
    }

    // 3. Dispatch to agent via Executor.
    result, err := s.executor.ExecuteTask(ctx, ExecuteRequest{
        RunID:      run.ID,
        WorkflowID: run.WorkflowID,
        Step:       step,
        Input:      resolvedInput,
        Context:    outputs, // prior step outputs as context
    })
    if err != nil {
        s.store.UpdateStepState(ctx, run.ID, state.StepState{
            StepID:     step.ID,
            Status:     state.RunFailed,
            Error:      err.Error(),
            FinishedAt: time.Now(),
        })
        return fmt.Errorf("execute task: %w", err)
    }

    // 4. Store output and update step state to Completed.
    outputs[step.ID] = result.Output // protected by mutex (see stage execution)
    s.store.UpdateStepState(ctx, run.ID, state.StepState{
        StepID:     step.ID,
        Status:     state.RunCompleted,
        Output:     result.Output,
        FinishedAt: time.Now(),
    })

    s.logger.Info("step completed",
        zap.String("run_id", run.ID),
        zap.String("step_id", step.ID),
        zap.Int64("tokens_used", result.TokensUsed),
        zap.Int64("duration_ms", result.DurationMs),
    )
    return nil
}
```

### Executor Interface

**Package:** `internal/executor/`

```go
// Executor dispatches tasks to agents and returns results.
type Executor interface {
    ExecuteTask(ctx context.Context, req ExecuteRequest) (*ExecuteResult, error)
    Close() error
}

// ExecuteRequest contains everything needed to dispatch a task to an agent.
type ExecuteRequest struct {
    RunID      string
    WorkflowID string
    Step       planner.Step
    Input      string            // resolved input (template variables substituted)
    Context    map[string]string // prior step outputs
}

// ExecuteResult contains the agent's response.
type ExecuteResult struct {
    Output     string
    TokensUsed int64
    DurationMs int64
}
```

#### GRPCExecutor Implementation

```go
type GRPCExecutor struct {
    registry   registry.Registry
    logger     *zap.Logger
    timeout    time.Duration // per-task timeout, default: 5 minutes
    maxRetries int           // default: 3
}

func NewGRPCExecutor(
    registry registry.Registry,
    logger *zap.Logger,
    opts ...ExecutorOption,
) *GRPCExecutor
```

Functional options pattern for configuring timeout and retry parameters:

```go
type ExecutorOption func(*GRPCExecutor)

func WithTimeout(d time.Duration) ExecutorOption
func WithMaxRetries(n int) ExecutorOption
```

#### Task Dispatch Flow

```go
func (e *GRPCExecutor) ExecuteTask(ctx context.Context, req ExecuteRequest) (*ExecuteResult, error) {
    // 1. Look up agent in registry.
    agent, err := e.registry.Get(ctx, req.Step.AgentID)
    if err != nil {
        return nil, fmt.Errorf("agent lookup %q: %w", req.Step.AgentID, err)
    }

    // 2. Verify agent is healthy.
    if agent.Status != registry.StatusHealthy {
        return nil, fmt.Errorf("agent %q is %s, not healthy", agent.ID, agent.Status)
    }

    // 3. Dial gRPC connection with timeout.
    ctx, cancel := context.WithTimeout(ctx, e.timeout)
    defer cancel()

    conn, err := grpc.NewClient(agent.Address,
        grpc.WithTransportCredentials(insecure.NewCredentials()),
    )
    if err != nil {
        return nil, fmt.Errorf("grpc dial %q: %w", agent.Address, err)
    }
    defer conn.Close()

    // 4. Build TaskRequest.
    client := pb.NewAgentServiceClient(conn)
    taskReq := &pb.TaskRequest{
        TaskId:     uuid.NewString(),
        WorkflowId: req.WorkflowID,
        AgentId:    req.Step.AgentID,
        Payload:    req.Input,
        Context:    req.Context,
        Config: &pb.TaskConfig{
            TimeoutSeconds: int32(e.timeout.Seconds()),
        },
    }

    // 5. Call ExecuteTask with retry on transient errors.
    var resp *pb.TaskResponse
    for attempt := 0; attempt <= e.maxRetries; attempt++ {
        resp, err = client.ExecuteTask(ctx, taskReq)
        if err == nil {
            break
        }
        // Classify gRPC error: retry on Unavailable, DeadlineExceeded (transient);
        // fail immediately on InvalidArgument, NotFound, PermissionDenied (permanent).
        if !isTransient(err) {
            return nil, fmt.Errorf("permanent gRPC error from agent %q: %w", agent.ID, err)
        }
        if attempt < e.maxRetries {
            backoff := time.Duration(1<<uint(attempt)) * 100 * time.Millisecond
            e.logger.Warn("retrying transient gRPC error",
                zap.String("agent_id", agent.ID),
                zap.Int("attempt", attempt+1),
                zap.Duration("backoff", backoff),
                zap.Error(err),
            )
            select {
            case <-ctx.Done():
                return nil, ctx.Err()
            case <-time.After(backoff):
            }
        }
    }
    if err != nil {
        return nil, fmt.Errorf("gRPC call to agent %q failed after %d retries: %w",
            agent.ID, e.maxRetries, err)
    }

    // 6. Check response status.
    if resp.Status == pb.TaskStatus_FAILED {
        return nil, fmt.Errorf("agent %q returned FAILED: %s", agent.ID, resp.ErrorMessage)
    }

    // 7. Return result.
    //    Metadata is a map<string, string> in proto/task.proto — extract
    //    numeric values via string parsing (no typed accessors on maps).
    tokensUsed, _ := strconv.ParseInt(resp.Metadata["tokens_used"], 10, 64)
    durationMs, _ := strconv.ParseInt(resp.Metadata["duration_ms"], 10, 64)
    return &ExecuteResult{
        Output:     resp.Result,
        TokensUsed: tokensUsed,
        DurationMs: durationMs,
    }, nil
}
```

#### Transient Error Classification

```go
func isTransient(err error) bool {
    st, ok := status.FromError(err)
    if !ok {
        return false
    }
    switch st.Code() {
    case codes.Unavailable, codes.DeadlineExceeded, codes.ResourceExhausted, codes.Aborted:
        return true
    default:
        return false
    }
}
```

`Unavailable` covers agent restart, network blip, and load-balancer draining. `DeadlineExceeded` covers slow agent responses that may succeed on retry. `ResourceExhausted` covers agent-side rate limiting. `Aborted` covers transient concurrency conflicts. All other codes (`InvalidArgument`, `NotFound`, `PermissionDenied`, `Internal`, `Unimplemented`) are treated as permanent.

### gRPC Proto Compilation

`proto/task.proto` defines the `AgentService` with `ExecuteTask`, `ExecuteTaskStream`, and `HealthCheck` RPCs. This RFC generates Go stubs:

```
proto/task.proto  ──protoc──►  internal/generated/taskpb/task.pb.go
                               internal/generated/taskpb/task_grpc.pb.go
```

The generated files live in `internal/generated/taskpb/` — matching the existing `go_package` option (`github.com/orchestr8/orchestr8/internal/generated/taskpb`) and the Makefile's `PROTO_GO_OUT := internal/generated`. The existing `make proto` target already generates Go stubs; no additional Makefile target is needed.

**Dependencies added to `go.mod`:**
- `google.golang.org/grpc` — gRPC client and server framework
- `google.golang.org/protobuf` — protobuf runtime

### State Store Extensions

RFC 0001's `Store` interface lacks facilities for:
1. Setting `WorkflowRun.FinishedAt` when a run completes or fails.
2. Setting `WorkflowRun.StartedAt` to the actual execution start (currently set at submission time per RFC 0002).

This RFC extends the `Store` interface with one additional method:

```go
// SetRunTimestamps updates the started_at and finished_at timestamps for a run.
// Either pointer may be nil to leave that field unchanged.
SetRunTimestamps(ctx context.Context, runID string, startedAt, finishedAt *time.Time) error
```

This is the minimal extension that avoids breaking the existing `UpdateRunStatus` signature while giving the Scheduler the ability to record accurate timing. The alternative — adding timestamp parameters to `UpdateRunStatus` — was rejected because it changes the signature used by multiple callers (REST API, tests) for a concern that only the Scheduler has.

### `RunStatus` Extension: `RunRetrying`

RFC 0001 reserved `RunStatus = 5` for alignment with `proto/task.proto`'s `TaskStatus.RETRYING`. This RFC adds:

```go
RunRetrying RunStatus = 5
```

This value is set on a step's `StepState.Status` when the Executor retries a transient gRPC failure. It is transitional — the step moves to `RunCompleted` on success or `RunFailed` when retries are exhausted. The run-level status never becomes `RunRetrying`; it remains `RunRunning` during step retries.

> **Spec reconciliation:** `proto/task.proto` defines `RETRYING = 5`, Go now has `RunRetrying = 5`. The inline proto in `ai-agents-orchestration-spec.md` §4.3 omits `RETRYING` — it should be updated in a follow-up documentation PR.

### Scheduler-to-Workflow Path Resolution

The Scheduler needs to resolve `WorkflowRun.WorkflowID` (e.g., `"feature-builder"`) to a filesystem path for `planner.Parse`. RFC 0002's `Server.resolveWorkflowPath` performs this resolution, but it is scoped to the `internal/server/` package.

Rather than importing `internal/server/` from `internal/scheduler/` (which would create a circular-dependency risk and couple the Scheduler to the HTTP layer), the Scheduler receives `workflowsDir string` at construction and performs its own path resolution:

```go
func (s *WorkflowScheduler) resolveWorkflowPath(workflowID string) string {
    return filepath.Join(s.workflowsDir, workflowID+".yaml")
}
```

The Scheduler's resolution is simpler than the server's — it does not need path traversal protection because `workflowID` comes from the trusted `WorkflowRun.WorkflowID` field, which was already validated by the REST API at submission time (regex check + `EvalSymlinks` prefix check). A `// NOTE: workflowID is pre-validated by the REST API; no path traversal check needed here` comment documents this trust boundary.

### Wiring into main.go

Update `cmd/orchestrator/main.go` to:

1. Generate gRPC stubs (build-time, via `make proto`).
2. Create `executor.NewGRPCExecutor(reg, logger)`.
3. `defer exec.Close()` — no-op in v0.1 (no persistent connections), wired for interface compliance and forward compatibility with connection pooling.
4. Create `scheduler.NewWorkflowScheduler(store, reg, plan, exec, logger, workflowsDir)` with `workflowsDir` from the existing `--workflows-dir` flag.
5. Launch `sched.Run(ctx)` in a goroutine with error propagation via `cancel()`.
6. Log `"scheduler started"`.

This satisfies the deferred portion of TODO step 8 ("scheduler deferred to RFC 0003") in `main.go`.

## Security Considerations

### gRPC Without TLS (v0.1)

v0.1 uses insecure gRPC credentials (`grpc.WithTransportCredentials(insecure.NewCredentials())`). This is acceptable for the docker-compose/localhost deployment. Any production deployment MUST enable mTLS or equivalent transport security. A `// TODO(security): enable mTLS for production gRPC connections` comment is placed at the `grpc.NewClient` call.

### Agent Address Trust

The Executor looks up agent addresses from the Registry, which is populated via the (unauthenticated) REST API in v0.1. A malicious actor with network access to the REST API could register a rogue agent address, causing the Executor to send workflow data to an attacker-controlled endpoint. The mitigation in v0.1 is the same as RFC 0002: bind to `127.0.0.1` (loopback) by default. The security RFC will add agent authentication.

### TaskRequest Payload Content

`TaskRequest.payload` contains the resolved step input, which includes user-provided inputs from `WorkflowRun.Inputs`. These values are treated as opaque strings and passed directly to the agent — they are not evaluated, executed, or interpreted by the orchestrator. The agent is responsible for its own input validation and sandboxing.

`TaskRequest.context` contains prior step outputs. These are also opaque strings. The single-pass template resolution in `ResolveInputs` (RFC 0001) prevents second-order injection.

### Concurrency Limits

The scheduler's semaphore (`maxConcurrentRuns`, default 10) prevents resource exhaustion from a flood of pending runs. Without this, a malicious or misconfigured client could submit thousands of runs, each spawning goroutines that dial gRPC connections and consume file descriptors.

### No Workflow Replay Protection

A `WorkflowRun` that fails and is not deleted can be re-executed if a scheduler bug or race condition picks it up again. In v0.1, the `Pending → Running` transition (`UpdateRunStatus`) does not use compare-and-swap, so two scheduler goroutines could race to execute the same run. The mitigation is the semaphore + single-scheduler design. RFC 0003 follow-up should add an atomic `ClaimRun(runID) bool` method to the Store interface.

> **Note:** This is documented as a known risk with a `// TODO(v0.2): add ClaimRun for atomic pending→running transition` comment.

## Phased Implementation Plan

### Phase 1: Proto Generation + Go gRPC Stubs (~50 LOC config, generated output)

Summary: Set up protobuf compilation toolchain and generate Go stubs from `proto/task.proto`.

**Deliverables:**
1. `internal/generated/taskpb/task.pb.go` — generated protobuf types.
2. `internal/generated/taskpb/task_grpc.pb.go` — generated gRPC client/server stubs.
3. Reuse existing `make proto` target (already generates Go stubs via `PROTO_GO_OUT := internal/generated`).
4. `go.mod` / `go.sum` — add `google.golang.org/grpc` and `google.golang.org/protobuf`.

**Dependencies:** None (proto files exist from project inception).

### Phase 2: Executor (~300 LOC, implementation + tests)

Summary: Implement `GRPCExecutor` with retry logic and transient error classification.

**Deliverables:**
1. `internal/executor/executor.go` — `Executor` interface, `ExecuteRequest`, `ExecuteResult`, `GRPCExecutor` implementation, `isTransient`, functional options.
2. `internal/executor/executor_test.go` — unit tests with mock gRPC server.

**Dependencies:** Phase 1 (generated gRPC stubs).

### Phase 3: Scheduler (~400 LOC, implementation + tests)

Summary: Implement `WorkflowScheduler` with polling loop, parallel stage execution, and state updates.

**Deliverables:**
1. `internal/scheduler/scheduler.go` — `Scheduler` interface, `WorkflowScheduler` implementation, stage/step execution, outputs collection, state transitions.
2. `internal/scheduler/scheduler_test.go` — unit tests with mock Executor, Store, and Planner.

**Dependencies:** Phase 2 (Executor interface), Phase 4 (`SetRunTimestamps`, `RunRetrying`), RFC 0001 (Store, Planner), RFC 0002 (runs in Pending state).

### Phase 4: Store Extension + State Wiring (~100 LOC)

Summary: Extend `Store` interface with `SetRunTimestamps`, add `RunRetrying` status, implement in `InMemoryStore`.

**Deliverables:**
1. `internal/state/state.go` — add `SetRunTimestamps` method, `RunRetrying` constant.
2. `internal/state/state_test.go` — tests for new method and status.

**Dependencies:** RFC 0001 (existing Store implementation). Independent of Phases 2–3; must complete before Phase 3 (scheduler needs `SetRunTimestamps` and `RunRetrying`).

### Phase 5: Wire into main.go (~50 LOC)

Summary: Instantiate Executor and Scheduler in `main.go`, launch scheduler loop.

**Deliverables:**
1. `cmd/orchestrator/main.go` — wire executor, scheduler; remove `_ = store`, `_ = reg`, `_ = plan` placeholders; launch scheduler goroutine.

**Dependencies:** Phases 1–4.

**Total estimated scope:** ~900 LOC implementation + tests. Generated proto output not counted.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Go orchestrator | `internal/executor/executor.go` | New — `Executor` interface, `GRPCExecutor`, retry logic, error classification |
| Go orchestrator | `internal/executor/executor_test.go` | New — unit tests with mock gRPC server |
| Go orchestrator | `internal/scheduler/scheduler.go` | Replace TODO stubs — `Scheduler` interface, `WorkflowScheduler`, polling loop, stage/step execution |
| Go orchestrator | `internal/scheduler/scheduler_test.go` | New — unit tests with mock dependencies |
| Go orchestrator | `internal/state/state.go` | Add `SetRunTimestamps` method to `Store` interface and `InMemoryStore`, add `RunRetrying` constant |
| Go orchestrator | `internal/state/state_test.go` | Extended — tests for `SetRunTimestamps` and `RunRetrying` |
| Go orchestrator | `cmd/orchestrator/main.go` | Wire Executor + Scheduler, remove unused-var placeholders, launch scheduler goroutine |
| Go generated | `internal/generated/taskpb/task.pb.go` | Generated — protobuf message types |
| Go generated | `internal/generated/taskpb/task_grpc.pb.go` | Generated — gRPC client and server stubs |
| Go dependency | `go.mod`, `go.sum` | Add `google.golang.org/grpc`, `google.golang.org/protobuf` |
| Build | `Makefile` | Reuse existing `proto` target (already generates Go stubs) |
| Proto | `proto/task.proto` | `go_package` already set to `internal/generated/taskpb` — no change needed |

## Test Strategy

- **Unit tests per package** using `testify/assert` and `testify/require`.

### Executor Tests

- **Successful dispatch**: mock gRPC server returns `COMPLETED` with result → `ExecuteResult` returned correctly.
- **Agent not found**: registry returns `ErrAgentNotFound` → error propagated.
- **Agent unhealthy**: registry returns agent with `StatusOffline` → error before gRPC dial.
- **Transient retry**: mock server fails with `Unavailable` twice, succeeds on third attempt → result returned, retry count logged.
- **Permanent failure**: mock server returns `InvalidArgument` → no retry, immediate error.
- **Retry exhaustion**: mock server returns `Unavailable` on all attempts → error after max retries.
- **Timeout**: mock server blocks beyond executor timeout → `DeadlineExceeded` error.
- **Agent returns FAILED status**: mock server returns `TaskResponse` with `FAILED` status → error with `ErrorMessage`.
- **Context cancellation**: cancel context mid-retry → returns `context.Canceled`.
- **gRPC error classification**: table-driven test for `isTransient` across all gRPC status codes.
- Race detector (`-race` flag).

### Scheduler Tests

- **End-to-end single-step**: create pending run, mock Executor returns success → run transitions to `Completed`, step output stored.
- **Multi-stage sequential**: 3-stage workflow → stages execute in order, template variables resolved between stages.
- **Parallel stage**: stage with 2 steps → both dispatched concurrently (verify via timing or mock synchronization).
- **Step failure fails run**: mock Executor returns error → run transitions to `Failed`, error message stored.
- **Template resolution**: `{{ user_request }}` and `{{ steps.<id>.output }}` resolved correctly from inputs and prior outputs.
- **Template resolution failure**: missing variable → run fails with descriptive error.
- **Polling loop**: start scheduler, create pending run, verify it executes within 2× poll interval.
- **Graceful shutdown**: cancel context → `Run()` returns `context.Canceled`.
- **Concurrent run limit**: submit more runs than semaphore capacity → excess runs wait.
- **Empty run list**: no pending runs → no-op poll cycle (no errors).
- **Parse failure**: workflow file missing or invalid YAML → run fails with parse error.
- **DAG validation failure**: cyclic workflow → run fails with cycle error.
- Race detector (`-race` flag).

### State Extension Tests

- **`SetRunTimestamps`**: set both timestamps → verify via `GetRun`; set only `StartedAt` → `FinishedAt` unchanged; nonexistent run → `ErrRunNotFound`.
- **`RunRetrying`**: set step status to `RunRetrying` → verify via `GetRun`.

### Integration Tests

- **Scheduler + Executor integration**: use a real (in-process) mock gRPC server, real `InMemoryStore`, real `YAMLPlanner`, and the `feature-builder.yaml` fixture. Submit a run, verify it completes with 4 steps in correct order.
- **Build smoke test**: `go build ./cmd/orchestrator` succeeds after wiring.

## Open Questions

1. ~~**Polling vs. event-driven scheduler**: Should the scheduler poll `ListRuns` or receive events from the REST API?~~
   **Resolved**: Polling for v0.1 simplicity. Event-driven via channel injection is a v0.2 optimization. The polling interval defaults to 1 second, which is acceptable for the expected v0.1 run volume. (2026-04-09)

2. ~~**Connection pooling**: Should the Executor maintain a pool of gRPC connections per agent?~~
   **Resolved**: No pooling in v0.1 — create a new connection per task dispatch, close after response. Connection pooling is a performance optimization for v0.2 when agents handle concurrent tasks. The overhead of per-task connection setup is negligible for v0.1 run volumes. (2026-04-09)

3. **Condition evaluation**: How should `{{ steps.review.output.approved == false }}` be evaluated? This requires parsing the step output as JSON/YAML and evaluating a boolean expression. Deferred — all steps execute unconditionally in v0.1.

4. **Scheduler restart recovery**: If the process restarts, all `Running` runs in the in-memory store are lost. Should the scheduler detect orphaned `Running` runs? Not applicable in v0.1 (in-memory store loses all state). Relevant for v0.2 persistent store.

5. **Concurrent scheduler instances**: In v0.3 multi-node deployment, multiple scheduler instances could race to execute the same pending run. Requires distributed locking or compare-and-swap at the store level. Out of scope for v0.1 single-node.

## Decision / Next Steps

Once this RFC is accepted:

1. Create feature branches per the PR plan (`0003-pr-plan.md`).
2. Implement in phase order (Proto Gen → Executor → Scheduler → State Extension → Wiring).
3. PR < 500 lines per phase; squash merge to `main`.
4. **Next RFC**: `0004-grpc-agent-server.md` — Python agent-side gRPC server implementation (`agents/server.py`) implementing `AgentService` from `proto/task.proto`.

## Related Documentation

- [ai-agents-orchestration-spec.md](../ai-agents-orchestration-spec.md) — §2.3 Tasks, §2.5 Workflows, §3.1–3.2 gRPC Communication, §6.7 Resilience, §9 Execution Flow
- [orchestr8-extension-spec.md](../orchestr8-extension-spec.md) — v0.2+ streaming, channels, memory
- [0001-core-orchestration-pipeline.md](0001-core-orchestration-pipeline.md) — State, Registry, Planner
- [0002-rest-api-server.md](0002-rest-api-server.md) — REST API, workflow submission
- [BRANCHING.md](../BRANCHING.md) — Branch naming and PR size guidelines
- Existing stubs: `internal/scheduler/scheduler.go`, `internal/executor/executor.go`, `internal/resilience/resilience.go`
- Proto definitions: `proto/task.proto`, `proto/agent_message.proto`
- Workflow fixture: `workflows/feature-builder.yaml`
