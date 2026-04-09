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

- [x] `go test ./internal/state/... -v -cover` passes (28/28, 100% coverage)
- [x] Coverage ≥ 80% (achieved: 100%)
- [x] `go vet ./internal/state/...` clean
- [x] No `iota` in `RunStatus`

#### Post-merge review findings (PR #6)

PR #6 was submitted as 781 lines (228 `state.go` + 545 `state_test.go` + `go.mod`/`go.sum`), exceeding the 500-line limit. The size waiver is acceptable given single-package, single-author scope and 100% test coverage. Full review: [`docs/pr-reviews/pr-006-state-inmemory-review.md`](../../docs/pr-reviews/pr-006-state-inmemory-review.md).

Actionable follow-ups for a **PR 1b follow-up** (`feature/v01-state-followup`) or folded into PR 2:

| Finding | Severity | Action | Disposition |
|---------|----------|--------|-------------|
| F-02: `CreateRun` mutates caller's `run.ID` | Low | Add documenting comment on mutation side effect | Address in PR 1b or PR 2 |
| F-03: Nil logger causes panic | Low | Add `if logger == nil { logger = zap.NewNop() }` guard in `NewInMemoryStore` | Address in PR 1b or PR 2 |
| F-04: No `String()` on `RunStatus` | Low | Add `String()` method for log readability | Address in PR 1b or PR 5 (wiring) |
| F-05: No timestamp management in `UpdateRunStatus` | Low | Design gap — Scheduler (RFC 0003) has no way to set `WorkflowRun.StartedAt`/`FinishedAt` via current interface | Document for RFC 0003; no change needed now |
| F-06: Non-printable rune IDs in concurrent tests | Low | Use `fmt.Sprintf` for test IDs instead of `string(rune('a'+i))` at high offsets | Address in PR 1b |

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
- **Nil-logger guard**: Add `if logger == nil { logger = zap.NewNop() }` in `NewInMemoryRegistry` (lesson from PR #6 review F-03 — apply same pattern to both constructors).

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

- [x] `go test ./internal/registry/... -v -cover` passes (24/24, 100% coverage)
- [x] Coverage ≥ 80% (achieved: 100%)
- [x] Existing `AgentStatus` constants reused (not redefined)
- [x] `go vet ./internal/registry/...` clean
- [x] `go build ./cmd/orchestrator` succeeds
- [x] Nil-logger guard in both `NewInMemoryRegistry` and `NewInMemoryStore`

> **Note**: `-race` requires CGO (unavailable on Windows dev environment). CI (Linux) validates with `-race`.

#### Post-merge review findings (PR #7)

PR #7 was submitted as 637 lines (151 `registry.go` + 483 `registry_test.go` + 3 `state.go`), exceeding the 500-line limit. Same waiver rationale as PR #6 — 76% test lines. Full review: [`docs/pr-reviews/pr-007-registry-inmemory-review.md`](../../docs/pr-reviews/pr-007-registry-inmemory-review.md).

PR 1 follow-up F-03 (nil-logger guard in `NewInMemoryStore`) was addressed in this PR.

Actionable follow-ups for a **PR 2b follow-up** or folded into subsequent PRs:

| Finding | Severity | Action | Disposition |
|---------|----------|--------|-------------|
| F-01: No agent ID validation in `Register` | Medium | Add comment that validation is caller's responsibility; add format validation when REST API lands | Document in PR 5 (wiring); validate in RFC 0002 |
| F-02: `cap` shadows built-in `cap()` | Low | Rename loop var to `c` or `capName` in `FindByCapability` | Address in PR 2b or any follow-up |
| F-03: nil vs empty slice inconsistency (`FindByCapability` vs `List`) | Low | Initialize `result` slice in `FindByCapability` for consistent JSON marshaling | Address before REST API (RFC 0002) |
| F-05: No `String()` on `AgentStatus` | Low | Add `String()` method (combine with `RunStatus.String()` from PR #6 F-04) | Address in PR 5 (wiring) |

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

- [x] `go test ./internal/planner/... -v -cover` passes (35/35, 97.7% coverage)
- [x] Coverage ≥ 80% (achieved: 97.7%)
- [x] `stepIDPattern` defined as single const, referenced by both validation and template regex
- [x] Fixtures in `internal/planner/testdata/`
- [x] `go vet ./internal/planner/...` clean
- [x] `go build ./cmd/orchestrator` succeeds

> **Note**: `-race` requires CGO (unavailable on Windows dev environment). CI (Linux) validates with `-race`.

#### Post-merge review findings (PR #8)

PR #8 was submitted as 1,071 lines (305 `planner.go` + 624 `planner_test.go` + 143 testdata YAML), exceeding the 500-line limit. Same waiver rationale as PRs #6 and #7 — single-package, high test coverage (97.7%), testdata YAML fixtures inflate line count. Full review: [`docs/pr-reviews/pr-008-planner-yaml-review.md`](../../docs/pr-reviews/pr-008-planner-yaml-review.md).

Actionable follow-ups for **PR 3b** or subsequent PRs:

| Finding | Severity | Action | Disposition |
|---------|----------|--------|-------------|
| F-01: stepID regex allows underscores/single-char but workflowID/agentID do not | Medium | Add clarifying comment above regex block explaining the intentional divergence | Address in PR 3b |
| F-02: 3 testdata YAML fixtures unused by any test (`cycle_complex.yaml`, `cycle_simple.yaml`, `self_reference.yaml`) | Medium | Add fixture-based Parse→ValidateDAG pipeline tests or remove unused fixtures | Address in PR 3b |
| F-03: `rejectAliases` only detects alias usage, not standalone anchor definitions | Low | No action — anchors without aliases are harmless; function name correctly describes behavior | Won't fix |
| F-04: Parse `io.ReadAll` error path uncovered (87% function coverage) | Low | Accept — would require mock reader injection for 2 uncovered lines | Won't fix |
| F-05: `validate` does not check for self-dependency in `depends_on` | Low | Consider adding `step.ID == dep` check for earlier/clearer error message | Address in PR 3b or defer |
| F-06: `ValidateDAG` cycle path includes duplicate node for self-reference | Info | Correct behavior (`loop → loop`); standard cycle display convention | Won't fix |
| F-07: `workflowIDRegex` and `agentIDRegex` are identical compiled patterns | Info | Semantic separation is intentional — clearer error messages and code intent | Won't fix |
| F-08: No test for malformed/invalid YAML syntax | Info | Consider adding a single invalid-syntax YAML test for `unmarshal YAML` error path | Address in PR 3b |

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

- [x] `go test ./internal/planner/... -v -cover` passes (62/62, 98.8% coverage)
- [x] Coverage ≥ 80% (achieved: 98.8%)
- [x] `stepIDPattern` reused from PR 3a (no duplication)
- [x] Total PR: 500 lines (at limit)
- [x] `go vet ./internal/planner/...` clean
- [x] `go build ./cmd/orchestrator` succeeds
- [x] All internal tests pass (state 98.4%, registry 100%, planner 98.8%)

> **Note**: `-race` requires CGO (unavailable on Windows dev environment). CI (Linux) validates with `-race`.

#### Post-merge review findings (PR #9)

PR #9 was submitted as ~480 lines (79 `planner.go` new + 339 `resolve_test.go` + 62 carry-forward tests in `planner_test.go`), within the 500-line limit. Full review: [`docs/pr-reviews/pr-009-deep-review.md`](../../docs/pr-reviews/pr-009-deep-review.md).

All 4 carry-forward items from PR #8 were addressed:

| PR #8 Finding | Status |
|--------------|--------|
| F-01: Regex divergence comment | ✅ Resolved — comment at `planner.go` L27–31 |
| F-02: Unused testdata fixtures | ✅ Resolved — 3 fixture-based tests + `cycle_complex.yaml` fixed |
| F-05: Self-dependency check | ✅ Resolved — check in `validate()` + `TestParse_SelfDependency` |
| F-08: Malformed YAML test | ✅ Resolved — `TestParse_MalformedYAML` added |

Post-review fix commit addressed F-01 through F-04 from PR #9 review:

| Finding | Severity | Action | Disposition |
|---------|----------|--------|-------------|
| F-01: godoc capture group numbering off by one | Low | Resolved naturally by F-03 non-capturing group change | ✅ Fixed |
| F-02: Dead `matchedRanges` allocation and unused `warnSuspicious` parameter | Low | Removed allocation, removed unused `_ [][2]int` parameter | ✅ Fixed |
| F-03: `stepIDPattern` capturing group inflates `templateRegex` to 4 groups | Medium | Changed to non-capturing `(?:...)` — reduces to 3 capture groups, eliminates unused group | ✅ Fixed |
| F-04: Test case name "no spaces" misleading | Low | Renamed to "hyphen in variable name" — passthrough is due to hyphen not matching `[a-z_][a-z0-9_]*` | ✅ Fixed |

Actionable follow-ups for **PR 5 (wiring)** or later:

| Finding | Severity | Action | Disposition |
|---------|----------|--------|-------------|
| F-01: No nil-logger guard in `ResolveInputs` | Medium | Add `if logger == nil { logger = zap.NewNop() }` guard + test (inconsistent with `NewYAMLPlanner`, `NewInMemoryStore`, `NewInMemoryRegistry`) | Address in PR 5 |
| F-02: Step ID missing from resolve error messages | Low | Add `step.ID` to error format strings for executor-level debugging | Address in PR 5 or defer |
| F-03: No test for adjacent templates (`{{ a }}{{ b }}`) | Low | Add test to verify `lastEnd` index tracking with zero-length literal segments | Address in PR 5 or defer |
| F-04: No test for empty-string substitution value | Low | Add test for `outputs[k] = ""` resolving cleanly | Address in PR 5 or defer |

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
| PR 1 (state) exceeds 500 lines with concurrent tests | **Resolved**: PR #6 submitted at 781 lines; size waiver accepted (single-package, 100% coverage). See [review](../../docs/pr-reviews/pr-006-state-inmemory-review.md). |
| PR 3a (planner) exceeds 500 lines | **Resolved via mandatory split + size waiver**: PR 3a (Parse+DAG+Plan) and PR 3b (ResolveInputs). PR #8 at 1,071 lines was accepted. |
| `go.mod` merge conflicts between parallel PRs | Land PR 1 first; PRs 2 and 3a rebase on updated `main` |
| Planner tests depend on fixture YAML files | Fixtures stored in `internal/planner/testdata/`; `go test` CWD is package directory, so repo-root relative paths would not resolve |
| `gopkg.in/yaml.v3` anchor/alias behavior | Decode to `yaml.Node` tree, walk and reject `yaml.AliasNode` types, then decode node to struct (2-pass); pin exact version in `go.mod` |
| Fixture path resolution fails in CI | Using `testdata/` within the package (standard Go convention); no repo-root path dependency |
| PR 1 review findings need follow-up | Low-severity items (F-02 through F-06) tracked in PR plan; address in PR 1b or fold into subsequent PRs |
| PR 2 (registry) exceeds 500 lines | **Resolved**: PR #7 submitted at 637 lines; size waiver accepted (76% tests, single-package, 100% coverage). See [review](../../docs/pr-reviews/pr-007-registry-inmemory-review.md). |
| PR 2 review findings need follow-up | Medium: no agent ID validation (F-01, defer to RFC 0002). Low: `cap` builtin shadow (F-02), nil/empty slice inconsistency (F-03), no `AgentStatus.String()` (F-05). Tracked in PR plan. |
| PR 3a (planner) exceeds 500 lines | **Resolved**: PR #8 submitted at 1,071 lines; size waiver accepted (single-package, 97.7% coverage, 143 lines testdata YAML). See [review](../../docs/pr-reviews/pr-008-planner-yaml-review.md). |
| PR 3a review findings need follow-up | Medium: unused testdata fixtures (F-02, address in PR 3b), regex comment (F-01, PR 3b). Low: self-dep check (F-05), malformed YAML test (F-08). Tracked in PR plan. |
| PR 3b review findings need follow-up | Medium: nil-logger guard in `ResolveInputs` (F-01, address in PR 5). Low: step ID in error messages (F-02), adjacent template test (F-03), empty substitution test (F-04). Tracked in PR plan. |
| `UpdateRunStatus` has no timestamp management | Design gap documented for RFC 0003 — Scheduler will need `UpdateRun`/`PatchRun` or expanded `UpdateRunStatus` signature |
| `-race` flag requires CGO on Windows | CI (Linux) runs `-race`; local Windows testing uses `-cover` only. Concurrency correctness verified via code inspection and CI. |
