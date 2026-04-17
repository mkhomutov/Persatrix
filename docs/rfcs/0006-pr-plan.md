# RFC 0006 — PR Implementation Plan

**RFC**: [0006-efficiency-execution-limits.md](0006-efficiency-execution-limits.md)
**Created**: 2026-04-16
**Branch prefix**: `feature/v02-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)

---

## Overview

RFC 0006 defines execution limit propagation, budget enforcement, deadline derivation, retry budget policy, response caching, and execution observability. The RFC spans 4 implementation phases across Go orchestrator, Python agents, YAML config, and JSON schemas.

This plan splits the work into **10 PRs**: Phase 1 is split into 1a (Go defaults + planner Step limits + schema), 1b (executor + scheduler limit wiring), and 1c (Python defaults + agent validation). Phase 2 is one PR (deadline derivation + retry budget). Phase 3 is split into 3a (TokenCounter + BudgetEnforcer) and 3b (CostReporter + scheduler budget integration). Phase 4 is split into 4a (StepExecutionMetadata + observability) and 4b (response cache + cost summary endpoint). PR 5 is reserved for review follow-ups. PR 6 closes the RFC.

Each PR is independently mergeable and leaves the codebase in a passing-tests, lint-clean state.

> **Estimate calibration**: RFC 0005 PRs used a 1.7× calibration factor based on v0.1 actuals. This plan applies the same factor. Sizes below are calibrated estimates.

> **RFC 0008 coordination**: RFC 0008 Open Question 8 (resolved) specifies that the `context_budget` step-level field should be added alongside RFC 0006's step-level fields to minimize schema revision count. PR 1a includes this addition — it adds the field to the `Step` struct and `schemas/workflow.schema.json` but does not implement context budget logic (that is RFC 0008's scope). This is a schema-only addition that costs ~10 lines and avoids a separate schema migration PR in RFC 0008.

**Prerequisite**: RFC 0005 fully merged (20/20 PRs). The persona agent, memory system, task agent, and tool infrastructure are the foundation for execution limit enforcement.

**Recommended merge order**: **PR 1a** → **PR 1b** → **PR 1c** (can parallel with PR 1b — no Go dependency; both must merge before PR 2 starts) → **PR 2** → **PR 3a** → **PR 3b** → **PR 4a** → **PR 4b** (can parallel with PR 4a — independent code paths) → **PR 5** → **PR 6**.

---

## Dependency Graph

```
PR 1a (Go defaults + Step struct + schema)
  ↓
PR 1b (executor + scheduler limit wiring)    PR 1c (Python defaults + validation)
  ↓                                             ↓
PR 2 (deadline derivation + retry budget) ←────┘
  ↓
PR 3a (TokenCounter + BudgetEnforcer)
  ↓
PR 3b (CostReporter + scheduler budget integration)
  ↓
PR 4a (StepExecutionMetadata)    PR 4b (response cache + cost endpoint)
  ↓                                ↓
PR 5 (review follow-ups) ←────────┘
  ↓
PR 6 (RFC close)
```

---

## PR Sequence

### PR 1a: `feature/v02-defaults-step-limits` — Go Defaults Package + Step Limit Fields + Schema

**Depends on**: Nothing (builds on v0.1 + RFC 0005 infrastructure)
**Branch**: `feature/v02-defaults-step-limits`
**Estimated size**: ~300–450 lines (implementation + tests + schema)

#### Scope

| File | Change |
|------|--------|
| `internal/defaults/defaults.go` | **New** — Centralized Go system default constants (`DefaultMaxLLMCalls`, `DefaultMaxTokens`, `DefaultTimeoutSeconds`, `DefaultTransportMargin`, `MinRetryBudgetFraction`) |
| `internal/defaults/defaults_test.go` | **New** — Verify defaults are positive, non-zero, internally consistent |
| `internal/planner/planner.go` | Add `TimeoutSeconds`, `MaxLLMCalls`, `MaxTokens`, `ContextBudget` fields to `Step` struct; parse from YAML `steps` entries |
| `internal/planner/planner_test.go` | Test Step limit parsing: present, absent (zero = inherit), negative (reject) |
| `schemas/workflow.schema.json` | Add `timeout_seconds`, `max_llm_calls`, `max_tokens`, `context_budget` properties to step definition |
| `schemas/agent.schema.json` | Add `max_llm_calls`, `max_tokens` properties if not already present (agent-level defaults for cascade) |

#### Key implementation details

- `internal/defaults/` is a new package with exported constants. No logic — just named values replacing scattered magic numbers.
- `Step` struct gains optional int fields (zero means "inherit from agent config or system defaults"). The YAML parser reads these fields when present.
- `ContextBudget` is from RFC 0008 — added here to avoid a second schema change. It is an optional integer field with no enforcement logic in this PR.
- Negative limit values in workflow YAML are rejected during `Parse()` with a descriptive validation error.
- Workflow schema gains `timeout_seconds` (integer, minimum 1), `max_llm_calls` (integer, minimum 1), `max_tokens` (integer, minimum 1), `context_budget` (integer, minimum 1) — all optional.

#### Tests

- Defaults package: all constants > 0, `MinRetryBudgetFraction` in (0, 1).
- Planner: YAML with step limits → Step struct populated correctly.
- Planner: YAML without step limits → Step struct fields are zero (inherit).
- Planner: YAML with negative step limits → `Parse()` returns validation error.
- Schema: `make validate` passes with updated workflow YAML fixtures.

#### PR checklist

- [x] `go test ./internal/defaults/ -v -race` passes
- [x] `go test ./internal/planner/ -v -race` passes
- [x] `make validate` passes
- [x] `internal/defaults/` exports all constants listed in RFC 0006 Section B
- [x] `Step` struct includes `TimeoutSeconds`, `MaxLLMCalls`, `MaxTokens`, `ContextBudget`
- [x] Negative limits rejected during parse

#### Review Findings (PR #79)

**Should Fix (before merge):**

- [ ] Add `TestParse_StepLimits_MinimumValidValues` with `timeout_seconds: 1`, `max_llm_calls: 1`, `max_tokens: 1`, `context_budget: 1`. Verifies schema `minimum: 1` boundary is correctly parsed by Go. *(Location: `internal/planner/planner_test.go`)*
- [ ] Add `// TODO(RFC-0008): Enforce context budget during execution.` to the `ContextBudget` field comment in `Step` struct — makes deferred work searchable and consistent with project phase stub convention. *(Location: `internal/planner/planner.go`)*

**Deferred to PR 5:**

