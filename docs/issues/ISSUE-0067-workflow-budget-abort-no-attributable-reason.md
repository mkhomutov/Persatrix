---
# Allowed values are documented in README.md. Comments above fields
# (not inline) so that the front-matter parser does not pick them up.
id: ISSUE-0067
# summary: one-line description, surfaced as the Summary column in INDEX.md
summary: "MT-COST-002: a workflow that exceeds budget aborts without a budget-attributable terminal reason; the scheduler pre-dispatch budget check is an optimistic early-fail, not the enforcement point"
# status: open | in_progress | resolved
status: open
# severity: low | medium | high | critical
severity: low
# area: internal/ package or agent subsystem
area: cost
# created: YYYY-MM-DD when the finding was first captured (validated)
created: 2026-05-22
# refs: documentary only — not surfaced in INDEX, useful for grep
refs:
  - docs/rfcs/0006-efficiency-execution-limits.md
  - docs/rfcs/0023-llm-call-leasing.md
  - docs/manual-tests/MT-COST-002.md
  - docs/manual-tests/v0.3.2-execution-report.md
---

## Summary

`MT-COST-002` ("a workflow exceeding budget is aborted with the expected reason") has carried an
⚠️ Accepted-with-known-gap outcome since v0.3.0 and again through v0.3.3. The workflow *does* abort,
but the terminal `error` references the agent-side LLM token limit
(`"LLM response truncated: max_tokens limit reached"`), **not** a budget scope. The expected
"budget exceeded" reason is not reliably observable, so the MT cannot cleanly verify budget-driven
workflow termination.

## Context

Budget enforcement for workflow-task LLM calls moved to **per-call wallet leases** in
[RFC 0023](../rfcs/0023-llm-call-leasing.md) (§ D / § G). The scheduler's pre-dispatch budget check
([`internal/scheduler/stage_runner.go:140-173`](../../internal/scheduler/stage_runner.go),
`ErrBudgetExceeded` in [`internal/scheduler/budget.go:23`](../../internal/scheduler/budget.go)) is
**still present but is now an *early-fail optimisation*, not the enforcement point** — the in-code
comment states this explicitly: it only rejects a *clearly over-budget* workflow before paying
executor-dispatch + agent-startup cost, and is "no longer load-bearing for cost correctness."

Two consequences observed in the manual runs (the v0.3.3 release-prep MT execution; carry-forward
of the same ⚠️ row in [`v0.3.2-execution-report.md`](../manual-tests/v0.3.2-execution-report.md)
and v0.3.1/v0.3.0):

1. **No budget-attributable terminal reason.** The `budget-test` fixture step fails on its first
   LLM call via agent-side `max_tokens` truncation *before* either the scheduler pre-dispatch
   reject or a wallet per-call budget-denial produces a budget-scoped terminal reason. The workflow
   ends `failed` with a token-limit message; an operator cannot tell a budget abort from an
   ordinary token-limit failure by inspecting workflow terminal state.
2. **The pre-dispatch check is optimistic for parallel steps.** Per the comment at
   `stage_runner.go:151-154`, parallel steps within a stage can each pass the check and collectively
   exceed the budget (overspend bounded by `parallel_steps × max_token_cost`); there is an existing
   `TODO(v0.3): Consider pessimistic budget reservation for high-value workflows`.

This is **not** "budget enforcement is missing" — over-budget LLM calls *are* denied at the wallet
lease boundary (proven by `MT-COST-003`/`MT-COST-004`). The gap is reason-attribution at the
workflow-termination layer plus the optimistic parallel window, which together make `MT-COST-002`
un-cleanly-verifiable.

## Impact

- `MT-COST-002` cannot reach an unambiguous ✅ Pass; it carries ⚠️ Accepted-with-known-gap
  indefinitely.
- Operators reading a `failed` workflow cannot distinguish a budget-driven abort from a token-limit
  or other step failure — both surface generic step-error text.
- The optimistic parallel-step window allows bounded overspend on wide stages of high-cost workflows.

Severity is **low**: cost correctness itself holds (wallet leases enforce per-call budgets); this is
a verifiability + terminal-reason-attribution gap, not a spend leak.

## Proposed fix / investigation path

1. **Surface a budget-attributable terminal reason.** When a workflow step fails because a wallet
   lease was budget-denied (or the scheduler pre-dispatch `ErrBudgetExceeded` fires), propagate a
   distinct terminal status/reason (e.g. `budget_exceeded` with the offending scope) into workflow
   state, rather than collapsing it into a generic step `error`. The REST layer already maps
   `ErrBudgetExceeded` to HTTP 429 (`budget.go:22` note) — extend the same attribution to the
   terminal workflow record.
2. **Rewrite the `MT-COST-002` fixture** to reliably drive the *actual* enforcement point: a step
   whose per-call wallet lease is denied (tight `per_agent`/`per_workflow` cap, model that is priced
   so the estimate is non-zero, and a request that does not trip `max_tokens` first), and assert the
   budget-attributable reason from (1).
3. **(Separate, lower priority)** Evaluate the `stage_runner.go` `TODO(v0.3)` — pessimistic budget
   reservation for parallel steps in high-value workflows — or document the bounded-overspend window
   as accepted behaviour. This likely warrants an RFC 0006 amendment rather than an inline change.

## Notes

> 2026-05-22 — captured during the v0.3.3 release-prep MT execution (`MT-COST-002` ⚠️ carry-forward).
> Corrects an earlier mischaracterisation that pre-flight workflow enforcement was *missing*: it
> exists (`stage_runner.go` pre-dispatch check) but is an optimistic early-fail; the real items are
> terminal-reason attribution, a fixture that exercises the wallet-lease enforcement point, and the
> optimistic parallel-step window.
