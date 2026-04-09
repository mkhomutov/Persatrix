# RFC 0001 — Core Orchestration Pipeline (Planner + State + Registry)

**Type**: architecture  
**Status**: 📋 Proposed  
**Author**: Orchestr8 team  
**Date**: 2026-04-08  
**Target**: v0.1 (MVP)  
**Depends on**: None  
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

Implement the three foundational Go orchestrator components that the entire execution pipeline depends on: **InMemoryStateStore**, **InMemoryRegistry**, and **YAMLPlanner**. These are the minimum viable internals needed before the Scheduler, Executor, REST API, or gRPC server can function. Without these, the orchestrator is an empty shell that starts, logs, and exits.

## Motivation

The orchestrator's `main.go` currently initializes logging and graceful shutdown but performs no actual work. All `internal/` packages are interface definitions and TODO stubs. The dependency chain for end-to-end workflow execution is:

```
Workflow YAML → Planner (parse + DAG) → Scheduler → Executor → Agents
                    ↕                       ↕           ↕
                  State Store           State Store   Registry
```

**Planner**, **State**, and **Registry** sit at the bottom of this dependency graph. Nothing above them can be implemented or tested without them. They have zero external dependencies (no gRPC, no HTTP, no LLM calls) and can be built and unit-tested in complete isolation.

If we do nothing, the project remains a well-documented skeleton with no executable logic.

## Goals

1. Implement `InMemoryStateStore` in `internal/state/` — track workflow and step execution state.
2. Implement `InMemoryRegistry` in `internal/registry/` — register, look up, and health-track agents.
3. Implement `YAMLPlanner` in `internal/planner/` — parse workflow YAML files, validate the DAG (cycle detection), and produce topologically sorted `ExecutionPlan` with parallel stages.
4. Implement template variable resolution for `{{ steps.X.output }}` references in step inputs.
5. Wire all three into `cmd/orchestrator/main.go` initialization sequence (steps 3, 6, 8 in the existing TODO list).
6. Achieve ≥ 80% test coverage for all three packages (`go test -race -cover`).

## Non-Goals

- HTTP/REST API server (separate RFC, depends on this one).
- gRPC server or agent communication (separate RFC).
- Scheduler or Executor logic (next step after this RFC).
- Persistent storage (SQLite is v0.2+; in-memory only for now).
- Condition expression evaluation (`{{ steps.review.output.approved == false }}`). Parse and store condition strings but defer evaluation to the Scheduler/Executor RFC.
- Approval gates. The `approval_required` and `approval_timeout` step fields defined in `workflow.schema.json` are not parsed or stored in v0.1. Approval gate logic is deferred to the Scheduler/Executor RFC (0003), which will add these fields to the `Step` struct at that time.
- MCP bridge, cost tracking, telemetry, security gates (independent packages that follow this RFC).

## Design / Implementation

### Phase 1: InMemoryStateStore

**Package:** `internal/state/`

The state store tracks workflow runs and individual step statuses. It is the single source of truth for "what happened" during execution.

#### Types

```go
type WorkflowRun struct {
    ID         string
    WorkflowID string
    Status     RunStatus           // Pending, Running, Completed, Failed, Cancelled
    Steps      map[string]StepState
    Error      string              // summarized failure reason (empty if not failed)
    StartedAt  time.Time
    FinishedAt time.Time
    Inputs     map[string]string   // user-provided variables (e.g. user_request)
}

type StepState struct {
    StepID    string
    Status    RunStatus
    Output    string              // captured agent response
    Error     string
    StartedAt time.Time
    FinishedAt time.Time
}

type RunStatus int
const (
    RunPending   RunStatus = 0
    RunRunning   RunStatus = 1
    RunCompleted RunStatus = 2
    RunFailed    RunStatus = 3
    RunCancelled RunStatus = 4
)
```

