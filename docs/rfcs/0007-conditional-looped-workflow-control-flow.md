# RFC 0007 — Conditional and Looped Workflow Control Flow

**Type**: feature  
**Status**: 📋 Proposed  
**Author**: Maksim Khomutov  
**Date**: 2026-04-15  
**Target**: v0.3.0  
**Depends on**: RFC 0001, RFC 0003, RFC 0006, RFC 0008

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Condition Expression Evaluation](#a-condition-expression-evaluation)
  - [B. Step Skip Semantics](#b-step-skip-semantics)
  - [C. Bounded Loop Construct](#c-bounded-loop-construct)
  - [D. For-Each Expansion](#d-for-each-expansion)
  - [E. Loop Observability and Failure Reporting](#e-loop-observability-and-failure-reporting)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

This RFC adds conditional branching and bounded loop constructs to workflows. The current planner enforces DAG validation and rejects cycles; the scheduler evaluates all steps unconditionally (condition evaluation is explicitly deferred with a `TODO(v0.2)` comment). This RFC activates condition evaluation, adds step skip semantics, and introduces a `repeat_until` loop construct with mandatory exit conditions and resource guardrails.

This RFC depends on RFC 0006 (Efficiency and Execution Limits). Loops compound every existing resource issue — more LLM turns, more tool calls, more retries — so budget enforcement, deadline derivation, and execution metadata must be in place before loops are safe to add.

## Motivation

### Conditional branching is already specified but not implemented

The architecture spec calls for conditional branching and workflow steps with `condition` fields. The planner already parses `condition` fields from YAML, but the scheduler ignores them: `TODO(v0.2): evaluate step conditions` at `scheduler.go` line 362. All steps execute unconditionally regardless of condition expressions.

This means workflows cannot:
- Skip code review for trivial changes
- Branch based on test results
- Choose between fast and thorough analysis paths
- Short-circuit on early success

### Loops are needed for iterative agent patterns

Common agent workflows are inherently iterative:
- Write code → run tests → fix failures → repeat until tests pass
- Draft document → review → revise → repeat until approved
- Generate plan → evaluate feasibility → refine → repeat until viable

Without loops, users must either (a) manually resubmit workflows, (b) encode iteration in a single oversized agent prompt, or (c) build external orchestration around Persatrix. All of these defeat the purpose of a workflow engine.

### Loops without guardrails are dangerous

Adding arbitrary graph cycles to the planner would undermine DAG validation (the cycle detection exists for safety) and create execution paths with unbounded cost. The plan's recommended approach — `repeat_until` with mandatory bounds — preserves DAG semantics while enabling iteration.

## Goals

1. **Condition evaluation**: Evaluate step `condition` expressions against structured outputs from prior steps. Steps whose conditions evaluate to false are skipped with a clear reason in run state.
2. **Bounded repeat-until loops**: A new `repeat_until` workflow construct that re-executes a step group until an exit condition is met, with mandatory `max_iterations`, `max_execution_seconds`, `max_llm_calls_total`, and `max_tokens_total` guards.
3. **For-each expansion**: Iterate over an input collection with bounded concurrency, expanding a step template once per item.
4. **Loop-level observability**: Per-iteration cost, token usage, and exit reason are recorded and surfaced in workflow status.
5. **Validation-time rejection**: Loops without required guardrail fields are rejected during workflow parsing, before any execution begins.

## Non-Goals

- Arbitrary graph cycles in the planner (cycles remain rejected; loops use a structured runtime construct).
- Dynamic workflow modification at runtime (adding/removing steps mid-execution).
- Nested loops (first implementation supports single-level loops only).
- LLM-evaluated conditions (conditions operate on structured data only; no LLM calls for condition evaluation).
- Recursive workflow invocation (a workflow calling itself).

---

## Design / Implementation

### A. Condition Expression Evaluation

Add a condition evaluator to the scheduler that operates on step outputs.

#### Expression language

Conditions use a simple, safe expression syntax that operates on structured step outputs:

```yaml
steps:
  - id: run-tests
    agent: code-writer
    input: "Run the test suite"

  - id: fix-failures
    agent: code-writer
    input: "Fix the failing tests: {{ steps.run-tests.output }}"
    condition: "{{ steps.run-tests.status }} == 'failed'"
```

Supported operators:
- Comparison: `==`, `!=`, `>`, `<`, `>=`, `<=`
- Boolean: `&&`, `||`, `!`
- String: `contains`, `startsWith`, `endsWith`
- Null check: `exists`, `isEmpty`

The evaluator is **not** a general-purpose expression engine. It does not support arithmetic, function calls, or nested expressions beyond simple boolean combinations. This keeps the attack surface small and the semantics predictable.

#### Resolution order

1. Parse the expression into a restricted AST that identifies operators, literals, and template variable placeholders.
2. Resolve template variables (`{{ steps.X.output }}`, `{{ steps.X.status }}`) to typed runtime values.
3. Bind those values into the parsed expression as literals (`string`, `number`, `boolean`, `null`) rather than raw token text.
4. Evaluate the resulting expression.
5. Return boolean result. Non-boolean results are an error.

This is intentionally parameterized evaluation, not string interpolation. Resolved step outputs must never be able to introduce new operators or alter parse structure.

#### Error handling

- Reference to a non-existent step → evaluation error → step fails (not skips).
- Reference to a step that was itself skipped → evaluates as `null` → explicit null check required.
- Malformed expression → parse error → step fails with diagnostic message.

### B. Step Skip Semantics

When a condition evaluates to `false`, the step transitions to a new `Skipped` state:

```go
const (
    StepPending   StepStatus = "pending"
    StepRunning   StepStatus = "running"
    StepCompleted StepStatus = "completed"
    StepFailed    StepStatus = "failed"
    StepSkipped   StepStatus = "skipped"    // NEW
)
```

Skip rules:
1. A skipped step produces no output. References to `{{ steps.skipped_step.output }}` evaluate to `null`.
2. A skipped step does not count against workflow budget or deadline.
3. Downstream steps that depend on a skipped step's output must handle `null` (either via their own conditions or by failing explicitly).
4. A skipped step is logged with the evaluated condition and the resolved values that led to the skip.

### C. Bounded Loop Construct

Loops are modeled as a **runtime construct**, not as DAG cycles. The planner validates loop definitions but does not add back-edges to the execution graph.

#### YAML schema

```yaml
steps:
  - id: iterative-fix
    type: repeat_until
    exit_when: "{{ steps.verify.status }} == 'completed' && {{ steps.verify.output }} contains 'all tests pass'"
    max_iterations: 5
    max_execution_seconds: 600
    max_llm_calls_total: 20
    max_tokens_total: 50000
    on_budget_exit: "fail"  # or "succeed_partial" or "pause"
    steps:
      - id: fix
        agent: code-writer
        input: "Fix failures: {{ loop.previous_output }}"
      - id: verify
        agent: code-reviewer
        input: "Verify the fix: {{ steps.fix.output }}"
```

#### Required guardrail fields

Every `repeat_until` block **must** define:

| Field | Type | Description |
|-------|------|-------------|
| `exit_when` | string (expression) | Condition evaluated after each iteration. Loop exits when true. |
| `max_iterations` | int (> 0) | Hard cap on iteration count. |
| `max_execution_seconds` | int (> 0) | Wall-clock deadline for the entire loop. |
| `max_llm_calls_total` | int (> 0) | Cumulative LLM call cap across all iterations. |
| `max_tokens_total` | int (> 0) | Cumulative token cap across all iterations. |
| `on_budget_exit` | enum | Behavior when loop exits via budget exhaustion instead of `exit_when`. |

Missing any required field → validation error at parse time. The workflow is rejected before execution.

#### Runtime behavior

1. Before each iteration, the scheduler checks all loop-level budgets (iterations, seconds, LLM calls, tokens).
2. If any budget is exhausted, the loop exits with the `on_budget_exit` behavior.
3. Inner steps execute sequentially within each iteration.
4. `{{ loop.previous_output }}` provides the last iteration's final step output.
5. `{{ loop.iteration }}` provides the current 1-based iteration number.
6. After loop exit, `{{ steps.iterative-fix.output }}` contains the final iteration's output.
7. Loop-level token/call counts are deducted from the parent workflow's budget (RFC 0006 integration).

### D. For-Each Expansion

For-each iterates a step template over an input collection:

```yaml
steps:
  - id: review-files
    type: for_each
    collection: "{{ steps.list-files.output }}"
    max_concurrency: 3
    max_items: 50
    as: "file"
    step:
      id: review-single
      agent: code-reviewer
      input: "Review {{ file }}"
```

- `collection` must resolve to a JSON array.
- `max_items` caps the expansion to prevent unbounded fan-out.
- `max_concurrency` limits parallel execution.
- Each expanded step gets its own budget allocation (inherited from workflow or configured per-item).
- For-each is a bounded parallel workflow construct, not a DAG cycle. It still requires scheduler support for controlled expansion, concurrency limiting, per-item budget accounting, and aggregated result handling.

#### Variable scoping

The `as` field names the loop variable. Inside the step template, the variable is referenced directly by the `as` name: `{{ file }}`, not `{{ item.file }}`. If the collection contains objects, their properties are accessed via the named variable: `{{ file.path }}`, `{{ file.size }}`. There is no implicit `item` namespace.

### E. Loop Observability and Failure Reporting

Each loop records per-iteration metadata:

```go
type LoopExecutionMetadata struct {
    IterationsCompleted int               `json:"iterations_completed"`
    ExitReason          LoopExitReason    `json:"exit_reason"`  // "condition_met" | "max_iterations" | "budget_exhausted" | "deadline_exceeded" | "error"
    TotalTokensUsed     int               `json:"total_tokens_used"`
    TotalLLMCalls       int               `json:"total_llm_calls"`
    TotalWallTime       time.Duration     `json:"total_wall_time_ms"`
    PerIteration        []IterationMetadata `json:"per_iteration"`
}
```

Exit reasons are explicit and always surfaced in the workflow status response, so users understand *why* a loop stopped — whether by success, budget, deadline, or error.

---

## Security Considerations

- **Expression injection**: The condition evaluator must not evaluate arbitrary code. The restricted expression language (comparison + boolean + string operators only) limits the attack surface, but only if template values are bound as typed literals after parsing. Raw string substitution before parsing would allow step output text to inject operators or alter boolean structure.
- **Resource exhaustion via loops**: Mandatory guardrail fields prevent unbounded loops. Validation-time rejection ensures no loop runs without guards.
- **Budget bypass via loop nesting**: Nested loops are explicitly disallowed in v1. If added later, inner loop budgets must be subtracted from outer loop budgets.
- **For-each amplification**: A malicious `collection` input could contain millions of items. The `max_items` cap prevents this. Default `max_items` should be conservative (e.g., 100).
- **Condition evaluation timing**: Conditions are evaluated synchronously in the scheduler's main loop. Complex or slow condition evaluation could block scheduling. The restricted expression language keeps evaluation fast (O(1) per condition).

---

## Phased Implementation Plan

### Phase 1: Condition Evaluator and Skip Semantics

**Summary**: Implement condition evaluation and step skipping. This is the foundation for loop exit logic.

**Deliverables**:
1. Condition expression parser (template resolution + operator evaluation).
2. `StepSkipped` status in state store.
3. Scheduler evaluates `condition` field before dispatching each step.
4. Skipped steps recorded with evaluated condition and resolved values.
5. Downstream step handling of `null` outputs from skipped steps.
6. Workflow status response includes skip reasons.

**Dependencies**: RFC 0006 Phase 1 (limit propagation ensures step configs are available for condition-aware dispatch).

### Phase 2: Repeat-Until Loop Construct

**Summary**: Add bounded loop support with mandatory guardrails.

**Deliverables**:
1. Planner validates `repeat_until` blocks (schema enforcement of required guardrail fields).
2. Scheduler loop executor: iterate inner steps, evaluate `exit_when` after each iteration, check budgets.
3. `{{ loop.previous_output }}` and `{{ loop.iteration }}` template variables.
4. Loop-level budget integration with RFC 0006 cost tracking.
5. `LoopExecutionMetadata` in workflow state and status responses.
6. `on_budget_exit` behavior (fail / succeed_partial / pause).

For v1 implementations, `fail` should remain the default until `pause` has an operator-visible resume path.

**Dependencies**: Phase 1 (condition evaluator provides `exit_when` evaluation). RFC 0006 Phase 3 (budget enforcement provides loop-level cost tracking).

### Phase 3: For-Each Expansion

**Summary**: Add collection iteration with bounded concurrency.

**Deliverables**:
1. Planner validates `for_each` blocks (collection reference, max_items, max_concurrency).
2. Scheduler expands for-each into parallel step instances.
3. Per-item budget allocation.
4. Aggregated results in parent step output.

**Dependencies**: Phase 1 (expanded steps may have conditions). RFC 0006 Phase 3 (per-item budget tracking).

---

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Go orchestrator | `internal/planner/planner.go` | Parse `repeat_until` and `for_each` blocks, validate guardrail fields |
| Go orchestrator | `internal/planner/planner_test.go` | Validation tests for loops and conditions |
| Go orchestrator | `internal/scheduler/scheduler.go` | Condition evaluation, loop executor, for-each expansion |
| Go orchestrator | `internal/scheduler/condition.go` (new) | Expression parser and evaluator |
| Go orchestrator | `internal/scheduler/loop.go` (new) | Loop runtime with budget tracking |
| Go orchestrator | `internal/state/state.go` | `StepSkipped` status, `LoopExecutionMetadata` |
| Go orchestrator | `internal/server/handlers.go` | Skip/loop metadata in status responses |
| Schemas | `schemas/workflow.schema.json` | `repeat_until`, `for_each`, condition, guardrail field definitions |
| Workflows | `workflows/` | Example workflows with conditions and loops |
| Config | — | No config changes needed |

---

## Test Strategy

- **Expression parser tests**: Valid expressions, invalid expressions, edge cases (null references, type mismatches, nested booleans).
- **Skip semantics tests**: Step skip, downstream null handling, skip reason recording.
- **Loop tests**: Normal exit (condition met), max_iterations exit, budget exit, deadline exit, error during iteration.
- **For-each tests**: Empty collection, single item, max_items cap, max_concurrency limit, item-level failure handling.
- **Integration tests**: End-to-end workflow with conditions and loops, verifying cost tracking integration with RFC 0006.
- **Validation tests**: Workflows with missing guardrail fields are rejected. Workflows with nested loops are rejected.
- **Schema validation**: Updated `workflow.schema.json` validates against example workflows.

---

## Open Questions

1. **Loop variable scoping**: Should inner step outputs from previous iterations be accessible? (`{{ loop.iterations[0].steps.fix.output }}`). This adds complexity but enables sophisticated iteration patterns.
2. **Parallel inner steps**: Should inner steps within a `repeat_until` support parallel execution (stages), or only sequential? Sequential is simpler and safer for v1.
3. **For-each failure handling**: If one item in a for-each fails, should the entire for-each fail, or continue with remaining items? Both are valid depending on use case.
4. **Condition evaluation caching**: If a condition references an unchanged step output, should the evaluator cache the result? Unlikely to matter for v1 given the simple expression language.
5. **Loop state persistence**: If the orchestrator crashes mid-loop, should it resume from the last completed iteration? Requires checkpointing loop state, which adds significant complexity.
6. **Expression language extensibility**: The v1 expression language supports `contains`, `startsWith`, `endsWith` for strings, but common agent patterns involve checking structured output (e.g., `"status": "pass"` embedded in larger text). Without regex or JSON path support, users must rely on agents producing precise outputs or use brittle substring matching via `contains`. This does not need to be solved in v1 but should be tracked — a `matches` (regex) or `jsonPath` operator will likely be needed once real workflows exercise the condition system.

---

## Decision / Next Steps

1. Review and accept this RFC after RFC 0006 is accepted.
2. Create PR plan once RFC 0006 implementation is underway.
3. Phase 1 (conditions) can begin after RFC 0006 Phase 1 lands.
4. Phase 2 (loops) requires RFC 0006 Phase 3 (budget enforcement).
5. Phase 3 (for-each) can follow independently.

---

## Related Documentation

- [ai-agents-orchestration-spec.md](../ai-agents-orchestration-spec.md) — Workflow DAG and condition spec
- [RFC 0006](0006-efficiency-execution-limits.md) — Prerequisite: execution limits and budget enforcement
- [RFC 0003](0003-scheduler-executor.md) — Scheduler and executor architecture
- [RFC 0001](0001-core-orchestration-pipeline.md) — Planner and DAG validation
