# RFC 0001 — PR Implementation Plan

**RFC**: [0001-core-orchestration-pipeline.md](0001-core-orchestration-pipeline.md)
**Created**: 2026-04-09
**Branch prefix**: `feature/v01-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)

---

## Overview

RFC 0001 defines ~780 LOC across 4 phases. The project's PR size limit is <500 lines of meaningful change. The planner phase alone is estimated at ~400 LOC implementation + tests, which will exceed 500 lines with comprehensive test coverage. This plan splits the work into **4 PRs**, one per phase, with the planner split into two PRs if it exceeds the limit during implementation.

Each PR is independently mergeable and leaves the codebase in a compilable, test-passing state.

---

## PR Sequence

### PR 1: `feature/v01-state-inmemory` — InMemoryStateStore

**Depends on**: nothing (first in chain)
**Branch**: `feature/v01-state-inmemory`
**Estimated size**: ~350–450 lines (types + interface + implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `internal/state/state.go` | Replace TODO stubs with: `RunStatus` enum (explicit integers, no `iota`), `WorkflowRun`, `StepState` types, `Store` interface (6 methods), `InMemoryStore` implementation (`sync.RWMutex` + map), sentinel errors (`ErrRunAlreadyExists`, `ErrRunNotFound`), `NewInMemoryStore` constructor |
| `internal/state/state_test.go` | New file — unit tests |
| `go.mod` / `go.sum` | Add `github.com/google/uuid` (direct); promote `github.com/stretchr/testify` from indirect to direct |

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
- `Register` takes `AgentInfo` by value (existing interface signature); store internal pointer copy with `Capabilities` slice deep-copied.
- `Get` / `List` / `FindByCapability` return copies with `Capabilities` slice reconstructed via `copy()`.
- `Get` returns `ErrAgentNotFound` on miss.
- No health-check loop (deferred to gRPC RFC).

#### Tests

- Register / Unregister / Get / List / UpdateStatus / FindByCapability.
- `ErrAgentAlreadyRegistered` on duplicate register.
- `ErrAgentNotFound` on get/unregister/update of nonexistent agent.
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

### PR 3: `feature/v01-planner-yaml` — YAMLPlanner

**Depends on**: PR 1 merged (uses `RunStatus` for alignment context, though no import dependency)
**Branch**: `feature/v01-planner-yaml`
**Estimated size**: ~400–500+ lines (implementation + tests)

> **Note**: If this PR exceeds 500 lines during implementation, split into two PRs:
> - **PR 3a**: Parse + ValidateDAG + Plan (core planner, ~300 LOC + tests)
> - **PR 3b**: ResolveInputs + template resolution tests (~200 LOC + tests)
>
> The split point is natural because `ResolveInputs` is a standalone package-level function with no dependency on `YAMLPlanner` state.

#### Scope

| File | Change |
|------|--------|
| `internal/planner/planner.go` | Add below existing types/interface: `WorkflowFile` wrapper struct, `YAMLPlanner` struct, `NewYAMLPlanner` constructor, `Parse` (YAML read + validation), `ValidateDAG` (DFS cycle detection), `Plan` (Kahn's topological sort), `ResolveInputs` (package-level function), shared `stepIDPattern` const, validation regexes |
| `internal/planner/planner_test.go` | New file — unit tests |
| `go.mod` / `go.sum` | Add `gopkg.in/yaml.v3` (direct) |

#### Key implementation details

- **Parse**: `WorkflowFile` wrapper unmarshals `schema_version` + `workflow`. Validate: `SchemaVersion == "0.1"`, required fields (`id`, `name`, steps non-empty, each step has `id`/`agent`/`input`), workflow ID format, agent ID format, step ID format, `output_key` format (if present), `depends_on` references, step ID uniqueness. Silent handling of unknown YAML keys (no `KnownFields`). `filepath.Clean` on path. `io.LimitReader` with 1 MB + 1 byte overflow detection. YAML anchor/alias rejection.
- **ValidateDAG**: DFS-based cycle detection with descriptive error (cycle path).
- **Plan**: Kahn's algorithm producing `ExecutionPlan` with parallel stages. MUST verify `len(emitted) == len(workflow.Steps)` after completion.
- **ResolveInputs**: Package-level function (not method). Single `const stepIDPattern` shared between Parse validation regex and template capture group. Single-pass substitution (no re-scan). Operates on `Step.Input` only (not `Step.Condition`). `logger.Warn` for suspicious unresolved patterns. Logger MUST be non-nil (use `zap.NewNop()` in tests).
- **Regex**: Combined pattern `\{\{\s*(steps\.(<stepIDPattern>)\.output|([a-z_][a-z0-9_]*))\s*\}\}`.

#### Tests

- **Parse**: valid fixture (`feature-builder.yaml`), missing required fields, bad agent IDs, bad step IDs, duplicate step IDs, empty steps, unknown schema version, empty schema version, `output_key` format validation.
- **ValidateDAG**: no-cycle, simple cycle (A→B→A), complex cycle (A→B→C→A), self-reference.
- **Plan**: linear chain, diamond dependency, fully parallel, single step. Node-count defense check.
- **Pipeline integration**: Parse → ValidateDAG → Plan on `feature-builder.yaml`, assert 4 stages.
- **ResolveInputs**: missing step ref, missing var ref, malformed patterns passthrough, multiple templates in one string, single-pass (no re-scan), suspicious pattern warning log, empty/nil maps, passthrough (no templates), single template input, extra unused outputs, multi-line block scalar input.
- **Security**: YAML file >1 MB rejected, anchor/alias rejected, `filepath.Clean` normalization.
- Race detector (`-race` flag).

#### PR checklist

- [ ] `go test ./internal/planner/... -v -race -cover` passes
- [ ] Coverage ≥ 80%
- [ ] `stepIDPattern` defined as single const, referenced by both validation and template regex
- [ ] Fixture test uses `workflows/feature-builder.yaml`
- [ ] Total PR ≤ 500 lines (split if exceeded)

---

### PR 4: `feature/v01-orchestrator-wiring` — Wire into main.go

**Depends on**: PRs 1, 2, 3 merged
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
PR 1 (state)  ──┐
                 ├──→  PR 4 (wiring)
PR 2 (registry) ─┤
                 │
PR 3 (planner) ──┘
```

PRs 1, 2, and 3 are logically independent (no import dependencies between the three packages). However, PR 1 should land first because it promotes `testify` from indirect to direct in `go.mod` and adds `google/uuid`, which avoids `go.mod` merge conflicts in subsequent PRs. PRs 2 and 3 can proceed in parallel after PR 1.

PR 4 depends on all three being merged.

## CI Validation

Each PR must pass the full CI pipeline (`.github/workflows/ci.yml`):

- `make build-orchestrator` — binary compiles
- `make test-go` — `go test ./internal/... -v -race -cover`
- `make lint` — Go vet + staticcheck
- `make validate` — JSON Schema validation of YAML configs (unchanged, but must not regress)

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| PR 3 (planner) exceeds 500 lines | Pre-planned split point: Parse+DAG+Plan vs. ResolveInputs |
| `go.mod` merge conflicts between parallel PRs | Land PR 1 first; PRs 2 and 3 rebase on updated `main` |
| Planner tests depend on fixture file (`feature-builder.yaml`) | Fixture already exists and is committed; planner tests use relative path from test working directory |
| `gopkg.in/yaml.v3` anchor/alias behavior differs across versions | Pin exact version in `go.mod`; test alias rejection explicitly |
