---
id: ISSUE-0157
summary: "Persona model calls carry no workflow id, and the wallet keeps a per-workflow total under the empty string like any other id. So every persona call (compose turns, salience bids, critiques, close summaries, and any workflow step a persona serves) counts against one shared total, and the per-workflow limit caps all of it together: with the stock $10 limit and three personas, persona spend stops at about $10 per orchestrator process (nothing resets the totals), below the $15 their own $5 limits add up to and with $90 of the $100 global left. A persona-served workflow step bills that shared total, not its workflow's. Also tracked: a workflow's own total is keyed by its definition, not its run, so it sums every run"
status: open
severity: medium
area: cost
created: 2026-09-11
refs:
  - https://github.com/mkhomutov/Persatrix/pull/938
  - docs/rfcs/0023-llm-call-leasing.md
  - docs/rfcs/0006-efficiency-execution-limits.md
  - docs/issues/ISSUE-0063-workflow-step-unleased-llm-spend-uncounted.md
  - docs/issues/ISSUE-0064-persona-as-sub-agent-attribution-gap.md
  - docs/guides/persona-agents.md
  - docs/observability.md
  - config/optimization.yaml
  - proto/wallet.proto
  - internal/cost/cost.go
  - internal/cost/reporter.go
  - internal/wallet/wallet.go
  - internal/scheduler/scheduler.go
  - internal/scheduler/stage_runner.go
  - cmd/orchestrator/main.go
  - agents/llm_client.py
  - agents/wallet_client.py
  - agents/persona_runtime/action_loop.py
  - agents/persona_runtime/wallet_cause.py
  - agents/salience_bid.py
  - agents/sub_agents/spawner.py
---

## Summary

Before a persona calls a model, it asks the orchestrator's wallet for a
lease, and the wallet charges the call to three running totals: the fleet's,
the agent's and the workflow's. Each total has a limit. Persona calls belong
to no workflow, and they show it by sending an empty workflow id. The wallet
does not read "empty" as "none". It keeps a workflow total under the empty
string like any other id, so every persona call adds to one shared
"workflow" total, and the per-workflow limit applies to it.

The stock limits are $100 for the fleet, $10 per workflow and $5 per agent.
With the stock roster's three personas, the shared total is what stops them:
every persona call is refused once they have spent about $10 together, below
the $15 their own limits add up to and with $90 of the fleet's $100 left.
Nothing resets the totals while the orchestrator runs, so that is $10 per
orchestrator process, not per day. The refusal names the per-workflow budget
though no workflow ran, and no endpoint shows the shared total.

A persona that serves a workflow step sends the empty id too. The step's
calls then count against the shared persona total, and the workflow's own
total — the one its limit and the scheduler's pre-dispatch check read —
never sees them.

One neighbouring gap is tracked here too: a workflow's own total is keyed by
the workflow's definition, not by its run, so it sums every run of that
workflow for the life of the process. RFC 0006 specified a total per run.

## Context

Line numbers are at `bfb8ed1f`.

### Gap 1 — calls with no workflow id share one workflow total (medium)

**The persona side.** `LLMClient.create_message` takes a `workflow_id` that
defaults to `""` (`agents/llm_client.py:196`) and passes it to the wallet
lease as is (`:248`); `WalletClient.lease` puts it on the `LeaseRequest`
unchanged (`agents/wallet_client.py:239`, `:258`). Only the task path sets
it: `BaseAgent` sends `task.workflow_id` (`agents/base.py:426`), filled from
the orchestrator's request (`agents/server_servicers.py:81`). Every persona
call that takes a lease passes a cause, an agent id and an interaction id,
and no workflow id:

| Call | Where |
|---|---|
| Compose turn: channel reply, chat turn, tick or task | `agents/persona_runtime/action_loop.py:387`–`398` |
| Salience bid | `agents/salience_bid.py:400`–`426` |
| Reflexion critic and rewrite | `agents/persona_runtime/reflexion.py:322`–`327`, `:373`–`378` |
| Close summary | `agents/persona_runtime/summarize_close.py:245`–`249`, `:269`–`281` |

The two memory-compression calls (`agents/memory/working.py:208`,
`agents/memory/episodic_retention.py:126`) pass no cause, so they take no
lease and reach no total; ISSUE-0063's closure records the first as an open
residual.

**The orchestrator side.** `AcquireLease` checks the budget under the
request's workflow id (`internal/wallet/wallet.go:215`) and records the
provisional charge under it (`:252`–`258`). `addToScopesLocked` creates a
workflow entry for any id on first use, the empty string included
(`internal/cost/cost.go:82`–`90`). The provisional charge (`:147`), settle
and release through `Reconcile` (`:188`), and the reaper's settle at the
granted amount (`wallet.go:475`) all go through it, so the empty-id total
holds real persona spend, not just estimates. `CheckBudget` reads the total
back (`cost.go:265`–`275`, `:386`) and applies
`per_workflow.default_max_usd` to it with no case for an empty id
(`:417`–`434`), after the global check (`:388`–`415`) and before the
per-agent one (`:436`–`453`).