> **Note:** `RunStatus` intentionally omits `RETRYING` (present in `proto/task.proto` as `TaskStatus.RETRYING = 5`). Retry logic is a Scheduler concern and will be addressed in the Scheduler/Executor RFC (`0003`). The `RunStatus` enum may be extended at that point to align with the protobuf definition. When extending `RunStatus`, enum values MUST maintain numeric alignment with `proto/task.proto` `TaskStatus` values (currently 0–4). Implementations MUST use explicit integer assignments (e.g., `RunPending RunStatus = 0`) instead of `iota` to prevent accidental misalignment if intermediate values are inserted — `iota` silently renumbers all subsequent constants when a new value is added between existing ones, which would corrupt status values serialized across the gRPC boundary. Note also that the inline proto in `ai-agents-orchestration-spec.md` §4.3 omits `RETRYING` — the canonical source is `proto/task.proto`. RFC 0003 should reconcile all three (spec, proto, Go enum).

#### Interface (replace TODO stubs in existing file)

The current `internal/state/state.go` contains only a package declaration and TODO comments — no existing types or interfaces. This phase replaces those stubs entirely with the types and interface below.

```go
type Store interface {
    CreateRun(ctx context.Context, run *WorkflowRun) error
    GetRun(ctx context.Context, runID string) (*WorkflowRun, error)
    ListRuns(ctx context.Context) ([]*WorkflowRun, error)
    UpdateRunStatus(ctx context.Context, runID string, status RunStatus) error
    UpdateStepState(ctx context.Context, runID string, step StepState) error
    DeleteRun(ctx context.Context, runID string) error
}
```

> **Note on `context.Context`:** All interface methods accept `ctx context.Context` for forward compatibility with the SQLite backend planned in v0.2, where context cancellation and timeouts become meaningful for I/O operations. The in-memory v0.1 implementations may reasonably ignore the context parameter — adding `ctx.Done()` checks to purely in-memory operations would be unnecessary boilerplate. This applies equally to the `Registry` interface in Phase 2.

#### Implementation

