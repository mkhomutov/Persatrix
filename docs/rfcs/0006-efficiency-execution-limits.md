# RFC 0006 — Efficiency and Execution Limits

**Type**: architecture  
**Status**: 📋 Proposed  
**Author**: Engineering Team  
**Date**: 2026-04-15  
**Target**: v0.2  
**Depends on**: RFC 0001, RFC 0003, RFC 0004

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Task Execution Limit Propagation](#a-task-execution-limit-propagation)
  - [B. Conservative Defaults](#b-conservative-defaults)
  - [C. Budget Enforcement](#c-budget-enforcement)
  - [D. Timeout Policy and Deadline Derivation](#d-timeout-policy-and-deadline-derivation)
  - [E. Retry Budget Policy](#e-retry-budget-policy)
  - [F. Response Caching](#f-response-caching)
  - [G. Execution Observability](#g-execution-observability)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

This RFC makes workflow execution predictably bounded, cost-observable, and safe for future control-flow expansion. It replaces permissive or absent defaults with explicit limits, implements budget enforcement that is currently stub-only, replaces fragile timeout coordination with derived deadlines, and adds per-step execution metadata so expensive runs are diagnosable without reading raw logs.

This RFC is a prerequisite for any workflow control-flow expansion (conditional branching, loops) proposed in RFC 0007. Loops and retries magnify every existing cost and timeout weakness; hardening limits first prevents richer control flow from creating runaway spend.

## Motivation

The v0.1 runtime executes workflows end-to-end, but several cost and execution controls are either permissive defaults that do not reflect production intent, or stubs that are not yet enforced.

### Problem 1: Execution limits are not fully propagated

The `TaskConfig` proto defines `max_llm_calls`, `max_tokens`, and `timeout_seconds`, but the orchestrator currently only populates `timeout_seconds` when dispatching tasks (see `executor.go` line 140). Agents fall back to local defaults (10 LLM calls, 4096 tokens in `base.py`) when the orchestrator sends zero values. This means the orchestrator cannot currently control agent resource consumption for individual tasks — agents silently apply their own defaults.

### Problem 2: Cost tracking is a complete stub

`internal/cost/cost.go` contains four TODO comments and no implementation. Budget thresholds are defined in `config/optimization.yaml` (`max_daily_usd: 100`, `per_workflow: 10`, `per_agent: 5`) but are advisory only — nothing reads or enforces them. A single misbehaving workflow can consume the entire daily budget with no guardrails.

### Problem 3: Timeout coordination is fragile

The executor timeout (5 minutes in production, set in `cmd/orchestrator/main.go` line 128) must be manually kept in sync with the largest configured agent timeout (code-writer at 300 seconds in `config/agents.yaml`). This is a guessed coupling maintained by developer knowledge, not by code. If a new agent type is added with a longer timeout, or if workflow steps vary in expected duration, the static executor timeout becomes wrong silently.

### Problem 4: Retries can multiply cost

Executor retry logic (3 retries with exponential backoff) gives each retry attempt its own fresh timeout window. A task that fails near its timeout boundary can consume up to 4× the expected execution time and 4× the expected LLM calls before the system gives up. When provider retries exist at the LLM client layer, the multiplication is worse.

### What happens if we do nothing

These weaknesses are tolerable in manual testing but become genuine risks as:
- More agents and workflow types are added (v0.2 personas, sub-agents)
- Loop constructs are introduced (RFC 0007)
- Autonomous tick loops run continuously (persona agents)
- Multiple workflows run concurrently in production

## Goals

1. **Propagate execution limits end-to-end**: orchestrator populates `max_llm_calls` and `max_tokens` in every `TaskConfig` dispatch, sourced from workflow step config → agent config → conservative system defaults.
2. **Enforce budgets**: implement the `internal/cost/` package so per-workflow, per-agent, and global daily budget checks gate task dispatch — not just logging.
3. **Replace guessed timeouts**: derive RPC deadlines from step-level timeout config with a transport margin, eliminating the static executor timeout constant.
4. **Bound retry cost**: retries consume from the step's deadline and token budget, not from fresh allocations.
5. **Expose execution metadata**: per-step token usage, LLM call count, retry count, and cache hit/miss are recorded in workflow run state and surfaced in status responses.
6. **Lower permissive defaults**: task agent defaults move from 10 LLM calls / 4096 tokens to values justified by observed v0.1 usage patterns.

## Non-Goals

- Conditional branching or loop support (RFC 0007).
- Real-time cost alerting dashboard (future enhancement).
- Provider-level rate limiting (handled by LLM client libraries).
- Token-level streaming budget interruption (requires streaming protocol changes).
- Changes to the persona agent tick loop budget model (already has independent caps in `persona_runtime.py`).

---

## Design / Implementation

### A. Task Execution Limit Propagation

**Current state**: Executor builds `TaskConfig` with only `TimeoutSeconds`. Agent receives zero for `max_llm_calls` and `max_tokens`, falls back to local defaults.

**Change**: The executor resolves limits from a three-level cascade:

```
workflow step config  →  agent config  →  system defaults
(highest priority)       (middle)          (lowest)
```

The scheduler passes resolved limits to the executor as part of `ExecuteRequest`. The executor populates all `TaskConfig` fields before gRPC dispatch.

**Impact on proto**: None for the minimum implementation path — `TaskConfig` already defines the limit fields needed for propagation, and existing response metadata is sufficient to carry `tokens_used` and similar counters. A follow-up proto refinement may still be worthwhile if execution metadata such as `llm_call_count`, `retry_count`, or `cache_hit` needs strongly typed fields instead of a string-keyed metadata map.

### B. Conservative Defaults

| Parameter | Current default | Proposed default | Justification |
|-----------|----------------|------------------|---------------|
| `max_llm_calls` (task agent) | 10 | 5 | Most v0.1 tasks complete in 1–3 LLM calls. 5 provides headroom for tool use without allowing runaway loops. |
| `max_tokens` (task agent) | 4096 | 8192 | 4096 is too low for code generation tasks that include context. 8192 covers typical code review and generation. |
| `max_llm_calls` (persona agent) | 10 | 10 | Persona event handling legitimately requires more turns. No change. |
| `max_tokens` (persona agent) | 4096 | 4096 | Persona runtime already defaults to 4096 output tokens for task execution. Keep the LLM output cap aligned with task agents; persona agents separately manage larger working-memory context windows, which are a different budget. |

Defaults are defined in a single `internal/defaults/` package (Go) and `agents/defaults.py` (Python) to eliminate scattered magic numbers.

### C. Budget Enforcement

Implement `internal/cost/` with three components:

#### TokenCounter

- Accepts step completion events with token counts (from agent response metadata).
- Maintains running totals per workflow run, per agent, and globally (daily).
- Thread-safe: concurrent workflow runs update shared counters atomically.

The metadata plumbing is already mostly present: agents populate `tokens_used` in `TaskOutput.metadata`, the gRPC server serializes that metadata into `TaskResponse.metadata`, and the executor already returns `resp.Metadata` in `ExecuteResult`. The missing work is consumption and normalization on the orchestrator side, not a brand-new end-to-end transport path.

#### BudgetEnforcer

- Called by the scheduler before dispatching each step.
- Checks running totals against thresholds from `config/optimization.yaml`.
- Returns one of: `allow`, `reject` (fail the step), or `pause` (hold for manual review).
- Configurable behavior per threshold level (`on_exceed` field).

#### CostReporter

- Extends the workflow run state with cost metadata.
- Surfaces per-step and per-run cost summaries in `GET /api/v1/workflows/{id}/status`.
- Provides a `GET /api/v1/cost/summary` endpoint for global daily/weekly spend.

### D. Timeout Policy and Deadline Derivation

**Current state**: Static 5-minute executor timeout, manually coordinated with agent timeouts.

**Change**: Replace with a derived deadline model:

```
step_deadline = workflow_step.timeout_seconds
             ?? agent_config.timeout_seconds
             ?? system_default (60s)

rpc_timeout = step_deadline + transport_margin (5s)

workflow_deadline = sum(step_deadlines) × concurrency_factor
                 ?? workflow_config.timeout_seconds
                 ?? system_default (600s)
```

Rules:
1. **Step deadline** is the authoritative maximum for one workflow step. Configured per-step, with fallback to agent config, then system default.
2. **RPC timeout** is computed from step deadline, never independently configured. The 5-second transport margin accounts for gRPC overhead, serialization, and network latency.
3. **Workflow deadline** is the total run budget. For parallel stages, the concurrency factor adjusts for overlapping execution.
4. **Retries share the step deadline**. If a step has a 60-second deadline and the first attempt takes 45 seconds, retries get the remaining 15 seconds, not a fresh 60-second window.
5. The static `WithTimeout(5 * time.Minute)` in `main.go` is removed. The executor's `timeout` field is replaced with a per-dispatch deadline computed from step config.

This intentionally reverses the current executor design, where each dispatch gets a fresh timeout window to maximize infrastructure retry resilience. The tradeoff is deliberate: once workflows can loop and branch, bounded cost and predictable runtime are more important than preserving a full retry window after a near-timeout failure.

### E. Retry Budget Policy

**Current state**: 3 retries with exponential backoff, each retry gets a fresh timeout window. No limit on total tokens consumed across retries.

**Change**:
1. Retries consume from the step deadline (see section D).
2. Retries consume from the step token budget — if the first attempt used 3000 of 8192 tokens, retries get the remaining 5192.
3. A retry is only attempted if sufficient budget (time and tokens) remains for a meaningful attempt. "Meaningful" means at least 25% of the original budget.
4. Provider-level retries (in `llm_client.py`) and executor-level retries are tracked separately. Executor retries are for infrastructure failures (gRPC unavailable); provider retries are for API rate limits. Both count against the step deadline.

### F. Response Caching

**Current state**: `config/optimization.yaml` defines cache settings (`exact: enabled, max_entries: 10000, ttl_seconds: 3600`) but no cache implementation exists.

**Change**: Implement exact-match response caching for deterministic task shapes.

- Cache key: hash of (agent_id, task_type, task_input, model, system_prompt).
- Cache store: in-memory with LRU eviction and configurable TTL.
- Cache is opt-in per task type. Only pure/deterministic tasks should be cached; persona and autonomous tasks are excluded.
- Cache hits are recorded in step metadata for observability.

### G. Execution Observability

Extend `StepState` and `WorkflowRun` with execution metadata:

```go
type StepExecutionMetadata struct {
    TokensUsed       int           `json:"tokens_used"`
    LLMCallCount     int           `json:"llm_call_count"`
    RetryCount       int           `json:"retry_count"`
    CacheHit         bool          `json:"cache_hit"`
    WallTime         time.Duration `json:"wall_time_ms"`
    EstimatedCostUSD float64       `json:"estimated_cost_usd"`
}
```

This metadata is:
- Populated by the executor after each step completes.
- Stored in `StepState` in the state store.
- Returned in workflow status API responses.
- Logged at INFO level for post-hoc analysis.

---

## Security Considerations

- **Budget bypass**: Budget enforcement must not be bypassable by crafting task configs with zero or negative limits. Zero means "use default", negative is rejected.
- **Cache poisoning**: Cache keys must include the full input hash. Cache entries must not be shared across agents with different permission sets.
- **Cost information exposure**: The `/api/v1/cost/summary` endpoint should require authentication (when auth is implemented in a future RFC). Token counts and cost estimates may reveal information about prompt complexity.
- **Denial of budget**: A rogue workflow could intentionally consume the daily budget to block other workflows. The `pause_and_alert` behavior on `on_exceed` mitigates this by requiring manual intervention.

---

## Phased Implementation Plan

### Phase 1: Limit Propagation and Conservative Defaults

**Summary**: Wire execution limits end-to-end and lower permissive defaults.

**Deliverables**:
1. Create `internal/defaults/` package with centralized system defaults.
2. Scheduler resolves step limits (workflow step config → agent config → system defaults) and passes to executor.
3. Executor populates all `TaskConfig` fields (`max_llm_calls`, `max_tokens`, `timeout_seconds`) before dispatch.
4. Agent `handle()` loop validates received limits and rejects negative values.
5. Lower task agent defaults per section B.
6. Add `agents/defaults.py` for Python-side default constants.

**Dependencies**: None.

### Phase 2: Deadline Derivation and Retry Budget

**Summary**: Replace static timeout with derived deadlines and bound retry cost.

**Deliverables**:
1. Remove static executor timeout from `main.go`.
2. Compute per-dispatch RPC timeout from step config + transport margin.
3. Implement shared-deadline retry: retries consume from step deadline, not fresh windows.
4. Add minimum-budget check before retry attempts.
5. Track token consumption across retries.

**Dependencies**: Phase 1 (limit propagation provides the step timeout values).

### Phase 3: Budget Enforcement (Cost Package)

**Summary**: Implement `internal/cost/` with real enforcement.

**Deliverables**:
1. `TokenCounter` — thread-safe running totals per workflow, per agent, global.
2. `BudgetEnforcer` — pre-dispatch budget checks with configurable `on_exceed` behavior.
3. `CostReporter` — cost metadata in workflow state and status responses.
4. Load budget thresholds from `config/optimization.yaml`.
5. Integration with scheduler dispatch loop.

**Dependencies**: Phase 1 (token counts from `TaskConfig` propagation).

### Phase 4: Caching and Observability

**Summary**: Add response caching and per-step execution metadata.

**Deliverables**:
1. Exact-match response cache (in-memory, LRU, TTL).
2. `StepExecutionMetadata` struct in state store.
3. Executor populates metadata after each step.
4. Metadata surfaced in `GET /api/v1/workflows/{id}/status` response.
5. `GET /api/v1/cost/summary` endpoint for global spend.

**Dependencies**: Phase 3 (cost data feeds into metadata).

---

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Go orchestrator | `internal/defaults/defaults.go` (new) | Centralized system default constants |
| Go orchestrator | `internal/executor/executor.go` | Populate full TaskConfig, derived deadlines, retry budget |
| Go orchestrator | `internal/scheduler/scheduler.go` | Resolve step limits, pass to executor, pre-dispatch budget check |
| Go orchestrator | `internal/cost/cost.go` | Implement TokenCounter, BudgetEnforcer, CostReporter |
| Go orchestrator | `internal/state/state.go` | Add StepExecutionMetadata to StepState |
| Go orchestrator | `internal/server/handlers.go` | Cost summary endpoint, metadata in status response |
| Go orchestrator | `cmd/orchestrator/main.go` | Remove static timeout, wire cost package |
| Python agents | `agents/defaults.py` (new) | Centralized Python default constants |
| Python agents | `agents/base.py` | Import defaults, validate received limits |
| Config | `config/optimization.yaml` | No schema changes needed (budget fields already defined) |
| Config | `schemas/agent.schema.json` | Add step-level timeout and limit fields to schema |

---

## Test Strategy

- **Unit tests**: Each new component (`TokenCounter`, `BudgetEnforcer`, `CostReporter`, cache) gets dedicated unit tests with >90% coverage.
- **Limit propagation tests**: Verify the three-level cascade (step → agent → system) resolves correctly for all combinations.
- **Deadline derivation tests**: Verify RPC timeout = step deadline + margin for various configurations.
- **Retry budget tests**: Verify retries stop when insufficient budget remains.
- **Budget enforcement tests**: Verify dispatch is blocked when per-workflow, per-agent, or global budget is exceeded.
- **Integration tests**: End-to-end workflow execution with budget limits, verifying that over-budget steps fail gracefully.
- **Negative value tests**: Verify that negative or absurdly large limit values are rejected or clamped.

---

## Open Questions

1. **Cache invalidation scope**: Should cache TTL be configurable per-agent or only globally? Global is simpler; per-agent adds complexity but allows tuning for different task types.
2. **Budget pause UX**: When `on_exceed: pause_and_alert` triggers, how does the operator resume? CLI command? REST endpoint? This may require a follow-up RFC for operator tooling.
3. **Cost estimation accuracy**: Should estimated cost use prompt token counts (available pre-dispatch) or actual completion token counts (available post-dispatch)? Pre-dispatch enables proactive blocking but is less accurate.
4. **Persona tick loop integration**: Persona agents have their own `_MAX_SUB_AGENT_TOKENS` and `_MAX_SUB_AGENT_LLM_CALLS` caps. Should these be migrated to the centralized defaults system, or kept as persona-specific overrides?

Until operator tooling exists, `fail` should remain the default `on_exceed` behavior for implementation PRs. `pause_and_alert` should only ship together with a defined resume path.

---

## Decision / Next Steps

1. Review and accept this RFC.
2. Create PR plan with sized PRs following the phased implementation plan.
3. Begin Phase 1 implementation (limit propagation + conservative defaults).
4. Phase 2–4 follow in sequence, each building on the prior phase.

---

## Related Documentation

- [ai-agents-orchestration-spec.md](../ai-agents-orchestration-spec.md) — Core MVP specification
- [persatrix-extension-spec.md](../persatrix-extension-spec.md) — Extension spec (budget, cost sections)
- [config/optimization.yaml](../../config/optimization.yaml) — Budget and caching configuration
- [RFC 0007](0007-conditional-looped-workflow-control-flow.md) — Dependent RFC for workflow control flow