3. **Multi-step limit inheritance test** — Add a test with 2+ steps where step A has limits and step B does not, verifying both parse correctly in the same workflow. Exercises the partial-inheritance scenario that PR 1b's cascade logic will depend on.
4. **Schema description parity** — Add `"description"` attributes to the four new workflow schema properties (matching the agent schema style) for consistency and tooling support (e.g., VS Code YAML extension hover hints).
5. **Document schema zero-value behavior** — Add `"description"` to the workflow schema's `max_llm_calls` property noting: "Minimum 1 when specified. Omit to inherit from agent config or system defaults." Clarifies the `minimum: 1` constraint for users.

**Info (no action required):**

6. **`MinRetryBudgetFraction` is untyped constant** — Go handles this correctly (inferred as `float64` in comparisons). An explicit `float64` type would clarify intent, but untyped is idiomatic and allows use with both float32/float64. No change needed.
7. **Schema `minimum: 1` vs Go `>= 0` divergence** — Intentional design: schema prevents ambiguous explicit zeros while Go treats zero as "inherit". Good design choice, no change needed.

---

### PR 1b: `feature/v02-executor-limit-propagation` — Executor TaskConfig + Scheduler Limit Resolution

**Depends on**: PR 1a merged (Step struct has limit fields)
**Branch**: `feature/v02-executor-limit-propagation`
**Estimated size**: ~350–500 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `internal/executor/executor.go` | Extend `ExecuteRequest` with `StepLimits` struct; populate all `TaskConfig` fields (`MaxLlmCalls`, `MaxTokens`, `TimeoutSeconds`) from resolved limits before gRPC dispatch |
| `internal/executor/executor_test.go` | Test full TaskConfig population: step limits present, step limits absent (fallback), mixed |
| `internal/scheduler/scheduler.go` | Add `resolveStepLimits()` — three-level cascade (step config → agent config → system defaults); pass resolved limits in `ExecuteRequest` |
| `internal/scheduler/scheduler_test.go` | Test limit resolution cascade: step overrides agent, agent overrides defaults, zero means inherit |
| `internal/registry/registry.go` | Ensure agent config exposes `timeout_seconds`, `max_llm_calls`, `max_tokens` for lookup (may already be available via agent config map) |

#### Key implementation details

- New `StepLimits` struct in executor package: `MaxLLMCalls int`, `MaxTokens int`, `TimeoutSeconds int`. Added to `ExecuteRequest`.
- Scheduler's `resolveStepLimits(step, agentConfig)` implements the cascade:
  ```
  resolved.MaxLLMCalls = step.MaxLLMCalls || agentConfig.MaxLLMCalls || defaults.DefaultMaxLLMCalls
  ```
- Executor's `dispatch()` populates `TaskConfig` from resolved limits instead of only setting `TimeoutSeconds`.
- Agent registry must expose per-agent config values. If the registry currently stores only connection info, extend it to include limit fields from `config/agents.yaml`.

#### Tests