- `InMemoryStore` backed by `sync.RWMutex` + `map[string]*WorkflowRun`.
- All methods are goroutine-safe.
- `CreateRun` generates a UUID if `run.ID` is empty. If a run with the given ID already exists, `CreateRun` returns an error (parallel to the Registry's `ErrAgentAlreadyRegistered` behavior). A corresponding `ErrRunAlreadyExists` sentinel error is defined for this case.
- `GetRun` returns a deep copy of the `WorkflowRun` to prevent callers from mutating internal state. (Same rationale as the Registry's `List` snapshot — without this, concurrent callers like the Scheduler and REST API could corrupt the store.) Deep copy is implemented via manual field copy; the `Steps` map must be reconstructed with new `StepState` values — a simple map assignment would share the underlying map reference, creating a subtle concurrency hazard.
- `UpdateStepState` merges into the existing run's `Steps` map. It returns an error if the `runID` does not exist. If the `StepID` is not already present in the run's `Steps` map, it is added — this allows the Scheduler to initialize step state on first execution without requiring pre-population.
- Like `GetRun`, `ListRuns` returns deep copies of all runs to prevent concurrent mutation — the Scheduler and REST API will both call `ListRuns`, and without copies, callers could corrupt the store's internal state. This is acceptable for the in-memory v0.1 backend, but the interface signature may need pagination parameters (e.g., `offset`/`limit` or cursor-based) when a persistent SQLite backend is added in v0.2+, to avoid unbounded result sets.
- `UpdateRunStatus` returns an error if the `runID` does not exist (consistent with `UpdateStepState` behavior below).
- `DeleteRun` removes a run from the store by ID. Returns `ErrRunNotFound` if the ID does not exist. `DeleteRun` accepts any run regardless of current status in v0.1 — restricting deletion of in-progress runs (e.g., requiring cancellation first) is deferred to RFC 0003 when the Scheduler owns the execution lifecycle. This method is included to ensure the `Store` interface is complete for v0.2 SQLite migration (which will need deletion for retention/cleanup policies) without requiring a breaking interface change. In v0.1, it also enables test cleanup.
- **In-memory state volatility**: All workflow run state is lost on process restart in v0.1. This is a known limitation. The `Store` interface is designed to accommodate a persistent SQLite backend in v0.2, which will provide durable state across restarts.
- **State transition validation**: `UpdateRunStatus` does not validate state transitions in v0.1 — any transition is allowed (e.g., `Completed → Running` would succeed). Defining and enforcing a valid state machine (e.g., `Pending → Running → {Completed, Failed, Cancelled}`) is deferred to RFC 0003 (Scheduler/Executor), which owns the execution lifecycle and will be the primary caller of `UpdateRunStatus`.

### Phase 2: InMemoryRegistry

**Package:** `internal/registry/`

Implements the existing `Registry` interface with an in-memory map.

> **Note:** The `AgentStatus` type and its constants (`StatusUnknown`, `StatusHealthy`, `StatusDegraded`, `StatusOffline`) are already defined in `internal/registry/registry.go`. The implementation must reuse these existing constants rather than redefining them.

#### Implementation

- `InMemoryRegistry` backed by `sync.RWMutex` + `map[string]*AgentInfo`.
- `Register` rejects duplicate IDs (returns `ErrAgentAlreadyRegistered`). Re-registration after agent restart requires calling `Unregister` first, then `Register` with the new address. An `Update` method may be added later if idempotent re-registration becomes a common pattern.
- `Get` returns `ErrAgentNotFound` (sentinel error) on miss.
- `FindByCapability` iterates all agents and filters.
- `List` and `Get` return copies of `AgentInfo` where slice fields (notably `Capabilities []string`) are reconstructed via `copy()` — not just a struct value copy, which would share the underlying slice backing array. This matches the deep-copy rationale applied to `GetRun`/`ListRuns` in Phase 1: without it, concurrent callers could mutate a returned agent's capabilities and corrupt the registry's internal state.
- **No health-check loop yet** — just `UpdateStatus()` for external callers. Health-check polling is deferred to the Executor/gRPC RFC when agents are reachable.

### Phase 3: YAMLPlanner

**Package:** `internal/planner/`

The planner is the most complex component in this RFC. It has three responsibilities:

#### 3a. Parse — YAML to Workflow struct

- Read a workflow YAML file from disk (path provided by caller).
- Unmarshal into a `WorkflowFile` wrapper type first, since workflow YAML files (e.g. `feature-builder.yaml`) have a top-level `schema_version:` field alongside a `workflow:` object:
  ```go
  type WorkflowFile struct {
      SchemaVersion string   `yaml:"schema_version"`
      Workflow      Workflow `yaml:"workflow"`
  }
  ```
  The `Parse` method unmarshals into `WorkflowFile`, validates `SchemaVersion`, then returns the inner `Workflow`. Without this wrapper, `yaml.Unmarshal` will fail on the actual fixture files.
- Validate required fields: `id`, `name`, at least one step, each step has `id`, `agent`, and `input` (all three are required per `workflow.schema.json`).
- Validate agent ID format: `^[a-z0-9][a-z0-9-]*[a-z0-9]$`. This regex requires a minimum of 2 characters, so single-character agent IDs are invalid. This aligns with `agent.schema.json`.
- Validate step ID format: `^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$`. This allows single-character IDs (e.g. `"a"`) and multi-character IDs with hyphens or underscores (e.g. `"code-review"`, `"step_1"`). Underscores are permitted (unlike agent IDs) for readability — existing fixture uses single-word IDs like `"plan"`, `"implement"`, `"review"`, `"revise"`. Format validation prevents step IDs containing `.`, `/`, `{`, `}`, or whitespace from causing parsing ambiguity in template patterns (`{{ steps.<id>.output }}`), log injection, or future REST API URL routing issues.
- Validate `SchemaVersion == "0.1"` and reject unknown versions. This is intentionally stricter than `workflow.schema.json`, which only enforces `"type": "string"` without an enum constraint. The schema validates structure; the planner validates compatibility. This asymmetry means a workflow YAML can pass JSON Schema validation (`make validate`) but still be rejected by the planner if its schema version is unsupported.
- The existing `Workflow` struct includes a `Trigger` field (parsed from YAML, e.g. `"manual"`). This field is preserved in the parsed struct but not acted upon in v0.1 — trigger evaluation is a Scheduler concern (RFC 0003). The `Trigger` field is not validated by the planner; JSON Schema validation (via `make validate`) is the canonical enforcement point for the `enum: ["manual", "schedule", "event"]` constraint defined in `workflow.schema.json`. The planner treats it as an opaque string.
- The existing `Workflow` struct in `internal/planner/planner.go` is sufficient for v0.1. Additional fields that the spec mentions (`Description`, `Variables` for declared workflow-level input variables, `Timeout` for workflow-level timeout) may be added by RFC 0002/0003 as needed. The struct is intentionally minimal to avoid speculative field additions.
- Validate that `depends_on` references point to existing step IDs.
- Validate that all step IDs within a workflow are unique. Duplicate step IDs would cause silent overwrites in `WorkflowRun.Steps` (a `map[string]StepState`), ambiguous `depends_on` resolution, and incorrect `outputs` lookup in `ResolveInputs`. Note that `workflow.schema.json` does not enforce uniqueness (steps are a JSON array, not keyed by ID), so this is a planner-side validation.

#### 3b. ValidateDAG — Cycle detection

- Build adjacency list from `depends_on` edges.
- Run DFS-based cycle detection.
- Return a descriptive error listing the cycle if one is found (e.g. `"cycle detected: review → revise → review"`).

#### 3c. Plan — Topological sort into parallel stages

- **Precondition**: `Plan` assumes the workflow has passed `ValidateDAG`. If called on an invalid (cyclic) DAG, Kahn's algorithm will silently drop nodes involved in cycles, producing an incomplete plan without error. Callers must call `ValidateDAG` before `Plan`. Both `ValidateDAG` (DFS) and Kahn's algorithm detect cycles, but `ValidateDAG` is the canonical cycle detector that returns a descriptive error; Kahn's implicit detection in `Plan` is defense-in-depth only.
- **Defensive node-count check**: `Plan()` SHOULD verify after Kahn's algorithm completes that `len(emitted) == len(workflow.Steps)` and return an error if not. This catches cycles that slip past a missing `ValidateDAG` call, turning Kahn's implicit detection from silent data loss into an explicit error.
- Kahn's algorithm (BFS topological sort) to produce layers.
- Each layer is a group of steps whose dependencies are all satisfied by prior layers.
- Output: `ExecutionPlan{ Stages: [][]Step }`.
- Example for `feature-builder.yaml`:
  - Stage 0: `[plan]`
  - Stage 1: `[implement]`
  - Stage 2: `[review]`
  - Stage 3: `[revise]`

#### 3d. Template variable resolution (basic)

- Recognize `{{ user_request }}` and `{{ steps.<id>.output }}` patterns.
- At parse/plan time: extract and store variable references, do **not** resolve values (values are only known at execution time).
- Provide a `ResolveInputs(step Step, outputs map[string]string, vars map[string]string) (string, error)` helper that the Scheduler/Executor will call at runtime to substitute actual values.
- **`vars` key convention**: The `vars` map keys are the literal variable names extracted from templates, without delimiters or whitespace. For example, `{{ user_request }}` resolves via `vars["user_request"]`. These keys correspond directly to `WorkflowRun.Inputs` entries, which are populated by the REST API caller (RFC 0002). The Scheduler/Executor is responsible for passing `WorkflowRun.Inputs` as the `vars` parameter.
- `ResolveInputs` returns an error if a template references a step ID not present in the `outputs` map or a variable not present in the `vars` map. Fail-fast prevents silent data loss from typos or misordered execution.
- Template resolution is **single-pass** — output values substituted into an input string are NOT re-scanned for template patterns. This prevents second-order template injection if a step's output happens to contain `{{ }}` patterns.
- **Scope**: `ResolveInputs` operates on `Step.Input` only, NOT on `Step.Condition`. Condition strings (e.g., `{{ steps.review.output.approved == false }}`) are opaque to this RFC — they resemble template patterns but contain expressions that the Scheduler/Executor RFC will evaluate. Passing a condition string to `ResolveInputs` would produce incorrect results because `.approved == false` is not a valid step ID or variable name. Note: the Scheduler/Executor RFC (0003) must handle both variable resolution *and* expression evaluation inside conditions — a condition like `{{ steps.review.output.approved == false }}` requires the step output value to be resolved before the boolean expression can be evaluated.
- Note that `Step.Input` can contain multiple template references interleaved with literal text in a single string (e.g., `"{{ steps.implement.output }}\nFeedback: {{ steps.review.output }}"`). `ResolveInputs` replaces all occurrences via regexp global substitution, not just the first match.
- Use `regexp` for pattern matching — no Jinja2 engine in Go. Support only the two patterns above for v0.1. Recommended combined regex: `\{\{\s*(steps\.([a-z0-9](?:[a-z0-9_-]*[a-z0-9])?)\.output|([a-z_][a-z0-9_]*))\s*\}\}`. Group 2 captures the step ID for output references; group 3 captures plain variable names. The step ID sub-pattern must be consistent with Phase 3a's step ID format validation regex. Specifying the pattern explicitly prevents implementation inconsistency when different regex choices produce subtly different behavior with nested braces or edge-case inputs.
- **Malformed patterns** (empty braces `{{ }}`, incomplete references `{{ steps. }}`, missing whitespace delimiters `{{no-spaces}}`) are left as-is in the output string — they are not substituted and do not cause an error. This is intentional: condition expressions like `{{ steps.review.output.approved == false }}` coexist in the same YAML file and must not be rejected by `ResolveInputs`.
- **Warning on suspicious patterns**: When `ResolveInputs` encounters a pattern in `Step.Input` that resembles a template reference (matches `{{ ... }}` delimiters) but does not match the known `{{ variable }}` or `{{ steps.<id>.output }}` patterns, it emits a `logger.Warn` with the unresolved pattern text. This aids debugging of typos in workflow YAML without causing errors. Since `ResolveInputs` only operates on `Step.Input` (never on `Step.Condition`), this warning does not conflict with condition expressions.
- **Output lookup key**: `ResolveInputs` resolves `{{ steps.<id>.output }}` by looking up `outputs[<id>]` where `<id>` is the step's `ID` field, not `OutputKey`. The `OutputKey` field is a downstream concern for how the Scheduler stores results externally; the `outputs` map parameter is keyed by step ID. In `feature-builder.yaml`, `id` and `output_key` happen to have the same values (e.g., both `"plan"`), which masks this distinction — implementations must use `Step.ID` as the canonical key.
- **Interface placement**: `ResolveInputs` is an exported standalone function on `YAMLPlanner`, not a method on the `Planner` interface. It is a utility the Scheduler/Executor will call at runtime and does not fit the parse/plan/validate lifecycle that the interface represents. This avoids premature interface expansion.

#### ResolveInputs Signature

```go
// ResolveInputs substitutes template variables in step.Input with actual values.
// It resolves {{ steps.<id>.output }} from the outputs map and {{ variable }}
// from the vars map. Returns the resolved input string or an error if any
// referenced step ID or variable is missing. Resolution is single-pass —
// substituted values are not re-scanned for template patterns.
func (p *YAMLPlanner) ResolveInputs(step Step, outputs map[string]string, vars map[string]string) (string, error)
```

#### Constructor

```go
func NewYAMLPlanner(logger *zap.Logger) *YAMLPlanner
```

The planner receives a logger (consistent with project conventions) and implements the existing `Planner` interface.

### Phase 4: Wire into main.go

#### Constructor Signatures

```go
func NewInMemoryStore(logger *zap.Logger) *InMemoryStore
func NewInMemoryRegistry(logger *zap.Logger) *InMemoryRegistry
```

Both constructors accept a logger for consistency with the project's structured logging convention (`NewYAMLPlanner` in Phase 3 follows the same pattern).

Update `cmd/orchestrator/main.go` to:

1. Create `state.NewInMemoryStore(logger)`.
2. Create `registry.NewInMemoryRegistry(logger)`.
3. Create `planner.NewYAMLPlanner(logger)`.
4. Log successful initialization of each component.
5. Keep the existing graceful-shutdown logic; no behavioral change to startup/shutdown flow yet.

This partially addresses `main.go` TODO step 8 ("Initialize workflow planner + scheduler") — only the planner is wired here. The scheduler portion is deferred to RFC 0003.

This phase is minimal wiring — the components exist but aren't serving traffic yet (that requires the REST API, which is the next RFC).

### Sentinel Error Catalog

All sentinel errors defined across the three packages, consolidated for implementer reference:

| Package | Error | Returned by | Trigger |
|---------|-------|-------------|---------|
| `state` | `ErrRunAlreadyExists` | `CreateRun` | Run with the given ID already exists |
| `state` | `ErrRunNotFound` | `GetRun`, `UpdateRunStatus`, `UpdateStepState`, `DeleteRun` | Run ID does not exist in the store |
| `registry` | `ErrAgentAlreadyRegistered` | `Register` | Agent with the given ID is already registered |
| `registry` | `ErrAgentNotFound` | `Get`, `Unregister`, `UpdateStatus` | Agent ID does not exist in the registry |
| `planner` | *(validation errors)* | `Parse`, `ValidateDAG` | Missing required fields, invalid IDs, unknown schema version, cycle detection, missing `depends_on` targets, duplicate step IDs |

## Security Considerations

- **No new attack surface.** These components are internal-only; no network listeners are added.
- **State store concurrency.** `sync.RWMutex` prevents data races; CI runs `go test -race`.
- **YAML parsing.** Use `gopkg.in/yaml.v3` (or `encoding/json` for schemas); limit file size to prevent DoS from malformed input. Reject YAML files > 1 MB. Enforce via `io.LimitReader(file, 1<<20+1)` in `Parse()` — read up to 1 MB + 1 byte, then reject if bytes read exceed 1 MB. This avoids silent truncation (plain `io.LimitReader` would truncate the stream and `yaml.Unmarshal` could succeed on partial, potentially security-relevant data). A post-read size check is insufficient because the full content would already be in memory.
- **YAML anchor/alias expansion.** Defend against YAML "billion laughs" attacks (exponential expansion via nested anchors/aliases). The `gopkg.in/yaml.v3` default node limit (10,000) mitigates extreme cases but still permits significant memory amplification from small inputs. The implementation MUST either: (1) disable alias expansion entirely in the YAML decoder, (2) set an explicit node limit below the library default, or (3) reject YAML documents containing anchors/aliases. Option 1 (disable aliases) is preferred — workflow YAML files have no legitimate use for anchors/aliases. This is a security requirement, not optional, because workflow YAML will become user-submitted via REST API in RFC 0002.
- **Template injection.** `ResolveInputs` does simple string substitution, not expression evaluation. Step outputs are treated as opaque strings, never executed. Condition evaluation is deferred.
- **Agent ID validation.** Enforced at parse time to prevent injection via agent IDs in downstream components (gRPC addresses, file paths, etc.).
- **Path traversal in `Parse()`.** The `yamlPath` parameter is internal-only in this RFC (no network exposure). As defense-in-depth, `Parse()` applies `filepath.Clean(yamlPath)` before opening the file — this normalizes double-slashes, dot-dot segments, and other path oddities that might arise from tests or future callers at negligible cost. When RFC 0002 exposes workflow submission via REST API, the submitted path must be validated against a configured workflows directory (canonicalized via `filepath.EvalSymlinks`, then prefix-checked) to prevent directory traversal attacks (e.g., `../../etc/passwd`). Symbolic link traversal must also be blocked.

## Phased Implementation Plan

### Phase 1: InMemoryStateStore (~200 LOC, ~1 day)

- Types, interface, implementation, unit tests.
- Deliverables: `internal/state/state.go` (types + interface + impl), `internal/state/state_test.go`.

### Phase 2: InMemoryRegistry (~150 LOC, ~0.5 day)

- Implementation of existing interface, sentinel errors, unit tests.
- Deliverables: `internal/registry/registry.go` (expanded), `internal/registry/registry_test.go`.

### Phase 3: YAMLPlanner (~400 LOC, ~2 days)

- Parse, ValidateDAG, Plan, ResolveInputs, unit tests.
- Test against `workflows/feature-builder.yaml` as a real fixture.
- Deliverables: `internal/planner/planner.go` (expanded), `internal/planner/planner_test.go`.

### Phase 4: Wire into main.go (~30 LOC, ~0.5 day)

- Initialize components in main, add `go.mod` dependency for YAML parser.
- Deliverable: updated `cmd/orchestrator/main.go`, updated `go.mod`.

**Total estimated scope:** ~780 LOC implementation + tests. 4 days.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Go orchestrator | `internal/state/state.go` | Add types, `Store` interface, `InMemoryStore` implementation |
| Go orchestrator | `internal/state/state_test.go` | New — unit tests for state store |
| Go orchestrator | `internal/registry/registry.go` | Add `InMemoryRegistry` implementation, sentinel errors |
| Go orchestrator | `internal/registry/registry_test.go` | New — unit tests for registry |
| Go orchestrator | `internal/planner/planner.go` | Add `YAMLPlanner` implementation (parse, DAG validate, plan, resolve) |
| Go orchestrator | `internal/planner/planner_test.go` | New — unit tests (valid workflow, cycles, missing deps, template vars) |
| Go orchestrator | `cmd/orchestrator/main.go` | Wire state store, registry, planner initialization |
| Go orchestrator | `go.mod` / `go.sum` | Add `gopkg.in/yaml.v3` and `github.com/google/uuid` dependencies |

## Test Strategy

- **Unit tests per package** using `testify/assert` and `testify/require`.
- **Table-driven tests** for planner parsing (valid, invalid YAML, missing fields, bad agent IDs, bad step IDs per format regex, duplicate step IDs, empty steps array).
- **Cycle detection tests**: no-cycle graph, simple cycle (A→B→A), complex cycle (A→B→C→A), self-referencing step.
- **Topological sort tests**: linear chain, diamond dependency, fully parallel (no deps), single step.
- **State store tests**: CRUD operations, concurrent read/write with goroutines, status transitions, `UpdateRunStatus` on nonexistent run ID (returns error), `DeleteRun` on existing and nonexistent IDs, `ListRuns` deep copy verification (modifying a returned run must not affect store state), `UpdateStepState` adding a new step ID not pre-populated in the run's `Steps` map (should succeed per Phase 1 design — verifies the Scheduler can initialize step state on first execution).
- **Registry tests**: register/unregister, duplicate ID rejection, capability search, status update, re-registration flow (register → unregister → register with same ID succeeds).
- **Registry concurrent tests**: goroutine-based concurrent register/get/list operations (mirrors state store concurrency tests; both use `sync.RWMutex`).
- **Fixture-based test**: parse `workflows/feature-builder.yaml` and assert the expected 4-stage plan.
- **Pipeline integration test**: Call `Parse` → `ValidateDAG` → `Plan` as a single pipeline on `feature-builder.yaml` and assert: no error from any stage, 4 stages with correct step grouping. This end-to-end test catches integration issues between the three stages that unit tests on individual methods would miss.
- **Template resolution edge cases**: malformed patterns (`{{ }}`, `{{ steps. }}`, `{{no-spaces}}`), input containing multiple template references with interleaved literal text, verification that single-pass resolution does NOT re-scan substituted output (prevents second-order template injection), confirmation that suspicious patterns in `Step.Input` emit a warning log (per Phase 3d), empty `outputs` map, empty `vars` map, `nil` maps, input with no template references (passthrough), input that is entirely a single template reference, `outputs` map containing extra keys not referenced by any template (should succeed without error — unused outputs are ignored), negative test confirming `Step.Condition` is NOT passed to `ResolveInputs` (documents the boundary between template resolution and condition evaluation per Phase 3d), and multi-line YAML input strings using block scalar syntax (`|`, `>`) to verify the parser and `ResolveInputs` handle embedded newlines correctly — `feature-builder.yaml` uses inline `\n` but real-world workflows may use YAML block scalars.
- **Schema version validation**: reject unknown schema versions (e.g., `schema_version: "0.2"`, `schema_version: ""`), confirm `schema_version: "0.1"` is accepted. This covers the behavior resolved in Open Question 2.
- **YAML file size limit**: reject YAML files exceeding 1 MB (per Security Considerations). This is a security-critical behavior that must have explicit test coverage.
- **YAML anchor/alias rejection**: verify that YAML documents containing anchors/aliases are rejected or limited per Security Considerations. Test with a minimal billion-laughs-style document to confirm the defense is effective.
- **Path traversal defense**: verify that `Parse()` applies `filepath.Clean` on the input path — test with paths containing `..` segments, double slashes, and trailing dots to confirm normalization.
- **Phase 4 smoke test**: build the binary (`go build ./cmd/orchestrator`) and verify it starts and shuts down cleanly with SIGINT. This validates that the wiring compiles and the component initialization doesn't panic.
- **Race detector**: all tests run with `-race` flag (already enforced in CI/Makefile).

## Open Questions

1. ~~**YAML library choice**: `gopkg.in/yaml.v3` is the standard Go YAML library. Any reason to prefer `sigs.k8s.io/yaml` (JSON-compatible subset)?~~
   **Resolved**: Use `gopkg.in/yaml.v3`. The project has no Kubernetes YAML or JSON round-trip requirement, so the standard library is the appropriate choice. It is also the most widely used and best-maintained Go YAML package. (2026-04-09)
2. ~~**Workflow schema version**: `feature-builder.yaml` declares `schema_version: "0.1"`. Should the planner enforce this and reject unknown versions?~~
   **Resolved**: Yes — validate `SchemaVersion == "0.1"` and reject unknown versions. Phase 3a already proposes a `WorkflowFile` wrapper that parses `schema_version`, and `workflow.schema.json` declares it as required. Parsing the field without enforcing it would silently accept incompatible schemas. (2026-04-09)
3. ~~**Template syntax**: Current convention is `{{ steps.X.output }}`. Should we also support `{{ steps.X.output_key_name }}` or keep it to the raw output string only?~~
   **Resolved**: v0.1 supports only the raw output string (`{{ steps.X.output }}`). Dotted field access (e.g. `{{ steps.X.output.field }}`) requires structured output parsing (JSON/YAML) and is deferred to a dedicated templating RFC or RFC 0003. The `feature-builder.yaml` fixture uses `{{ steps.review.output.approved == false }}` in a `condition` field, which is already out of scope for `ResolveInputs` per the Non-Goals and Phase 3d design. (2026-04-09)
4. ~~**State store ID generation**: Use `google/uuid` package or a simpler approach (e.g. `crypto/rand` hex string)?~~
   **Resolved**: Use `google/uuid` with **UUIDv4** (random). UUIDv4 is the most common and well-understood variant, sufficient for v0.1 where run listing is unordered. If time-ordered listing becomes important (e.g., for the REST API's `ListRuns` paginated response), a migration to UUIDv7 can be considered in the persistence RFC (v0.2+). (2026-04-09)

## Decision / Next Steps

Once this RFC is accepted:

1. Create feature branch `feature/v01-core-pipeline`.
2. Implement in phase order (State → Registry → Planner → Wiring).
3. PR < 500 lines per phase if needed; squash merge to `main`.
4. **Next RFC**: `0002-rest-api-server.md` — HTTP server with workflow submission endpoint, run status queries, and SSE streaming. Depends on all three components from this RFC.
5. **After that**: `0003-scheduler-executor.md` — parallel stage execution and gRPC task dispatch to agents.

## Related Documentation

- [ai-agents-orchestration-spec.md](../ai-agents-orchestration-spec.md) — Core MVP specification
- [orchestr8-extension-spec.md](../orchestr8-extension-spec.md) — Extension spec (v0.2+ features)
- [orchestr8-spec-audit.md](../orchestr8-spec-audit.md) — Spec gap audit
- [BRANCHING.md](../BRANCHING.md) — Branch naming and PR size guidelines
- Existing stubs: `internal/planner/planner.go`, `internal/state/state.go`, `internal/registry/registry.go`
- Workflow fixture: `workflows/feature-builder.yaml`
- Partially addresses spec audit findings: #24 (schema versioning — now enforced at parse time via `SchemaVersion` validation) and #39 (execution state — in-memory store for v0.1, `Store` interface designed for SQLite migration in v0.2)
