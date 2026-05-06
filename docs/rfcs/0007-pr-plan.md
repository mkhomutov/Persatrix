# RFC 0007 — PR Implementation Plan (scaffold)

**RFC**: [0007-conditional-looped-workflow-control-flow.md](0007-conditional-looped-workflow-control-flow.md)
**Created**: 2026-04-25 (retargeted from v0.3.0 to v0.4.0 on 2026-05-06)
**Branch prefix**: `feature/v040-rfc0007-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: pending v0.4.0-plan.md (this scaffold was authored under [v0.3.0-plan.md Phase 1](../v0.3.0-plan.md#phase-1--author-the-six-rfc-pr-plans) before retargeting; it will be re-anchored to the future v0.4.0 master plan when that document opens)

> **Status**: 🔨 Scaffold — PR rows have branch names, scopes, and dependency links pinned, but per-PR key-implementation-detail and tests sections are placeholders. Flesh out before the first implementation PR opens.
>
> **Retarget note (2026-05-06)**: this RFC was originally scoped to v0.3.0. It was retargeted to v0.4.0 because its load-bearing use cases (iterative refinement, branching on child-agent outputs, parallel fan-out) are unlocked by v0.4.0's sub-agent spawning (RFC 0010) and skill registry (RFC 0014), not by v0.3.0's conversation infrastructure. RFC 0008 is the only hard dep and ships fully in v0.3.0 — by v0.4.0-start the prerequisite is satisfied. See [ROADMAP §v0.4.0 Why RFC 0007 lands in v0.4.0](../../ROADMAP.md#v040--agent-organizations) for full rationale.

---

## Overview

RFC 0007 introduces conditional steps, repeat-until loops, and for-each expansion to the workflow language. Full RFC scope ships in v0.4.0.

This plan splits the work into **5 PRs**.

> **Estimate calibration**: 1.7× factor.

**Prerequisites**:
- [RFC 0008 PR plan](0008-pr-plan.md) PR 1 merged (per-step context budget) — required by PR 3 of this plan (loop budget integration).
- RFC 0006 Phase 1 (limit propagation) and Phase 3 (budget enforcement) already shipped in v0.2.0.

**Cross-RFC sequencing**: parallel workstream with [RFC 0011 PR plan](0011-pr-plan.md) once RFC 0008 PR 1 has merged. No blocking dep on RFC 0011 / 0020 / 0021.

---

## Dependency Graph

```
PR 1 (Phase 1 — condition evaluator + StepSkipped status + skip semantics)
  ↓
PR 2 (Phase 2a — repeat_until planner validation + schema)
  ↓
PR 3 (Phase 2b — repeat_until scheduler executor + loop budget integration)
  ↓
PR 4 (Phase 3 — for_each expansion + per-item budget allocation)
  ↓
