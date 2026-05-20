---
id: ISSUE-0063
summary: "recordStepUsage's TokenCounter retirement (RFC 0023 PR 3) assumes every workflow-step LLM call is leased — a persona agent serving a workflow step routes through the un-leased persona action loop, so its spend reaches neither the budget counter nor a wallet lease until PR 5 wires those origins."
status: open
severity: medium
area: internal/scheduler
created: 2026-05-19
refs:
  - docs/rfcs/0023-llm-call-leasing.md
  - docs/rfcs/0023-pr-plan.md
---

## Summary

RFC 0023 PR 3 ([#385](https://github.com/mkhomutov/Persatrix/pull/385)) retired
the post-hoc `TokenCounter.RecordUsage` call in
`internal/scheduler/budget.go::recordStepUsage`, on the rationale that the
agent-side wallet now records every workflow-task LLM call's spend on the same
counter (`RecordProvisional` at acquire, `Reconcile` at settle). That holds
**only if every LLM call contributing to a workflow step's reported token total
is leased**. PR 3 leases exactly one path — `BaseAgent._run_llm_loop`, used by
`TaskAgent`. A workflow step dispatched to a persona agent does not use it.

## Context

Found during the PR #385 code review ("Finding 1 — confirm").

- `recordStepUsage` is invoked from `internal/scheduler/stage_runner.go` for
  **every** dispatched step, regardless of agent type. Its input is the step's
  `ExecuteResult.Metadata` token counts, which come from the agent's
  `TaskOutput.metadata`.
- `AgentServiceServicer.ExecuteTask` (`agents/server_servicers.py`) dispatches a
  workflow task by calling `agent.handle(task)` on whichever agent is registered
  for `request.agent_id`.
- For a `TaskAgent`, `handle` → `BaseAgent._run_llm_loop` → `LLMClient.create_message`
  with `cause=CAUSE_WORKFLOW_TASK` → **leased** (PR 3). ✅
- For a persona agent (`_LLMPersonaAgent`), `PersonaAgent.handle`
  (`agents/persona.py:128`, "Backward-compatible: wraps task as a TASK_ASSIGNED
  event") routes through `on_event` → the persona action loop
  (`agents/persona_runtime/action_loop.py:416`). That `create_message` call
  passes **no `cause`** → `CAUSE_UNSPECIFIED` → **un-leased**.

So a persona agent serving a workflow step produces LLM spend that:

1. is **not** recorded by a wallet lease (the action-loop path is un-leased —
   PRs 4–6 scope), and
2. is **no longer** recorded by `recordStepUsage` (PR 3 retired that feed).

The spend escapes the budget `TokenCounter` entirely — a silent
budget-enforcement gap. Persona working-memory compression
(`agents/memory/working.py:183`) is likewise un-leased, but it is reachable only
from persona agents, so it collapses into the same persona-as-workflow-step case.

## Impact

If — and only if — a workflow definition can dispatch a step to a persona agent
in a production deployment, that step's LLM spend is uncounted against all three
budget scopes for the window between PR 3 and PR 5 (`AUTONOMOUS_TICK` /
`SUB_AGENT` origin wiring). `TaskAgent`-only workflows are unaffected: their sole
LLM origin (`_run_llm_loop`) is leased, so the PR 3 retirement is correct for them.

The pre-dispatch `CheckBudget` early-fail still fires, so a *clearly* over-budget
workflow is still rejected up front — but the per-call enforcement RFC 0023 was
written to add does not cover the persona-as-workflow-step path.

## Proposed fix / investigation path

1. **Confirm the trigger.** Determine whether any production workflow definition
   (or `config/agents.yaml` wiring) routes a workflow step to a persona agent.
   If persona agents are never workflow-step targets, this is theoretical — close
   as resolved with that finding recorded, and add a regression assertion that
   workflow steps only dispatch to `TaskAgent`.
2. **If persona agents can serve workflow steps**, the persona action loop's
   workflow-task path needs leasing. `PersonaAgent.handle` knows it is handling a
   `TASK_ASSIGNED` event, so the action loop can pass `cause=CAUSE_WORKFLOW_TASK`
   for that event kind. This is currently unscoped: RFC 0023 PR 3 wired only
   `_run_llm_loop`, and PR 5 covers `AUTONOMOUS_TICK` / `SUB_AGENT`, not the
   persona TASK_ASSIGNED path. Decide whether it belongs in a PR 3 follow-up or
   folds into PR 5's persona action-loop changes.

## Notes

> 2026-05-19 — initial capture during PR #385 review. The PR-3 double-count
> rationale in `docs/rfcs/0023-pr-plan.md` (PR 3 Key-implementation-detail
> bullet) now carries a one-line caveat pointing here.

> 2026-05-19 — investigation step 1 result. **The gap is latent, not active.**
> No shipped workflow definition routes a step to a persona agent:
> `workflows/feature-builder.yaml` and `workflows/budget-test.yaml` reference
> only `planner` / `code-writer` / `code-reviewer`, all `type: "task"` in
> `config/agents.yaml`. So the PR 3 `RecordUsage` retirement is correct for
> every workflow that ships today — keeping the post-hoc record would instead
> re-introduce the double-count it removed.
>
> However, there is **no code-level guard**: `internal/planner/planner.go`'s
> `validate` checks only the step `agent` ID *format* regex; `executeStep`
> (`internal/scheduler/stage_runner.go`), `ExecuteTask`
> (`internal/executor/dispatch.go`) and the Python `AgentServiceServicer.
> ExecuteTask` (`agents/server_servicers.py`) all dispatch by ID with no
> agent-type check. An operator who authors a workflow whose step `agent` is a
> persona ID (e.g. `ember-owl`) would activate the gap. The step-1 suggestion
> to "add a regression assertion that workflow steps only dispatch to
> `TaskAgent`" therefore is **not** a free assertion — it requires *adding* a
> new constraint (planner-side rejection of persona-agent step targets, or
> scheduler-side type enforcement). Folding the leasing fix into PR 5's persona
> action-loop work (path 2) remains the alternative. Decision deferred to the
> follow-up; PR 3 merges with the gap genuinely latent.

> 2026-05-19 — PR #385 review follow-up. Resolution scheduled into **RFC 0023
> PR 5** (`feature/v032-rfc0023-tick-subagent`): `docs/rfcs/0023-pr-plan.md`
> now carries an `action_loop.py` scope row, a Key-implementation-detail
> bullet, and a PR-checklist item for this issue. PR 5 already edits
> `agents/persona_runtime/action_loop.py` for the `AUTONOMOUS_TICK` origin, so
> it is the natural home for whichever resolution path (persona `TASK_ASSIGNED`
> leasing, or the planner/scheduler guard) PR 5 design selects.