**The limits.** `max_daily_usd: 100`, `per_workflow.default_max_usd: 10` and
`per_agent.default_max_usd: 5` (`config/optimization.yaml:118`–`126`); every
`config/demo/*/optimization.yaml` carries the same three. The "daily" totals
never reset: nothing outside tests calls `ResetDaily`
(`internal/cost/cost.go:285`), and the orchestrator's wiring for it is still
a `TODO(v0.2)` (`cmd/orchestrator/main.go:240`–`244`).

**What it adds up to.** One persona runs into its own $5 limit first. Two
personas run out of the shared $10 and their own $5 limits at about the same
point. From three on, the shared total is what stops the fleet, and the
stock `config/agents.yaml` has three (`ember-owl`, `iron-fox`,
`nova-sparrow`). A throwaway test drove the real `WalletService` with the
stock limits: three personas took turns, each call estimated and settled at
4 000 input and 1 000 output tokens of a model priced at $3 and $15 per
million. The wallet granted 368 calls and refused the next one with
`per_workflow budget exceeded: spent=9.936000, limit=10.000000,
estimated=0.075000`. Each persona had spent $3.29–$3.32, and the fleet
$9.94 of $100. A lease for a real workflow was granted at the same moment.

**Workflow steps a persona serves.** A workflow step can name a persona;
ISSUE-0063 found that nothing checks a step's agent type.
`PersonaAgent.handle` wraps the task as a `TASK_ASSIGNED` event
(`agents/persona.py:128`), and `cause_for_event` leases that as
`CAUSE_WORKFLOW_TASK` (`agents/persona_runtime/wallet_cause.py:61`–`62`),
the fix ISSUE-0063 closed with. The task carries its `workflow_id`, but the
action loop never passes it on, so the step bills the shared persona total.
The workflow's own total never sees the spend: the scheduler's pre-dispatch
check reads that total (`internal/scheduler/stage_runner.go:165`), and the
scheduler no longer records step spend itself
(`internal/scheduler/budget.go:143`–`153`). In the throwaway test, a
workflow-task lease with no workflow id left the workflow's total unchanged.
No shipped workflow names a persona (`workflows/feature-builder.yaml` and
`workflows/budget-test.yaml` use task agents only), so this part is latent.

The sub-agent spawner has a sibling default: a delegation with no workflow
id gets the id `delegation` (`agents/sub_agents/spawner.py:114`), so a
task-agent child would bill a second shared total of the same kind. The
spawner has no production caller yet
([ISSUE-0064](ISSUE-0064-persona-as-sub-agent-attribution-gap.md)).