- Cascade: step limit set → step value used.
- Cascade: step limit zero, agent config set → agent value used.
- Cascade: both zero → system default used.
- TaskConfig in mock gRPC call contains all three limit fields.
- Negative resolved limits (shouldn't happen — planner rejects, but defensive) → clamped to defaults.

#### PR checklist

- [x] `go test ./internal/executor/ -v -race` passes
- [x] `go test ./internal/scheduler/ -v -race` passes
- [x] `go test ./internal/registry/ -v -race` passes
- [x] `ExecuteRequest` includes `StepLimits`
- [x] `TaskConfig` fully populated (no zero-value `MaxLlmCalls`/`MaxTokens`)
- [x] Three-level cascade verified in tests

#### Review Findings (PR #81)

**Addressed in PR 2 (planned):**

1. **F-01 (Medium) — gRPC call timeout vs `TaskConfig.TimeoutSeconds` mismatch** — `e.timeout` (30s default) controls the actual `context.WithTimeout` in `dispatch()`, while `req.Limits.TimeoutSeconds` (now correctly populated from the three-level cascade) is only sent in the `TaskConfig` proto. An agent configured with a 300s step timeout receives that value in `TaskConfig` but gets its gRPC call cancelled after 30s. PR 2 explicitly replaces static per-executor timeouts with derived deadlines (`rpc_timeout = step.TimeoutSeconds + transport_margin`) — this finding is subsumed by that work. *(Location: `internal/executor/executor.go` L156, L226)*

**Deferred to PR 5:**

2. **F-02 (Low) — `int` → `int32` truncation without bounds check** — `MaxLlmCalls`, `MaxTokens`, and `TimeoutSeconds` are cast from `int` to `int32` without overflow guards. Values exceeding `math.MaxInt32` would silently wrap. In practice bounded by config, but a clamp or validation in the planner is safer. *(Location: `internal/executor/executor.go` L154–156)*
3. **F-04 (Low) — Negative limit values silently ignored** — Negative step/agent config values pass the `> 0` check as false and fall through to defaults without any warning. While the planner rejects negative values at parse time (PR 1a), agent-level limits come from the registry and are not validated. Add negative-value rejection to `resolveStepLimits()` or to `AgentInfo` registration. *(Location: `internal/scheduler/scheduler.go` L407–430)*
4. **F-05 (Nit) — Redundant `TestExecuteTask_PopulatesTaskConfig_AllFields`** — Nearly identical to the updated `TestExecuteTask_PopulatesTaskConfig`; both verify `StepLimits` → `TaskConfig` field mapping with different numbers. Consolidate or delete the duplicate to reduce maintenance surface. *(Location: `internal/executor/executor_test.go`)*

**Info (no action required):**

5. **F-03 (Design) — Zero as "not configured" prevents explicit zero limits** — The `> 0` cascade convention means it is impossible to set a limit to 0 at the agent or step level. Intentional for v0.1/v0.2; would require pointer types or sentinel values to change. No action.

---

### PR 1c: `feature/v02-python-defaults-validation` — Python Defaults + Agent Limit Validation

**Depends on**: PR 1a merged (schema changes ensure consistent contract). Can parallel with PR 1b — no Go dependency (both must merge before PR 2 starts).
**Branch**: `feature/v02-python-defaults-validation`
**Estimated size**: ~200–350 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `agents/defaults.py` | **New** — `DEFAULT_MAX_LLM_CALLS = 5`, `DEFAULT_MAX_TOKENS = 8192`, `DEFAULT_TIMEOUT_SECONDS = 60` and other Python-side constants |
| `agents/base.py` | Import from `defaults.py` instead of inline magic numbers; add validation in `_run_llm_loop()` — reject negative `max_llm_calls`/`max_tokens`, clamp zero to defaults |
| `tests/unit/python/test_defaults.py` | **New** — Verify defaults module constants match Go-side values conceptually |
| `tests/unit/python/test_agents.py` | Add tests: negative limits → `ValueError`, zero limits → default substitution |

#### Key implementation details

- `agents/defaults.py` centralizes constants currently scattered across `base.py` (10 LLM calls, 4096 tokens).
- Default changes per RFC Section B: `max_llm_calls` 10 → 5, `max_tokens` 4096 → 8192 for task agents.
- Persona agent constants (`_MAX_SUB_AGENT_TOKENS`, etc.) remain in `persona_runtime.py` per RFC Open Question 4 — not migrated here.
- `_run_llm_loop()` validation: if `max_llm_calls < 0` or `max_tokens < 0`, raise `ValueError("Negative execution limits are not allowed")`.
- Zero values continue to mean "use default" — existing behavior preserved.
- **Breaking change**: `max_llm_calls` default drops from 10 → 5. Agents performing complex multi-tool operations that relied on the 10-call default may need explicit step-level `max_llm_calls` overrides. Document this in CHANGELOG.md as a breaking behavioral change and consider logging a warning when an agent hits the default limit.
- **Note**: Python-side `context_budget` handling is deferred to RFC 0008 implementation. PR 1a adds the field to Go structs and workflow schema, but no Python-side constant or validation is introduced here — that is RFC 0008's scope.

#### Tests

- `defaults.py`: all constants > 0.
- `base.py`: negative `max_llm_calls` in TaskInputConfig → `ValueError`.
- `base.py`: negative `max_tokens` in TaskInputConfig → `ValueError`.
- `base.py`: zero `max_llm_calls` → resolved to `DEFAULT_MAX_LLM_CALLS` (5).
- `base.py`: zero `max_tokens` → resolved to `DEFAULT_MAX_TOKENS` (8192).
- `base.py`: explicit positive value → used as-is.

#### PR checklist

- [x] `pytest tests/unit/python/ -v` passes
- [x] `ruff check agents/` clean
- [x] `mypy agents/` passes
- [x] `agents/defaults.py` exports all constants listed in RFC 0006 Section B
- [x] `_run_llm_loop()` rejects negative limits
- [x] No inline magic numbers remain in `base.py` for `max_llm_calls`/`max_tokens`

#### Review Findings (PR #83)

**Deferred to PR 5:**

1. **M1 (Medium) — `ValueError` vs `TaskOutput(FAILED)` inconsistency** — Negative limits raise `ValueError`, but all other error conditions in `_run_llm_loop()` (missing model, LLM provider errors) return `TaskOutput(status=FAILED)`. A `ValueError` propagates through the gRPC servicer as an opaque gRPC error, while `TaskOutput.FAILED` is the expected reporting mechanism. Consider returning `TaskOutput(FAILED)` instead, or documenting the intentional fail-fast distinction. *(Location: `agents/base.py` L250–251)*
2. **M2 (Medium) — `DEFAULT_TIMEOUT_SECONDS` defined but unused** — The constant is exported and tested but never referenced in `base.py` or anywhere else. Wire it up as a timeout fallback or add a comment noting it is a forward-declared constant for PR 2. *(Location: `agents/defaults.py` L30)*
3. **L2 (Low) — No loop-exhaustion test for new default** — Tests verify zero resolves to defaults and explicit values are used, but no test confirms the loop actually stops after `DEFAULT_MAX_LLM_CALLS` (5) iterations. Add a test that passes `max_llm_calls=0` with a mock always returning `TOOL_USE`, then assert exactly 5 LLM calls. *(Location: `tests/unit/python/test_agents.py`)*

**Info (no action required):**

4. **L3 — Breaking change documented** — CHANGELOG correctly documents `max_llm_calls` 10→5 as a breaking change. Good.

---

### PR 2: `feature/v02-deadline-derivation` — Deadline Derivation + Retry Budget Policy

**Depends on**: PR 1b merged (executor has step limits), PR 1c merged (Python validates limits)
**Branch**: `feature/v02-deadline-derivation`
**Estimated size**: ~350–500 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `cmd/orchestrator/main.go` | Remove static `executor.WithTimeout(5*time.Minute)`; wire deadline mode config flag (`execution.deadline_mode: "derived" | "static"`) |
| `internal/executor/executor.go` | Replace per-executor timeout with per-dispatch derived deadline: `rpc_timeout = step_deadline + transport_margin`; implement shared-deadline retry (retries consume from step deadline, not fresh windows); add minimum-budget check before retry (`>= 25%` of original budget remaining) |
| `internal/executor/executor_test.go` | Tests for derived deadlines, shared retry budget, minimum budget cutoff |
| `config/environments/development.yaml` | Add `execution.deadline_mode: "derived"` (can be overridden to `"static"` for rollback) |

#### Key implementation details

- Derived deadline: `rpc_timeout = step.TimeoutSeconds + defaults.DefaultTransportMargin (5s)`.
- Shared-deadline retry: track elapsed time across retries. Each retry attempt gets `remaining_deadline = step_deadline - elapsed`. If `remaining < step_deadline * defaults.MinRetryBudgetFraction (0.25)`, skip retry.
- Token budget tracking across retries: accumulate `tokens_used` from response metadata. If cumulative tokens exceed step's `max_tokens` budget, skip retry.
- Config flag for rollback safety: `execution.deadline_mode: "derived"` (default) or `"static"` (reverts to PR 1b behavior with per-executor timeout).
- When `deadline_mode: "static"`, the executor falls back to the configured timeout from `main.go` (preserving backward compatibility).

#### Tests

- Derived deadline: step timeout 60s → RPC timeout 65s.
- Shared deadline: first attempt takes 45s of 60s → retry gets 15s context.
- Minimum budget: only 10% remaining → retry skipped.
- Token budget: first attempt used 7000/8192 tokens → retry gets remaining 1192 budget.
- Static mode: flag set to `"static"` → original per-executor timeout behavior.
- `DeadlineExceeded` error from gRPC → no retry (permanent error, existing behavior preserved).

#### PR checklist

- [x] `go test ./internal/executor/ -v -race` passes
- [x] `go test ./cmd/orchestrator/ -v -race` passes (if applicable)
- [x] Static `WithTimeout(5*time.Minute)` retained as fallback (used in static mode and zero-timeout derived mode); `--deadline-mode` flag added for runtime mode selection
- [x] Derived deadline computed from step config + transport margin
- [x] Retries share step deadline (not fresh windows)
- [x] Minimum budget check prevents wasteful retries
- [x] Config flag `execution.deadline_mode` controls behavior

#### Review Findings (PR #84)

**Applied:**

1. **S1 (Low) — PR plan checklist wording** — Checklist item 3 said "Static `WithTimeout(5*time.Minute)` removed from `main.go`" but the timeout is intentionally retained as a fallback for static mode and zero-timeout derived mode. Reworded and checked. *(Location: this section)*
2. **S2 (Low) — `WithTokenParser` option for clean test injection** — Tests directly mutated `env.executor.tokenParser` (unexported field). Added `WithTokenParser` option following the established `With*` pattern. *(Location: `internal/executor/executor.go`)*
3. **S3 (Low) — Concurrent derived-mode dispatch test** — Existing `TestExecuteTask_ConcurrentDispatch` used static mode only. Added `TestDerivedDeadline_ConcurrentDispatch` to validate `time.Since(start)` goroutine isolation under race detector. *(Location: `internal/executor/executor_test.go`)*
4. **S4 (Info) — Token budget cutoff documented as infrastructure-only** — Added inline comment at the token budget check in the retry loop noting that `parseTokensUsed` returns 0 until gRPC trailer metadata parsing is implemented. *(Location: `internal/executor/executor.go`)*
5. **S5 (Info) — Debug logging for derived deadline computation** — Added DEBUG-level log when derived mode computes step deadline and dispatch timeout. Aids production debugging without code changes. *(Location: `internal/executor/executor.go`)*

**Should Fix (from deep review):**

6. **S6 (Low) — Clarifying comment for transport margin in retry timeout** — Each retry dispatch timeout is `remaining + transport_margin`, which could appear to extend beyond the original deadline. Total wall-clock is bounded because `remaining ≤ stepDeadline`, so `remaining + margin ≤ stepDeadline + margin` (original dispatch timeout). Add an inline comment at `ExecuteTask()` ~L221 explaining this invariant to prevent future confusion. *(Location: `internal/executor/executor.go`)*
7. **S7 (Low) — Test independent token budget cutoff** — No test where token budget blocks retry but time budget allows. Add `TestDerivedDeadline_TokenBudgetCutoff_TimeAllowed` — inject a parser returning high tokens with a long step timeout (60s). Verify retry is skipped due to token budget, not time budget. Validates that both constraints are independently evaluated. *(Location: `internal/executor/executor_test.go`)*
8. **S8 (Low) — Verify ROADMAP status consistency** — ROADMAP.md shows PR 1c (#83) as `⬜ next` but PR plan shows review findings for it, suggesting it was reviewed. Verify PR 1c merge status and update ROADMAP accordingly before merging PR 2. The Go/Python code changes are independent, but the logical dependency (Python validates limits before orchestrator changes deadline semantics) should be acknowledged. *(Location: `ROADMAP.md`)*

**Deferred to PR 5:**

9. **S9 (Low) — Backoff-aware budget check** — Before sleeping for backoff, check if `remaining - backoff_duration` still exceeds `minBudget`. Low priority since the window is small (max ~400ms backoff vs typical 60s+ step deadlines).
10. **S10 (Low) — Upper-bound validation for `TimeoutSeconds`** — No upper bound on `TimeoutSeconds`. A reasonable cap (e.g., 3600s) in the planner or schema would prevent absurd deadlines. Low priority since proto int32 bounds and workflow deadlines (future) provide natural limits.
11. **S11 (Low) — Extract `--deadline-mode` inference into testable function** — The inference logic in `main.go` (lines 64–72) is straightforward but untestable in isolation. Extracting into `func resolveDeadlineMode(explicit, env string) string` would enable unit testing. Low priority since the logic is trivial. *(Location: `cmd/orchestrator/main.go`)*
12. **S12 (Low) — Test `MaxTokens = 0` with derived mode + token parser** — Verify the `MaxTokens > 0` guard explicitly prevents token budget evaluation when `MaxTokens` is unresolved. Validates the zero-value guard independently. *(Location: `internal/executor/executor_test.go`)*

**Info (no action required):**

13. **`parseTokensUsed` always returns 0** — By design. Token budget infrastructure is wired end-to-end and tested via injected parser. Production parsing deferred to PR 3a when gRPC trailer metadata is available.
14. **`development.yaml` config is documentation-only** — No code reads `execution.deadline_mode` from YAML yet; `--deadline-mode` CLI flag is the sole mechanism. Documented in YAML comments. Config loading tracked for follow-up.
15. **`int` → `int32` cast without overflow guard** — Pre-existing from PR 1b (deferred finding F-02). `int32(req.Limits.MaxLLMCalls)` et al. could silently overflow for values > 2^31. In practice bounded by planner validation and schema `minimum: 1`. Tracked in PR 1b findings.
16. **Dual-layer mode validation is intentional defense-in-depth** — `main.go` validates at startup (fail-fast); constructor validates and falls back (programmatic misuse guard). Matches existing `--env` pattern.

---

### PR 3a: `feature/v02-token-counter-budget-enforcer` — TokenCounter + BudgetEnforcer

**Depends on**: PR 2 merged (executor tracks token consumption per step)
**Branch**: `feature/v02-token-counter-budget-enforcer`
**Estimated size**: ~350–500 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `internal/cost/cost.go` | Replace TODO stub with `TokenCounter` (thread-safe running totals per workflow, per agent, global daily) and `BudgetEnforcer` (pre-dispatch budget check with `on_exceed` behavior) |
| `internal/cost/cost_test.go` | **New** — Unit tests for TokenCounter and BudgetEnforcer |
| `internal/cost/config.go` | **New** — Budget config loader: read thresholds from `config/optimization.yaml` (`budgets.global`, `budgets.per_workflow`, `budgets.per_agent`) and pricing table (`cost.pricing.models`) |

#### Key implementation details

- `TokenCounter`: uses `sync.Mutex` for thread safety. Maintains `map[string]int64` for per-workflow and per-agent totals, plus a global daily counter with midnight reset.
- `RecordUsage(workflowID, agentID, model string, inputTokens, outputTokens int64)` — updates all three counters and computes estimated cost using the pricing table.
- `BudgetEnforcer`: `CheckBudget(workflowID, agentID string, estimatedMaxTokens int64) BudgetDecision` — returns `Allow`, `Reject`, or `PauseNotImplemented`.
- Pre-dispatch heuristic guard per RFC Open Question 3: multiply `max_tokens` by model's per-token cost. If remaining budget < estimated max cost, reject.
- `on_exceed: "fail"` is the only implemented behavior per RFC Open Question 2. `"pause_and_alert"` is accepted as config but treated as `"fail"` with a warning log at config load time and at enforcement time.
- Budget thresholds loaded from `config/optimization.yaml` — fields already exist (`max_daily_usd`, `per_workflow.default_max_usd`, `per_agent.default_max_usd`).

#### Tests

- TokenCounter: record usage → per-workflow total correct.
- TokenCounter: record usage → per-agent total correct.
- TokenCounter: record usage → global daily total correct.
- TokenCounter: concurrent usage recording (goroutine safety).
- BudgetEnforcer: under budget → Allow.
- BudgetEnforcer: per-workflow budget exceeded → Reject.
- BudgetEnforcer: per-agent budget exceeded → Reject.
- BudgetEnforcer: global daily budget exceeded → Reject.
- BudgetEnforcer: `on_exceed: "pause_and_alert"` → treated as Reject with warning.
- Pre-dispatch guard: estimated cost > remaining → Reject.
- Config loader: valid optimization.yaml → thresholds parsed correctly.

#### PR checklist

- [x] `go test ./internal/cost/ -v -race` passes
- [x] `TokenCounter` is thread-safe (race detector clean)
- [x] `BudgetEnforcer` implements `on_exceed: "fail"` behavior
- [x] `on_exceed: "pause_and_alert"` degrades to `"fail"` with warning
- [x] Config loads from `config/optimization.yaml` budget section
- [x] Pricing table used for cost estimation
- [x] All TODO comments in `cost.go` replaced with implementation

#### Review Findings (PR #85)

**Should Fix (quality improvement):**

1. **C1 (Low) — Missing CHANGELOG entry** — The `[Unreleased]` section does not include the cost/budget feature. Previous PRs in this RFC (PR 2, PR 1c) have entries. Add: `*(orchestrator)* Implement TokenCounter and BudgetEnforcer: per-workflow, per-agent, and global daily budget tracking with pre-dispatch cost gating (RFC 0006 PR 3a, #85)`. *(Location: `CHANGELOG.md`)*
2. **C2 (Low) — `pause_and_alert` warning log not verified in test** — `TestBudgetEnforcer_PauseAndAlert_TreatedAsFail` uses `zap.NewNop()`, so the constructor warning and enforcement-time warning logs are not observed. Use `zaptest.NewLogger(t)` or `zap.NewDevelopment()` sink to assert warnings are emitted. *(Location: `internal/cost/cost_test.go`)*
3. **C3 (Low) — No Debug log for unknown model in `EstimateCost`** — Unknown models return `$0` silently, meaning all dispatches for unrecognized models bypass budget checks. Add a `Debug`-level log when a model is not found in the pricing table. Helps operators diagnose cost tracking showing $0 for certain agents (e.g., model name mismatch between config and usage). *(Location: `internal/cost/config.go` → `EstimateCost()`)*

**Deferred to PR 5:**

4. **C4 (Low) — Non-atomic multi-scope snapshot in `CheckBudget`** — Three separate lock acquisitions per budget check (Global → Workflow → Agent) creates non-atomic scope reads. Accepted per RFC TOCTOU note, but could be tightened with an internal `snapshot()` method reading all three totals under a single lock without changing the public API. *(Location: `internal/cost/cost.go` → `CheckBudget()`)*
5. **C5 (Low) — Explicit `ResetDaily` agent-scope assertion in test** — `TestTokenCounter_ResetDaily` verifies global and workflow reset but not agent-scope reset explicitly. The `perAgent` map is reassigned so it's implicitly tested, but an explicit assertion would be more thorough. *(Location: `internal/cost/cost_test.go`)*
6. **C6 (Low) — Test concurrent `CheckBudget` + `RecordUsage`** — No test exercises `CheckBudget` racing against `RecordUsage`. Would validate the TOCTOU gap is benign under `-race`. *(Location: `internal/cost/cost_test.go`)*
7. **C7 (Low) — Config validation for budget thresholds** — No validation that `MaxDailyUSD >= 0`, `DefaultMaxUSD >= 0`, or `OnExceed` is one of `"fail"` or `"pause_and_alert"` at config load time. Negative values silently disable enforcement and unknown `on_exceed` values are silently accepted. *(Location: `internal/cost/config.go` → `LoadCostConfig()`)*

**Positive deviation (no action required):**

8. **`CheckBudget` API improved over plan** — Plan specified `CheckBudget(workflowID, agentID string, estimatedMaxTokens int64) BudgetDecision`. Implementation adds `model` parameter (necessary for pricing lookup) and returns `BudgetCheckResult` (wraps `BudgetDecision` + `Reason` for better error messages). `PauseNotImplemented` return value correctly omitted — `pause_and_alert` degrades to `BudgetReject` with warning. All improvements.
9. **Float64 cost accumulation** — Acceptable for v0.2 internal cost estimation (not billing). Thousands of micro-cost events could accumulate rounding errors at production scale, but not a concern for current phase.

---

### PR 3b: `feature/v02-cost-reporter-scheduler-budget` — CostReporter + Scheduler Budget Integration

**Depends on**: PR 3a merged (TokenCounter and BudgetEnforcer available)
**Branch**: `feature/v02-cost-reporter-scheduler-budget`
**Estimated size**: ~300–450 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `internal/cost/reporter.go` | **New** — `CostReporter`: aggregates cost metadata per workflow run, per agent; provides summary data for API responses |
| `internal/cost/reporter_test.go` | **New** — CostReporter unit tests |
| `internal/scheduler/scheduler.go` | Add pre-dispatch `BudgetEnforcer.CheckBudget()` call before `executor.ExecuteTask()`; post-dispatch `TokenCounter.RecordUsage()` from response metadata; fail step if budget rejected |
| `internal/scheduler/scheduler_test.go` | Test budget check integration: under budget → dispatch proceeds, over budget → step fails with budget error |
| `cmd/orchestrator/main.go` | Wire `TokenCounter`, `BudgetEnforcer`, `CostReporter` into scheduler construction |

#### Key implementation details

- `CostReporter` wraps `TokenCounter` data into structured summaries: `WorkflowCostSummary` (per-run token totals, estimated USD, per-step breakdown) and `GlobalCostSummary` (daily totals, top agents, top workflows).
- Scheduler integration: before `executeStep()` dispatches, it calls `enforcer.CheckBudget()`. If rejected, step transitions to `Failed` with error `"budget exceeded: <detail>"`. After successful dispatch, it calls `counter.RecordUsage()` with tokens from `ExecuteResult.Metadata["tokens_used"]`.
- Token metadata parsing: agent responses include `tokens_used` in metadata (already populated by Python agents in `TaskOutput.metadata`). Scheduler parses this string to int64.
- `main.go` creates `TokenCounter`, `BudgetEnforcer`, `CostReporter` and passes them to `WorkflowScheduler` constructor (new parameters or options pattern).

#### Tests

- Scheduler: budget check passes → step dispatched normally.
- Scheduler: budget check rejects → step marked Failed, error includes "budget exceeded".
- Scheduler: after dispatch, token usage recorded in counter.
- Scheduler: missing `tokens_used` metadata → warning logged, zero recorded (graceful degradation).
- CostReporter: workflow summary includes per-step token counts.
- CostReporter: global summary includes daily totals.

#### PR checklist

- [x] `go test ./internal/cost/ -v -race` passes
- [x] `go test ./internal/scheduler/ -v -race` passes
- [x] Pre-dispatch budget check wired into scheduler
- [x] Post-dispatch token recording wired into scheduler
- [x] `CostReporter` produces workflow and global summaries
- [x] `main.go` wires cost components into scheduler

#### Review Findings (PR #86)

**Should Fix (quality improvement):**

1. **S-01 (Low) — No parallel budget overspend test** — Missing test where N parallel steps within a stage all pass budget checks and collectively exceed the budget. Would document the known optimistic check behavior under `-race`. *(Location: `internal/scheduler/scheduler_test.go`)*
2. **S-02 (Low) — No concurrent `CheckBudget` + `RecordUsage` race test** — From PR 3a finding C6, still unaddressed. Spawn goroutines that interleave `CheckBudget` and `RecordUsage` calls; run under `-race` to validate the TOCTOU gap is benign. *(Location: `internal/cost/cost_test.go`)*
3. **S-03 (Low) — `ResetDaily` TODO missing tracking reference** — The TODO at `cmd/orchestrator/main.go` says `(PR #86 review S-05)` but doesn't reference a tracking issue or future PR. Add `// TODO(v0.2): Wire to midnight timer — see RFC 0006 PR 5 review follow-ups` for traceability. *(Location: `cmd/orchestrator/main.go`)*

**Nice to Have (follow-up):**

4. **N-01 (Low) — Atomic snapshot for multi-scope budget check** — Replace three separate lock acquisitions (Global → Workflow → Agent) in `CheckBudget` with a single `snapshot()` method. Pre-existing finding C4 from PR 3a. *(Location: `internal/cost/cost.go`)*
5. **N-02 (Low) — Config validation for budget thresholds** — Validate `MaxDailyUSD >= 0`, `DefaultMaxUSD >= 0`, and `OnExceed` enum at config load time. Pre-existing finding C7 from PR 3a. *(Location: `internal/cost/config.go`)*
6. **N-03 (Low) — Structured error type for budget rejection** — Replace `fmt.Errorf("%w: %s", ErrBudgetExceeded, reason)` with a `BudgetError` struct containing `Scope`, `Spent`, `Limit`, `Estimated` fields. Enables structured 429 responses in PR 4b. *(Location: `internal/cost/cost.go`, `internal/scheduler/scheduler.go`)*

**Positive (no action required):**

7. **Excellent inline comments** — TOCTOU documentation, `ResetDaily` ordering rationale, negative-token clamping security note, and pessimistic `tokens_used` fallback explanation are all high quality.
8. **Nil-safe optional injection** — `WithCostComponents()` pattern with nil checks maintains backward compatibility. `TestNoCostComponents_NoPanic` validates this.
9. **Comprehensive test coverage** — 12 reporter tests + 14 scheduler budget tests covering happy path, rejection, error wrapping, concurrency, fallback, security clamping, and observability.
10. **Boundary compliance** — PR stays within Go orchestrator boundary. No LLM logic, no Python changes, no proto modifications.

---

### PR 4a: `feature/v02-execution-metadata` — StepExecutionMetadata + Observability

**Depends on**: PR 3b merged (cost data available from scheduler)
**Branch**: `feature/v02-execution-metadata`
**Estimated size**: ~300–450 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `internal/state/state.go` | Add `StepExecutionMetadata` struct to `StepState`: `TokensUsed`, `LLMCallCount`, `RetryCount`, `CacheHit`, `WallTime`, `EstimatedCostUSD` |
| `internal/state/state_test.go` | Test metadata storage and retrieval |
| `internal/executor/executor.go` | Populate `StepExecutionMetadata` after each dispatch: wall time measured, retry count tracked, token/cost data from response |
| `internal/executor/executor_test.go` | Test metadata population in various scenarios (success, retry, failure) |
| `internal/server/workflow_handlers.go` | Include `StepExecutionMetadata` in `GET /api/v1/workflows/{id}/status` response |
| `internal/server/workflow_handlers_test.go` | Test metadata appears in status response JSON |

#### Key implementation details

- `StepExecutionMetadata` is a Go struct (not a proto message, per RFC Section A clarification). Stored in `StepState` and serialized to JSON in API responses.
- Executor measures wall time using `time.Since(start)` around the dispatch + retry loop.
- `RetryCount` incremented per retry attempt. `LLMCallCount` parsed from response metadata (agents report this). `CacheHit` is always `false` until PR 4b.
- `EstimatedCostUSD` computed using pricing table from `cost.Config`.
- Status API response JSON gains a `metadata` field per step with the execution metadata.
- Metadata logged at INFO level after each step completes: `logger.Info("step completed", zap.Int("tokens_used", ...), zap.Int("retry_count", ...), ...)`.

#### Tests

- State: store step with metadata → retrieve includes metadata.
- Executor: successful dispatch → metadata has wall time, zero retries.
- Executor: dispatch with 2 retries → metadata has retry count 2.
- Server: status response includes `metadata` field per step with correct values.
- Metadata: missing response fields → zero values (graceful degradation).

#### PR checklist

- [x] `go test ./internal/state/ -v -race` passes
- [x] `go test ./internal/executor/ -v -race` passes
- [x] `go test ./internal/server/ -v -race` passes
- [x] `StepExecutionMetadata` struct defined with all 6 fields
- [x] Executor populates metadata after each step
- [x] Status API includes per-step metadata
- [x] Metadata logged at INFO level

#### Review Findings (PR #87)

**Should Fix (quality improvement):**

1. **M-01 (Medium) — Cost estimation inconsistency between `recordStepUsage` and `buildStepMetadata`** — When an agent only reports `tokens_used` (no `input_tokens`/`output_tokens`), `recordStepUsage` maps it to `outputTokens` and computes a pessimistic cost. But `buildStepMetadata` passes `input_tokens=0, output_tokens=0` to `EstimateCost`, resulting in `$0` cost in the metadata — despite non-zero `TokensUsed` in the same struct. Fix: use resolved `tokensUsed` as `outputTokens` fallback in `buildStepMetadata`, or extract a shared token resolution function for both callers. *(Location: `internal/scheduler/scheduler.go` → `buildStepMetadata()` lines 670–685)*
2. **M-02 (Low) — No deep copy of Metadata pointer on write in `UpdateStepState`** — Stores caller's pointer directly. Asymmetric with `CreateRun` which deep-copies. A future caller reusing a metadata pointer would silently corrupt store state. Fix: add `if step.Metadata != nil { metaCopy := *step.Metadata; step.Metadata = &metaCopy }` before `run.Steps[step.StepID] = step`. *(Location: `internal/state/state.go` → `UpdateStepState()` L201)*
3. **M-03 (Low) — Empty step ID in `buildStepMetadata` warning logs** — `parseMetadataInt64` calls inside `buildStepMetadata` pass `""` as step ID, making warning logs undiagnosable ("failed to parse metadata value as int64, stepID="). Fix: add `stepID string` parameter to `buildStepMetadata` and pass `step.ID` from the call site. *(Location: `internal/scheduler/scheduler.go` → `buildStepMetadata()` L650)*
4. **M-04 (Low) — Missing write-isolation test for metadata** — `TestGetRunDeepCopy_MetadataIsolation` tests read isolation only. No test verifies that mutating input metadata after `UpdateStepState` doesn't affect the store. Fix: add `TestUpdateStepState_WriteIsolation`. *(Location: `internal/state/state_test.go`)*

**Nice to Have (follow-up):**

5. **N-01 — Extract shared `resolveStepTokenData` helper** — Merge duplicated metadata parsing in `recordStepUsage` and `buildStepMetadata` into a single function returning a struct with `TokensUsed`, `InputTokens`, `OutputTokens`, `LLMCallCount`, `Model`, `EstimatedCostUSD`. Eliminates divergence risk. *(Location: `internal/scheduler/scheduler.go`)*
6. **N-02 — Add `WallTimeMs` accuracy test** — Inject a mock agent with a fixed delay and assert `result.WallTimeMs >= delay`. Validates the measurement mechanism rather than just checking non-zero. *(Location: `internal/executor/executor_test.go`)*

**Info (no action required):**

7. **`int64` → `int` narrowing in `buildStepMetadata`** — `tokensUsed = int(parseMetadataInt64(...))` narrows int64 to int. On 64-bit platforms this is a no-op. Low risk given server deployment targets.
8. **Redundant nil check in `buildStepMetadata`** — `result.Metadata != nil` rechecked inside an outer condition that already guarantees non-nil. Harmless but redundant.
9. **`WallTimeMs` set only on success** — Failed steps have no wall time metadata. Acceptable for v0.2 since metadata is observability-only.
10. **`CacheHit` hardcoded to `false`** — Correct forward-declaration for PR 4b. The constant `false` with a comment is better than omitting the field.

---

### PR 4b: `feature/v02-response-cache-cost-endpoint` — Response Cache + Cost Summary Endpoint

**Depends on**: PR 3b merged (cost data available). Can parallel with PR 4a — independent code paths.
**Branch**: `feature/v02-response-cache-cost-endpoint`
**Estimated size**: ~350–500 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `internal/cost/cache.go` | **New** — In-memory LRU response cache with configurable TTL. Key: hash of (agent_id, task_type, task_input, context, model, system_prompt, sampling_parameters, allowed_tools). Opt-in via `cacheable` step flag. |
| `internal/cost/cache_test.go` | **New** — Cache tests: hit, miss, eviction, TTL expiry, opt-in enforcement |
| `internal/executor/executor.go` | Check cache before dispatch; store result on cache miss; record `CacheHit` in metadata |
| `internal/server/cost_handlers.go` | **New** — `GET /api/v1/cost/summary` endpoint returning global and per-workflow cost summaries from `CostReporter` |
| `internal/server/cost_handlers_test.go` | **New** — Cost endpoint tests |
| `internal/server/routes.go` | Register `/api/v1/cost/summary` route |
| `schemas/workflow.schema.json` | Add `cacheable` boolean property to step definition |

#### Key implementation details

- Cache key: SHA-256 hash of the full `TaskRequest` content minus volatile fields (`task_id`, `workflow_id`). This future-proofs against proto evolution.
- **Permission isolation**: The cache key includes `agent_id`, which partitions cache entries per agent. This satisfies RFC 0006 Security Consideration that cache entries must not be shared across agents with different permission sets — agents with different permissions have different IDs and therefore different cache keys.
- Cache store: `map[string]cacheEntry` with LRU eviction (doubly-linked list) and per-entry TTL from `config/optimization.yaml` (`caching.exact.ttl_seconds: 3600`, `caching.exact.max_entries: 10000`).
- Cache is checked only for steps with `cacheable: true` in workflow YAML. Persona and autonomous tasks are never cached.
- Cache hit → return stored result, set `CacheHit = true` in metadata, skip gRPC dispatch.
- Cost summary endpoint returns JSON with daily totals, top-spending workflows, top-spending agents, model breakdown.
- Endpoint requires no auth in this PR (auth deferred to RFC 0009, noted in RFC Security Considerations).

#### Tests

- Cache: same request twice → second is cache hit.
- Cache: different agent_id → cache miss (key differs).
- Cache: TTL expired → cache miss.
- Cache: max entries exceeded → LRU entry evicted.
- Cache: step without `cacheable: true` → cache bypassed.
- Cost endpoint: returns valid JSON with expected fields.
- Cost endpoint: empty state → zero totals.
- Schema: `cacheable` field validates in workflow YAML.

#### PR checklist

- [ ] `go test ./internal/cost/ -v -race` passes
- [ ] `go test ./internal/executor/ -v -race` passes
- [ ] `go test ./internal/server/ -v -race` passes
- [ ] `make validate` passes with `cacheable` field
- [ ] Cache respects opt-in (`cacheable: true` only)
- [ ] Cache key includes all relevant request fields
- [ ] LRU eviction and TTL expiry working
- [ ] Cost summary endpoint registered and tested

---

### PR 5: `feature/v02-rfc0006-review-followups` — Review Follow-Ups

**Depends on**: All core PRs (1a–4b) merged
**Branch**: `feature/v02-rfc0006-review-followups`
**Estimated size**: TBD (populated during review)

#### Scope

Review findings accumulated during PRs 1a–4b. Findings will be recorded per-PR in the sections above during implementation. This PR addresses all deferred Medium and Low findings.

#### Accumulated Findings

| Source | # | Finding | Severity |
|--------|---|---------|----------|
| PR 1a (#79) | 3 | Multi-step limit inheritance test (2+ steps, partial limits) | Low |
| PR 1a (#79) | 4 | Schema description parity — add `description` to workflow schema step-limit fields | Low |
| PR 1a (#79) | 5 | Document zero-value behavior in workflow schema `max_llm_calls` description | Low |
| PR 1b (#81) | F-02 | `int` → `int32` truncation without overflow guard in `executor.go` L154–156 | Low |
| PR 1b (#81) | F-04 | Negative agent-level limit values in `resolveStepLimits()` silently ignored; add rejection or warning | Low |
| PR 1b (#81) | F-05 | Redundant `TestExecuteTask_PopulatesTaskConfig_AllFields` — consolidate with existing all-fields test | Nit |
| PR 1c (#83) | M1 | `ValueError` vs `TaskOutput(FAILED)` inconsistency for negative limits | Medium |
| PR 1c (#83) | M2 | `DEFAULT_TIMEOUT_SECONDS` defined but unused — wire up or add forward-declaration comment | Medium |
| PR 1c (#83) | L2 | No loop-exhaustion test for new default (5 iterations) | Low |
| PR 2 (#84) | S6 | Clarifying comment for transport margin in retry timeout invariant | Low |
| PR 2 (#84) | S7 | Test independent token budget cutoff (time allows, tokens block) | Low |
| PR 2 (#84) | S9 | Backoff-aware budget check before retry sleep | Low |
| PR 2 (#84) | S10 | Upper-bound validation for `TimeoutSeconds` (e.g., 3600s cap) | Low |
| PR 2 (#84) | S11 | Extract `--deadline-mode` inference into testable function | Low |
| PR 2 (#84) | S12 | Test `MaxTokens = 0` with derived mode + token parser | Low |
| PR 3a (#85) | C1 | Missing CHANGELOG entry for TokenCounter + BudgetEnforcer | Low |
| PR 3a (#85) | C2 | `pause_and_alert` warning log not verified in test — use zaptest | Low |
| PR 3a (#85) | C3 | No Debug log for unknown model in `EstimateCost` | Low |
| PR 3a (#85) | C4 | Non-atomic multi-scope snapshot in `CheckBudget` (TOCTOU tightening) | Low |
| PR 3a (#85) | C5 | Explicit `ResetDaily` agent-scope assertion in test | Low |
| PR 3a (#85) | C6 | Test concurrent `CheckBudget` + `RecordUsage` under `-race` | Low |
| PR 3a (#85) | C7 | Config validation for budget thresholds (`MaxDailyUSD >= 0`, `OnExceed` enum) | Low |
| PR 3b (#86) | S-01 | No parallel budget overspend test (N parallel steps exceed budget collectively) | Low |
| PR 3b (#86) | S-02 | No concurrent `CheckBudget` + `RecordUsage` race test (C6 still unaddressed) | Low |
| PR 3b (#86) | S-03 | `ResetDaily` TODO missing tracking reference — add RFC 0006 PR 5 ref | Low |
| PR 3b (#86) | N-01 | Atomic snapshot for multi-scope `CheckBudget` (pre-existing C4) | Low |
| PR 3b (#86) | N-02 | Config validation for budget thresholds (pre-existing C7) | Low |
| PR 3b (#86) | N-03 | Structured `BudgetError` type for budget rejection (enables 429 responses) | Low |
| PR 4a (#87) | M-01 | Cost estimation inconsistency: `buildStepMetadata` returns $0 when agent only reports `tokens_used` | Medium |
| PR 4a (#87) | M-02 | No deep copy of Metadata pointer on write in `UpdateStepState` (asymmetric with `CreateRun`) | Low |
| PR 4a (#87) | M-03 | Empty step ID in `buildStepMetadata` warning logs makes them undiagnosable | Low |
| PR 4a (#87) | M-04 | Missing write-isolation test for metadata in `state_test.go` | Low |
| PR 4a (#87) | N-01 | Extract shared `resolveStepTokenData` helper to eliminate duplicated metadata parsing | Low |
| PR 4a (#87) | N-02 | Add `WallTimeMs` accuracy test with injected delay | Low |

#### PR checklist

- [ ] All deferred Medium findings addressed
- [ ] All deferred Low findings addressed (or explicitly deferred to next RFC)
- [ ] `make test` passes
- [ ] `make lint` passes
- [ ] `make validate` passes

---

### PR 6: `feature/v02-rfc0006-close` — RFC Close

**Depends on**: PR 5 merged
**Branch**: `feature/v02-rfc0006-close`
**Estimated size**: ~50–100 lines (status updates only)

#### Scope

| File | Change |
|------|--------|
| `docs/rfcs/0006-efficiency-execution-limits.md` | Status → `✅ Implemented` |
| `docs/rfcs/0006-pr-plan.md` | Final checklist verification |
| `ROADMAP.md` | RFC 0006 status → `✅ Implemented`, component status updates, merged PR count |

#### PR checklist

- [ ] RFC 0006 status is `✅ Implemented`
- [ ] ROADMAP.md RFC Tracker updated
- [ ] ROADMAP.md Component Status tables updated (`internal/cost/` → Complete, `internal/defaults/` → Complete)
- [ ] All PR plan checklists are complete
- [ ] `make test` passes
- [ ] `make lint` passes

---

## Size Summary

| PR | Phase | Naive estimate | Calibrated (1.7×) | Status |
|----|-------|----------------|--------------------|--------|
| 1a | 1 | ~200–270 lines | ~340–460 lines | ✅ Merged (PR #79) |
| 1b | 1 | ~200–300 lines | ~340–510 lines | ✅ Merged (PR #81) |
| 1c | 1 | ~130–200 lines | ~220–340 lines | ✅ Merged (PR #83) |
| 2 | 2 | ~200–300 lines | ~340–510 lines | ✅ Merged (PR #84) |
| 3a | 3 | ~200–300 lines | ~340–510 lines | ✅ Merged (PR #85) |
| 3b | 3 | ~180–260 lines | ~310–440 lines | ✅ Merged (PR #86) |
| 4a | 4 | ~180–260 lines | ~310–440 lines | ✅ Merged (PR #87) |
| 4b | 4 | ~200–300 lines | ~340–510 lines | 🟡 In review |
| 5 | Follow-up | TBD | TBD | Not started |
| 6 | Close | ~50–100 lines | ~50–100 lines | Not started |
| **Total** | | **~1,540–2,290** | **~2,630–3,820** | |

---

## Files Touched Summary

| File | PRs |
|------|-----|
| `internal/defaults/defaults.go` (new) | 1a |
| `internal/planner/planner.go` | 1a |
| `internal/executor/executor.go` | 1b, 2, 4a, 4b |
| `internal/scheduler/scheduler.go` | 1b, 3b |
| `internal/cost/cost.go` | 3a |
| `internal/cost/config.go` (new) | 3a |
| `internal/cost/reporter.go` (new) | 3b |
| `internal/cost/cache.go` (new) | 4b |
| `internal/state/state.go` | 4a |
| `internal/server/workflow_handlers.go` | 4a |
| `internal/server/cost_handlers.go` (new) | 4b |
| `cmd/orchestrator/main.go` | 2, 3b |
| `agents/defaults.py` (new) | 1c |
| `agents/base.py` | 1c |
| `schemas/workflow.schema.json` | 1a, 4b |
| `schemas/agent.schema.json` | 1a |
| `config/environments/development.yaml` | 2 |
