# RFC 0001 — PR Implementation Plan

**RFC**: [0001-core-orchestration-pipeline.md](0001-core-orchestration-pipeline.md)
**Created**: 2026-04-09
**Branch prefix**: `feature/v01-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)

---

## Overview

RFC 0001 defines ~780 LOC across 4 phases. The project's PR size limit is <500 lines of meaningful change. The planner phase alone is estimated at ~350–400 LOC implementation + ~300–500 LOC tests, which will exceed 500 lines. This plan splits the work into **5 PRs**: phases 1, 2, and 4 each get one PR, while phase 3 (planner) is split into two PRs at a natural boundary (Parse+DAG+Plan vs. ResolveInputs).

Each PR is independently mergeable and leaves the codebase in a compilable, test-passing state.

---

## PR Sequence

### PR 1: `feature/v01-state-inmemory` — InMemoryStateStore

**Depends on**: nothing (first in chain)
**Branch**: `feature/v01-state-inmemory`
**Estimated size**: ~350–450 lines (types + interface + implementation + tests)

> **Note**: If this PR exceeds 500 lines during implementation, the concurrent goroutine tests (`_concurrent_test.go`) can be split into a follow-up PR within the same branch, keeping the core CRUD tests in the initial PR.

#### Scope

| File | Change |
|------|--------|
| `internal/state/state.go` | Replace TODO stubs with: `RunStatus` enum (explicit integers, no `iota`), `WorkflowRun`, `StepState` types, `Store` interface (6 methods), `InMemoryStore` implementation (`sync.RWMutex` + map), sentinel errors (`ErrRunAlreadyExists`, `ErrRunNotFound`), `NewInMemoryStore` constructor |
| `internal/state/state_test.go` | New file — unit tests |
| `go.mod` / `go.sum` | Add `github.com/google/uuid` (direct, new — adds entries to both `go.mod` and `go.sum`); promote `github.com/stretchr/testify` from indirect to direct (`go.mod` only — already resolved in `go.sum` at v1.9.0) |

#### Key implementation details

- `RunStatus` values 0–4 with explicit integer assignments (no `iota`). Do NOT add `RunRetrying = 5`.
- `CreateRun` generates UUIDv4 via `google/uuid` if ID is empty; returns `ErrRunAlreadyExists` on duplicate.
- `GetRun` / `ListRuns` return deep copies (manual copy of `Steps` and `Inputs` maps).
- `UpdateStepState` adds new step IDs (no pre-population required); errors on unknown `runID`.
- `DeleteRun` accepts any run status; returns `ErrRunNotFound` on miss.
- No state transition validation (deferred to RFC 0003).

#### Tests

- CRUD operations (create, get, update status, update step, delete, list).
- `ErrRunAlreadyExists` on duplicate create.
- `ErrRunNotFound` on get/update/delete of nonexistent run.
- Deep copy verification: mutating returned `*WorkflowRun` must not affect store.
- `ListRuns` deep copy verification.
- `UpdateStepState` adding new step ID not in initial `Steps` map.
- `CreateRun` with nil vs. empty vs. pre-populated `Steps`.
- Concurrent goroutine tests: read/write, concurrent `UpdateStepState` on same run with different step IDs.
- Race detector (`-race` flag).

#### PR checklist

- [ ] `go test ./internal/state/... -v -race -cover` passes
- [ ] Coverage ≥ 80%
- [ ] `go vet ./internal/state/...` clean
- [ ] No `iota` in `RunStatus`

---

### PR 2: `feature/v01-registry-inmemory` — InMemoryRegistry

**Depends on**: PR 1 merged (for testify promotion in go.mod, though technically independent)
**Branch**: `feature/v01-registry-inmemory`
**Estimated size**: ~250–350 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `internal/registry/registry.go` | Add below existing types/interface: sentinel errors (`ErrAgentAlreadyRegistered`, `ErrAgentNotFound`), `InMemoryRegistry` struct, `NewInMemoryRegistry` constructor, all 6 interface methods |
| `internal/registry/registry_test.go` | New file — unit tests |

#### Key implementation details

- Reuse existing `AgentInfo`, `AgentStatus`, `Registry` interface — do not redefine.
- **Note**: Existing `AgentStatus` uses `iota`, which is inconsistent with the explicit-integer mandate for `RunStatus` (PR 1). This is acceptable because `AgentStatus` has no proto wire-format alignment requirement — the proto `HealthStatus` enum has different semantics (`UNKNOWN=0, SERVING=1, NOT_SERVING=2`).
- `Register` takes `AgentInfo` by value (existing interface signature); store internal pointer copy with `Capabilities` slice deep-copied.
- `Get` / `List` / `FindByCapability` return copies with `Capabilities` slice reconstructed via `copy()`.
- `Get` returns `ErrAgentNotFound` on miss.
- No health-check loop (deferred to gRPC RFC).

#### Tests

- Register / Unregister / Get / List / UpdateStatus / FindByCapability.
- `ErrAgentAlreadyRegistered` on duplicate register.
- `ErrAgentNotFound` on Get of nonexistent agent.
- `ErrAgentNotFound` on Unregister of nonexistent agent.
- `ErrAgentNotFound` on UpdateStatus of nonexistent agent.
- Re-registration flow: register → unregister → register with same ID succeeds.
- FindByCapability with multiple matches, no matches.
- Deep copy verification: mutating returned `AgentInfo.Capabilities` must not affect registry.
- Concurrent goroutine tests: register/get/list.
- Race detector (`-race` flag).

#### PR checklist

- [ ] `go test ./internal/registry/... -v -race -cover` passes
- [ ] Coverage ≥ 80%
- [ ] Existing `AgentStatus` constants reused (not redefined)

---

### PR 3a: `feature/v01-planner-yaml` — YAMLPlanner (Parse + DAG + Plan)

**Depends on**: PR 1 merged (uses `RunStatus` for alignment context, though no import dependency)
**Branch**: `feature/v01-planner-yaml`
**Estimated size**: ~350–450 lines (implementation + tests)

> **Rationale for mandatory split**: The full planner phase (implementation ~350–400 LOC + tests ~300–500 LOC) realistically totals 650–900 lines. `ResolveInputs` is a standalone package-level function with no dependency on `YAMLPlanner` state, making it a natural split point into PR 3b.

#### Scope

| File | Change |
|------|--------|
| `internal/planner/planner.go` | Add below existing types/interface: `WorkflowFile` wrapper struct, `YAMLPlanner` struct, `NewYAMLPlanner` constructor, `Parse` (YAML read + validation), `ValidateDAG` (DFS cycle detection), `Plan` (Kahn's topological sort), shared `stepIDPattern` const, validation regexes |
| `internal/planner/planner_test.go` | New file — unit tests for Parse, ValidateDAG, Plan |
| `internal/planner/testdata/` | Test fixtures: valid workflow YAML, invalid variants, oversized file for security test |
| `go.mod` / `go.sum` | Promote `gopkg.in/yaml.v3` from indirect to direct (already resolved in `go.sum` at v3.0.1 via testify — no new checksum entries) |

#### Key implementation details

- **Parse**: `WorkflowFile` wrapper unmarshals `schema_version` + `workflow`. Validate: `SchemaVersion == "0.1"`, required fields (`id`, `name`, steps non-empty, each step has `id`/`agent`/`input`), workflow ID format, agent ID format, step ID format, `output_key` format (if present), `output_key` uniqueness across steps (reject duplicates to prevent silent overwrites), `depends_on` references point to existing step IDs, step ID uniqueness. Silent handling of unknown YAML keys (no `KnownFields`). `filepath.Clean` on path. `io.LimitReader` with 1 MB + 1 byte overflow detection.
- **YAML anchor/alias rejection**: Decode to `yaml.Node` tree first, walk nodes rejecting any with `Kind == yaml.AliasNode`, then decode the validated node to the target struct. This 2-pass approach is necessary because `gopkg.in/yaml.v3` has no built-in "disable aliases" option.
- **ValidateDAG**: DFS-based cycle detection with descriptive error (cycle path).
- **Plan**: Kahn's algorithm producing `ExecutionPlan` with parallel stages. MUST verify `len(emitted) == len(workflow.Steps)` after completion.
- **Fixture path strategy**: Tests use `testdata/` directory within `internal/planner/` for all YAML fixtures. The `feature-builder.yaml` integration test copies or embeds the repo-root fixture rather than relying on relative path resolution (which breaks because `go test` sets CWD to the package directory).

#### Tests

- **Parse**: valid fixture, missing required fields, bad workflow IDs, bad agent IDs, bad step IDs, duplicate step IDs, empty steps, unknown schema version, empty schema version, `output_key` format validation, `output_key` uniqueness violation, invalid `depends_on` reference (references nonexistent step ID).
- **ValidateDAG**: no-cycle, simple cycle (A→B→A), complex cycle (A→B→C→A), self-reference.
- **Plan**: linear chain, diamond dependency, fully parallel, single step. Node-count defense check.
- **Pipeline integration**: Parse → ValidateDAG → Plan on `feature-builder.yaml` fixture, assert 4 stages.
- **Security**: YAML file >1 MB rejected, anchor/alias rejected, `filepath.Clean` normalization.
- Race detector (`-race` flag).

#### PR checklist

- [ ] `go test ./internal/planner/... -v -race -cover` passes
- [ ] Coverage ≥ 80%
- [ ] `stepIDPattern` defined as single const, referenced by both validation and template regex
- [ ] Fixtures in `internal/planner/testdata/`
- [ ] Total PR ≤ 500 lines

---

### PR 3b: `feature/v01-planner-resolve` — ResolveInputs

**Depends on**: PR 3a merged
**Branch**: `feature/v01-planner-resolve`
**Estimated size**: ~200–350 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `internal/planner/planner.go` | Add `ResolveInputs` package-level function, combined regex pattern |
| `internal/planner/resolve_test.go` | New file — ResolveInputs unit tests |

#### Key implementation details

- **ResolveInputs**: Package-level function (not method). Single `const stepIDPattern` shared between Parse validation regex and template capture group. Single-pass substitution (no re-scan). Operates on `Step.Input` only (not `Step.Condition`). `logger.Warn` for suspicious unresolved patterns. Logger MUST be non-nil (use `zap.NewNop()` in tests).
- **Regex**: Combined pattern `\{\{\s*(steps\.(<stepIDPattern>)\.output|([a-z_][a-z0-9_]*))\s*\}\}`.

#### Tests

- **ResolveInputs**: missing step ref, missing var ref, malformed patterns passthrough, multiple templates in one string, single-pass (no re-scan), suspicious pattern warning log, empty/nil maps, passthrough (no templates), single template input, extra unused outputs, multi-line block scalar input.
- Race detector (`-race` flag).

#### PR checklist

- [ ] `go test ./internal/planner/... -v -race -cover` passes
- [ ] Coverage ≥ 80%
- [ ] `stepIDPattern` reused from PR 3a (no duplication)
- [ ] Total PR ≤ 500 lines

---

### PR 5: `feature/v01-orchestrator-wiring` — Wire into main.go

**Depends on**: PRs 1, 2, 3a, 3b merged
**Branch**: `feature/v01-orchestrator-wiring`
**Estimated size**: ~30–50 lines

#### Scope

| File | Change |
|------|--------|
| `cmd/orchestrator/main.go` | Add imports for `state`, `registry`, `planner` packages. Initialize `NewInMemoryStore`, `NewInMemoryRegistry`, `NewYAMLPlanner` with logger. Log component initialization. Assign to struct or use in log statements to satisfy Go's unused-variable check. |

#### Key implementation details

- Initialize in order matching existing TODO comments: state store (step 3), registry (step 6), planner (step 8).
- Use `logger` (structured, not sugar) for component init logging with `zap.String` fields.
- Assign components to a struct or log them to prevent `declared and not used` compiler error.
- No behavioral change to startup/shutdown flow.
- Partially addresses `main.go` TODO step 8 (planner only; scheduler deferred to RFC 0003).

#### Tests

- Smoke test: `go build ./cmd/orchestrator` succeeds.
- Binary starts and shuts down cleanly with SIGINT (manual or scripted).

#### PR checklist

- [ ] `go build ./cmd/orchestrator` succeeds
- [ ] `go vet ./cmd/orchestrator/...` clean
- [ ] Component initialization logged on startup
- [ ] No unused variable compiler errors

---

## Dependency Graph

```
PR 1 (state)  ──────────────────────────────────────┐
PR 2 (registry) ──────────────────────────────────┼──→ PR 5 (wiring)
PR 3a (planner parse+dag+plan) ──→ PR 3b (resolve) ──┘
```

PRs 1, 2, and 3a have no import dependencies between their packages. However, PR 1 should land first because it promotes `testify` from indirect to direct in `go.mod` and adds `google/uuid`, which avoids `go.mod` merge conflicts in subsequent PRs. PRs 2 and 3a can proceed in parallel after PR 1. PR 3b depends on PR 3a. PR 5 (wiring) depends on all others being merged.

## CI Validation

Each PR must pass the full CI pipeline (`.github/workflows/ci.yml`):

- `go build ./cmd/orchestrator` — binary compiles
- `go test ./internal/... -v -race -cover` — unit tests with race detector

> **Note**: The CI pipeline does not currently run `make lint`, `go vet`, or `staticcheck`. These are local-only checks until CI is extended. Run `make lint` locally before opening each PR.

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| PR 1 (state) exceeds 500 lines with concurrent tests | Concurrent goroutine tests can be split into `_concurrent_test.go` in a follow-up PR |
| PR 3a (planner) exceeds 500 lines | Mandatory split: PR 3a (Parse+DAG+Plan) and PR 3b (ResolveInputs) |
| `go.mod` merge conflicts between parallel PRs | Land PR 1 first; PRs 2 and 3a rebase on updated `main` |
| Planner tests depend on fixture YAML files | Fixtures stored in `internal/planner/testdata/`; `go test` CWD is package directory, so repo-root relative paths would not resolve |
| `gopkg.in/yaml.v3` anchor/alias behavior | Decode to `yaml.Node` tree, walk and reject `yaml.AliasNode` types, then decode node to struct (2-pass); pin exact version in `go.mod` |
| Fixture path resolution fails in CI | Using `testdata/` within the package (standard Go convention); no repo-root path dependency |