PR 5 (Review follow-ups + RFC close)
```

---

## PR Sequence

### PR 1: `feature/v040-rfc0007-condition-evaluator` — Phase 1: Condition + Skip

**Depends on**: Nothing (RFC 0006 Phase 1 already shipped).
**Estimated size**: ~400–500 lines.

#### Scope (high-level)

- `internal/scheduler/condition.go` (new) — condition expression parser + evaluator (template resolution + operator evaluation).
- `internal/state/state.go` — `StepSkipped` status.
- `internal/scheduler/scheduler.go` — evaluate `condition` field before dispatching each step.
- Skipped steps recorded with evaluated condition + resolved values.
- Downstream step handling of `null` outputs from skipped steps.
- `internal/server/handlers.go` — workflow status response includes skip reasons.

#### Key implementation details *(TBD)*
#### Tests *(TBD)*

#### PR checklist

- [ ] ROADMAP.md row for RFC 0007 → `🚧 Implementing`
- [ ] Master Progress Overview row 7 → 🔄 In progress

---

### PR 2: `feature/v040-rfc0007-repeat-until-planner` — Phase 2a: repeat_until Planner

**Depends on**: PR 1.
**Estimated size**: ~300–450 lines.

#### Scope (high-level)

- `internal/planner/planner.go` — parse + validate `repeat_until` blocks (mandatory guardrail fields).
- `schemas/workflow.schema.json` — `repeat_until` definition with required `exit_when`, `max_iterations`, `on_budget_exit`.
- `internal/planner/planner_test.go` — validation tests.

#### Key implementation details *(TBD)*
#### Tests *(TBD)*

---

### PR 3: `feature/v040-rfc0007-repeat-until-runtime` — Phase 2b: repeat_until Runtime

**Depends on**: PR 2 + [RFC 0008 PR plan](0008-pr-plan.md) PR 1 (per-step context budget).
**Estimated size**: ~400–500 lines.

#### Scope (high-level)

- `internal/scheduler/loop.go` (new) — loop runtime; iterate inner steps, evaluate `exit_when`, check budgets.
- `{{ loop.previous_output }}` and `{{ loop.iteration }}` template variables.
- Loop-level budget integration with RFC 0006 cost tracking + RFC 0008 per-step packaging.
- `LoopExecutionMetadata` in workflow state + status responses.
- `on_budget_exit` behavior: `fail` / `succeed_partial` / `pause`. **Default `fail`** until `pause` has an operator-visible resume path.

#### Key implementation details *(TBD)*
#### Tests *(TBD)*

#### PR checklist

- [ ] `pause` mode shipped behind a feature flag if implemented; default remains `fail`
- [ ] Loop-budget interaction with RFC 0008's per-step budget verified by integration test

---

### PR 4: `feature/v040-rfc0007-for-each` — Phase 3: for_each Expansion

**Depends on**: PR 3.
**Estimated size**: ~350–500 lines.

#### Scope (high-level)

- `internal/planner/planner.go` — validate `for_each` blocks (collection reference, `max_items`, `max_concurrency`).
- `internal/scheduler/scheduler.go` — expand for-each into parallel step instances.
- Per-item budget allocation.
- Aggregated results in parent step output.
- `workflows/` — example workflows with conditions and loops.

#### Key implementation details *(TBD)*
#### Tests *(TBD)*

---

### PR 5: `feature/v040-rfc0007-close` — Review Follow-Ups + RFC Close

**Depends on**: PR 4.
**Estimated size**: ~150–300 lines.

| File | Change |
|------|--------|
| `docs/rfcs/0007-conditional-looped-workflow-control-flow.md` | Status → `✅ Implemented`. |
| `ROADMAP.md` | RFC 0007 row → `✅ Implemented`. |
| v0.4.0 master plan (TBD) | Mark RFC 0007 workstream row complete. |

CHANGELOG.md is **deferred to v0.4.0 release prep**.

#### PR checklist

- [ ] All deferred review findings addressed or downgraded
- [ ] `make test` passes; `make lint` clean

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| PR 3 loop budget integration disagrees with RFC 0008 per-step budget contract | RFC 0008 PR 1 lands first; this plan's PR 3 cites the concrete RFC 0008 PR number. Contract gaps surface in PR-plan review. |
| `pause` mode under-specified — operators expect resume that does not exist | Default `fail` until resume path ships; `pause` gated behind feature flag. v0.4.0 release notes call out the constraint. |
| `for_each` parallel concurrency interacts badly with RFC 0011 channel cascade depth | `max_concurrency` is per-step; channel cascade is a separate global counter. Documented in workflow guide. |
| Condition expression parser becomes a DSL maintenance burden | Scope to template resolution + simple operators (`==`, `!=`, `<`, `>`, `&&`, `||`); no arbitrary code. |

---

## ROADMAP Hygiene

- **PR 1 opens** → ROADMAP RFC 0007 → `🚧 Implementing`; v0.4.0 master plan workstream row → 🔄.
- **PR 5 merges** → ROADMAP RFC 0007 → `✅ Implemented`; v0.4.0 master plan workstream row → ✅.

---

## Scaffold TODOs

Before opening PR 1:
- [ ] Fill in "Key implementation details" + "Tests" for each PR.
- [ ] Pin estimated sizes against the RFC's Files Touched table; if the 1.7× calibration factor would push the upper bound past the [BRANCHING.md](../BRANCHING.md) 500-line soft cap, split the PR before opening.
- [ ] Decide `pause` mode disposition (ship behind flag vs. defer).
- [ ] Re-anchor the **Master plan** link in this file's header to the v0.4.0 master plan once that document opens.