**Nothing says it is intended.** RFC 0023 expected the empty id: the proto
comment reads "empty for chat / TICK" (`proto/wallet.proto:24`,
[RFC 0023 §C](../rfcs/0023-llm-call-leasing.md#c-proto-surface)). It also
charges every lease to all three scopes
([§B](../rfcs/0023-llm-call-leasing.md#b-lease-lifecycle)) and reuses the
existing counters ([Non-Goals](../rfcs/0023-llm-call-leasing.md#non-goals)).
It never says what the per-workflow scope does with an empty id. The nearest
it comes is open question 4, which notes that all causes share the same
limits and defers per-cause caps
([Open Questions](../rfcs/0023-llm-call-leasing.md#open-questions)). Nothing
in `internal/cost`, the budget block of `config/optimization.yaml`, the
[observability guide's wallet section](../observability.md#107-wallet-lease-lifecycle-rfc-0023)
or the persona guide's
[USD budgets](../guides/persona-agents.md#usd-budgets) section mentions it;
that last section still describes only the scheduler's pre-dispatch check.
The manual tests reached the chat and tick refusals by tightening
`per_agent` and left `per_workflow` at $10
([MT-COST-003](../manual-tests/MT-COST-003.md),
[MT-COST-004](../manual-tests/MT-COST-004.md)), and no unit test pins an
empty-id total either way.

**Nothing shows it.** `GET /api/v1/cost/summary` returns the fleet's total
and each agent's (`internal/cost/reporter.go:159`, served by
`internal/server/cost_handlers.go:16`). `WorkflowSummary` (`reporter.go:134`)
has no caller outside tests. The shared total appears only in the refusal
and in the wallet's `lease denied` warning, whose `workflow_id` field is
empty (`wallet.go:218`–`226`).

### Gap 2 — a workflow's total sums every run (low)

RFC 0006 §C specified running totals "per workflow run"
([RFC 0006](../rfcs/0006-efficiency-execution-limits.md#c-budget-enforcement)).
The code keys them by the workflow's definition. A `WorkflowRun` holds both a
run `ID` and a `WorkflowID` (`internal/state/state.go:54`–`55`). The
scheduler loads the workflow file by `run.WorkflowID`
(`internal/scheduler/scheduler.go:285`) and passes that same id down as each
step's workflow id (`:369`, then `stage_runner.go:43`, `:165`, `:218`),
which the agent's lease then carries. With no reset, every run of a workflow
adds to one total for the life of the process. Once a workflow's runs have
spent $10 together, the pre-dispatch check fails every later run at its
first step, citing spend the new run never made. Low: it fails closed, a
restart clears it, and it takes a priced workflow run many times.

## Impact

- **Persona traffic stops early, all at once.** In a priced deployment with
  three or more personas, every persona call is refused after about $10 of
  persona spend per orchestrator process: a chat reply carries the refusal
  as its text, ticks go idle (`idle_reason=budget_denied`), and a channel
  reply is replaced by an error notice
  ([failure surface](../observability.md#107-wallet-lease-lifecycle-rfc-0023)).
  Since the personas share one total, they all stop together.
- **The wrong knob.** The limit that stops persona traffic is the one
  operators set for workflows. Lowering it to rein in a workflow also
  throttles every persona; raising it for the personas loosens every
  workflow. The refusal says `per_workflow` on a chat turn, and nothing
  shows the shared total filling.
- **A workflow's limit misses the steps personas serve** (latent: no shipped
  workflow uses one).
- **Unpriced models never bind.** With `mock` or `ollama` every total stays
  at $0.
- **Why medium.** The review process gives medium to "wrong behaviour on a
  real path, or a gate that cannot catch what it claims"
  ([review process](../methodology/review-process.md#severity)). Every priced
  deployment with three or more personas takes the first path, and the
  per-workflow limit is the second for persona-served steps. It is not high:
  it fails closed (spend stops early, and every call still meets some
  limit), the limit is a config value, and a restart clears it.

**Workaround until a fix lands.** Raise
`cost.budgets.per_workflow.default_max_usd`, which loosens every workflow
too, or set it to `0`, which turns the per-workflow check off for workflows
as well (`cost.go:419`; the loader accepts any value from `0` up,
`internal/cost/config.go:104`–`105`). The per-agent and global limits still
apply. Restarting the orchestrator clears every total, including the ones
meant to hold.

## Proposed fix / investigation path

The candidates are not ranked here; the choice belongs to slotting, and some
combine.

1. **No workflow, no workflow check.** Skip the per-workflow scope, both the
   check and the charge, when the id is empty (`cost.go:417` and
   `addToScopesLocked`). Persona traffic then meets its per-agent limits and
   the global one. Open point: nothing then caps all persona spend below the
   global; with the stock roster the per-agent limits add up to $15.
2. **A scope of its own.** Give calls with no workflow their own limit: one
   "no workflow" total, or one per cause, the design space RFC 0023's open
   question 4 reserved. It needs a config key, a `LeaseDenied.scope` value
   and docs. The workflow limit then governs workflows only.
3. **Pass the workflow id where there is one.** Thread `task.workflow_id`
   from a `TASK_ASSIGNED` event into the action loop's lease and the calls
   it gates (bid, critic, rewrite), so a persona serving a step bills that
   workflow; drop the spawner's `delegation` default or pass the parent's
   id. This fixes step billing whichever of 1, 2 or 4 lands.
4. **Document it and show it.** Keep the behaviour, and say so in
   `config/optimization.yaml`, the persona guide's USD budgets section
   (which needs rewriting for RFC 0023 anyway), RFC 0023 and the
   observability guide; add the per-workflow totals, the empty one included,
   to `GET /api/v1/cost/summary`. The coupling stays.
5. **Count per run (gap 2).** Key workflow totals by the run, not the
   definition. The id the agent receives is `TaskRequest.workflow_id`, so
   this either changes what that field carries or adds a run id beside it.
   Wiring the midnight reset alone would bound the sum to a day but still
   pool runs.

Whichever lands:

- write the failing test first — for example, three agents leasing with an
  empty workflow id past $10 and never refused under `per_workflow`
  (candidates 1 and 2), or a persona `TASK_ASSIGNED` lease that carries the
  task's workflow id (3);
- correct what the change makes untrue, including RFC 0023's Decision note
  and, if the proto comment changes, the generated stubs.

**Slot.** This issue is docs only and can merge now. A code fix is outside
v0.3.16's scope as ratified by the
[2026-08-19 sequencing amendment](../v0.3.x-sequencing.md#amendment-2026-08-19--v0315--v0316-attribution-and-audience-before-the-v040-train),
so the next amendment slots it. Under the
[version-train gate](../methodology/process-glossary.md#version-train-gate),
a fix slotted for a later version can be written and reviewed now but merges
only after v0.3.16 is tagged.

## Notes

> 2026-09-11 — filed from a code reading at `604fab72`, confirmed with a
> throwaway `go test -overlay` run against the real `WalletService` (not
> committed). Found in the review of
> [#938](https://github.com/mkhomutov/Persatrix/pull/938), which filed
> [ISSUE-0156](ISSUE-0156-standing-timer-unbounded.md): its Impact section
> names this shared total as what finally stops a runaway convene timer, and
> its F-8 left the defect to an issue of its own. Gap 2 turned up while tracing where a workflow's id comes from.
> RFC 0023's Decision section and ISSUE-0063's notes now link here. Docs
> only: no code changes.
