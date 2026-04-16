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

- [ ] `go test ./internal/defaults/ -v -race` passes
- [ ] `go test ./internal/planner/ -v -race` passes
- [ ] `make validate` passes
- [ ] `internal/defaults/` exports all constants listed in RFC 0006 Section B
- [ ] `Step` struct includes `TimeoutSeconds`, `MaxLLMCalls`, `MaxTokens`, `ContextBudget`
- [ ] Negative limits rejected during parse

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

- [ ] `go test ./internal/executor/ -v -race` passes
- [ ] `go test ./internal/scheduler/ -v -race` passes
- [ ] `go test ./internal/registry/ -v -race` passes
- [ ] `ExecuteRequest` includes `StepLimits`
- [ ] `TaskConfig` fully populated (no zero-value `MaxLlmCalls`/`MaxTokens`)
- [ ] Three-level cascade verified in tests

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

- [ ] `pytest tests/unit/python/ -v` passes
- [ ] `ruff check agents/` clean
- [ ] `mypy agents/` passes
- [ ] `agents/defaults.py` exports all constants listed in RFC 0006 Section B
- [ ] `_run_llm_loop()` rejects negative limits
- [ ] No inline magic numbers remain in `base.py` for `max_llm_calls`/`max_tokens`

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

- [ ] `go test ./internal/executor/ -v -race` passes
- [ ] `go test ./cmd/orchestrator/ -v -race` passes (if applicable)
- [ ] Static `WithTimeout(5*time.Minute)` removed from `main.go`
- [ ] Derived deadline computed from step config + transport margin
- [ ] Retries share step deadline (not fresh windows)
- [ ] Minimum budget check prevents wasteful retries
- [ ] Config flag `execution.deadline_mode` controls behavior

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

- [ ] `go test ./internal/cost/ -v -race` passes
- [ ] `TokenCounter` is thread-safe (race detector clean)
- [ ] `BudgetEnforcer` implements `on_exceed: "fail"` behavior
- [ ] `on_exceed: "pause_and_alert"` degrades to `"fail"` with warning
- [ ] Config loads from `config/optimization.yaml` budget section
- [ ] Pricing table used for cost estimation
- [ ] All TODO comments in `cost.go` replaced with implementation

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

- [ ] `go test ./internal/cost/ -v -race` passes
- [ ] `go test ./internal/scheduler/ -v -race` passes
- [ ] Pre-dispatch budget check wired into scheduler
- [ ] Post-dispatch token recording wired into scheduler
- [ ] `CostReporter` produces workflow and global summaries
- [ ] `main.go` wires cost components into scheduler

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

- [ ] `go test ./internal/state/ -v -race` passes
- [ ] `go test ./internal/executor/ -v -race` passes
- [ ] `go test ./internal/server/ -v -race` passes
- [ ] `StepExecutionMetadata` struct defined with all 6 fields
- [ ] Executor populates metadata after each step
- [ ] Status API includes per-step metadata
- [ ] Metadata logged at INFO level

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
| 1a | 1 | ~200–270 lines | ~340–460 lines | Not started |
| 1b | 1 | ~200–300 lines | ~340–510 lines | Not started |
| 1c | 1 | ~130–200 lines | ~220–340 lines | Not started |
| 2 | 2 | ~200–300 lines | ~340–510 lines | Not started |
| 3a | 3 | ~200–300 lines | ~340–510 lines | Not started |
| 3b | 3 | ~180–260 lines | ~310–440 lines | Not started |
| 4a | 4 | ~180–260 lines | ~310–440 lines | Not started |
| 4b | 4 | ~200–300 lines | ~340–510 lines | Not started |
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
