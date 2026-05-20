---
id: ISSUE-0064
summary: "RFC 0023 PR 5 wires CAUSE_SUB_AGENT + parent attribution in BaseAgent._run_llm_loop, but a PersonaAgent dispatched as a sub-agent child routes through the persona action loop (cause_for_event) instead, so its leased spend bills the child rather than the delegating parent. Latent today — SPAWN_SUB_AGENT is not_implemented in action_executor.py and SubAgentSpawner has no production caller."
status: resolved
severity: low
area: agents/persona_runtime
created: 2026-05-20
closed: 2026-05-20
refs:
  - docs/rfcs/0023-llm-call-leasing.md
  - docs/rfcs/0023-pr-plan.md
  - docs/issues/ISSUE-0063-workflow-step-unleased-llm-spend-uncounted.md
---

## Summary

RFC 0023 PR 5 ([#388](https://github.com/mkhomutov/Persatrix/pull/388)) attributes
sub-agent LLM spend to the delegating parent persona by:

1. Threading `parent_agent_id` from `SubAgentSpawner` into
   `TaskInputConfig.sub_agent_parent_id` (`agents/sub_agents/spawner.py`).
2. Reading that field in `BaseAgent._run_llm_loop` to flip the lease cause to
   `CAUSE_SUB_AGENT` and the lease `agent_id` to the parent
   (`agents/base.py:407-410`).

The discriminator lives only in `_run_llm_loop`. A `PersonaAgent` child does
not use that path — `PersonaAgent.handle` wraps the task as a `TASK_ASSIGNED`
event and routes through `agents/persona_runtime/action_loop.py`, which derives
the cause via `cause_for_event(event)` and uses `self.agent_id` (the child's
own id) for lease attribution. The `sub_agent_parent_id` field on
`task.config` is never consulted on that path.

This is the same shape as the gap [ISSUE-0063](ISSUE-0063-workflow-step-unleased-llm-spend-uncounted.md)
just closed for the workflow-task origin: `_run_llm_loop` was wired, the
persona action loop was not, and `cause_for_event` had to be taught the new
mapping. The persona-as-sub-agent variant is the residual sibling.

## Context

Found during the PR #388 code review (Finding #2).

- `SubAgentSpawner.dispatch(child, request)` takes any `BaseAgent` as
  `child` (`agents/sub_agents/spawner.py:87-96`).
- `PersonaAgent` extends `BaseAgent` (`agents/persona.py:100`), so a persona
  is type-eligible to be a sub-agent child.
- For a `TaskAgent` child, `handle` → `_run_llm_loop` reads
  `sub_agent_parent_id` and the lease is tagged `CAUSE_SUB_AGENT` /
  parent. ✅
- For a `PersonaAgent` child, `handle` → `_on_event_inner` →
  `cause_for_event(TASK_ASSIGNED)` returns `CAUSE_WORKFLOW_TASK` and the
  lease is acquired against `self.agent_id` (the child). The
  `sub_agent_parent_id` field on `task.config` is silently ignored. ❌

## Impact

If a persona agent is ever dispatched as a sub-agent child in a production
deployment, per-persona cost dashboards attribute the delegated work to the
*child* persona rather than the delegating *parent* persona. The wallet still
records the spend (lease cause is the correct workflow-task value, just not
the sub-agent value), so the *budget enforcement* contract is intact — only
the per-persona *attribution* breaks.

**Latent today.** `SPAWN_SUB_AGENT` returns `{"status": "not_implemented"}`
in `agents/action_executor.py:170-189`; no production code path constructs
a `SubAgentSpawner` and passes a persona as the child. The sole call sites
are integration tests, all of which use `BaseAgent`-derived test fixtures
(`_ScriptedSubAgent`, `_RecordingChild`). The gap activates the day either
(a) `SPAWN_SUB_AGENT` is wired through to the spawner with persona-eligible
children, or (b) a new caller routes a persona through `SubAgentSpawner.dispatch`.

## Proposed fix / investigation path

Two routes — same fork ISSUE-0063 considered:

1. **Persona action-loop branch.** Teach
   `agents/persona_runtime/action_loop.py` to honour
   `task.config.sub_agent_parent_id` on the `TASK_ASSIGNED` path: when set,
   override `cause_for_event`'s `CAUSE_WORKFLOW_TASK` to `CAUSE_SUB_AGENT`
   and substitute the parent's `agent_id`. Symmetric with the
   `BaseAgent._run_llm_loop` override and reuses the same field already
   plumbed by the spawner. The TaskInput → AgentEvent wrap in
   `PersonaAgent.handle` would need to carry `sub_agent_parent_id` (either
   via `event.metadata` or by reading it directly off `self._current_task`
   inside the loop).

2. **Constraint route.** Reject persona-typed agents at the
   `SubAgentSpawner.dispatch` boundary (or earlier, at planner/registry
   validation) so a persona cannot be a sub-agent child. Mirror of the
   ISSUE-0063 "constraint at the boundary" option. Cheaper to implement but
   may be wrong long-term if delegated reasoning to a persona is a planned
   product capability.

The path 1 fix is the same shape as PR 5's existing
`BaseAgent._run_llm_loop` override (~5 lines + a test) and matches how
ISSUE-0063 was ultimately resolved (lease in the action loop rather than
gate at the boundary). Defer to whichever PR re-opens
`agents/persona_runtime/action_loop.py` for related work.

## Notes

> 2026-05-20 — initial capture during PR #388 review. Gap is structural twin
> of ISSUE-0063; both stem from the persona action loop being a parallel
> LLM-call origin to `BaseAgent._run_llm_loop` that needs its own copy of
> any lease-attribution overrides. Future RFC 0023 work that adds a new
> cause should consider both call sites by construction.

## Resolution

> 2026-05-20 — resolved via path 1 (persona action-loop branch). A new free
> function `lease_attribution_for_event(event, *, agent_id)` in
> `agents/persona_runtime/wallet_cause.py` layers the `sub_agent_parent_id`
> override on top of `cause_for_event`: a `TASK_ASSIGNED` event whose
> `event.payload["task"].config.sub_agent_parent_id` is non-empty flips
> the lease cause to `CAUSE_SUB_AGENT` and the lease `agent_id` to the
> parent's id. The action loop calls the new helper instead of
> `cause_for_event` directly. Exact twin of the override RFC 0023 PR 5
> added to `BaseAgent._run_llm_loop` (`agents/base.py:407-410`).
>
> Pinned by `agents/tests/test_action_loop_subagent_lease.py` —
> persona-as-sub-agent dispatch tags `CAUSE_SUB_AGENT` and bills the
> parent; persona workflow-step dispatch without the marker still tags
> `CAUSE_WORKFLOW_TASK` against the persona's own id (ISSUE-0063
> invariant); explicit empty-string marker does not trip the override.
