---
id: ISSUE-0285
summary: "The rule that a sub-agent gets no more than its parent is not built, and a delegation's `budget.timeout_seconds` is carried and never read: the spawner waits for its sub-agent without a time limit. A delegation request with an `allowed_tools` value of the wrong shape raises a bare `TypeError` or is read as a set of letters, and one that names no tool gives its sub-agent every tool the sub-agent lists. Nothing a persona or the orchestrator does reaches the spawner, so all of it is latent."
status: open
severity: low
area: agents/sub_agents
created: 2026-11-26
refs:
  - agents/sub_agents/spawner.py
  - agents/sub_agents/delegation.py
  - agents/tools/permissions.py
  - agents/persona.py
  - agents/task_types.py
  - agents/tools/tool_list.py
  - docs/ai-agents-orchestration-spec.md
  - docs/persatrix-extension-spec.md
  - docs/issues/ISSUE-0214-declared-tool-permissions-are-never-enforced.md
  - docs/issues/ISSUE-0272-other-docs-still-present-delegation-and-permission-checks-as-working.md
  - https://github.com/mkhomutov/Persatrix/pull/1148
---

# ISSUE-0285: The sub-agent permission rule and a delegation's time limit are not built

## Summary

The design says a sub-agent can never have more permissions than the
agent that spawned it ("child ≤ parent"). No code compares the two. A
delegation also carries a time limit, `budget.timeout_seconds`, that
nothing reads.

Both were the third part of
[ISSUE-0214](ISSUE-0214-declared-tool-permissions-are-never-enforced.md).
That issue closed when its first two parts were fixed: a tool's declared
permissions and a task's `allowed_tools` list are now enforced. The
maintainer deferred this part, because nothing reaches the spawner yet.

## Context

**What exists.** `SubAgentSpawner.dispatch`
(`agents/sub_agents/spawner.py`) runs a sub-agent inside the parent's own
process: it builds a task from a `DelegationRequest` and awaits
`child.handle(task)`. Only tests call it. `PersonaAgent.spawn_sub_agent`
forwards to a client that nothing provides, and the `spawn_sub_agent`
action answers `not_implemented`.

**What holds a sub-agent today.** The child is an agent with a config of
its own, and since ISSUE-0214's fix its tool round holds it to three
things: its own `tools` list, its own `permissions` block, and the
delegation's `allowed_tools`, which arrives as the task's list and binds
only when it names a tool (see the table). A task agent also honours the delegation's `budget.max_llm_calls` and
`budget.tokens` (`BaseAgent._run_llm_loop`).

**What does not.**

| Field or rule | State | Where |
|---|---|---|
| Child ≤ parent | Nothing compares the child's permissions with the parent's. The spawner is handed a child agent and knows the parent only by its id. | `agents/sub_agents/spawner.py` (listed there as out of scope), the note at the end of `agents/tools/permissions.py` |
| `budget.timeout_seconds` | Checked to be zero or more, written into the request JSON, and never read. `dispatch` awaits the child with no time limit. | `agents/sub_agents/delegation.py`, `agents/sub_agents/spawner.py` |
| Depth and concurrency limits, the parent's budget pool | Not built. | the same docstrings |
| A delegation that names no tool | Not decided. `DelegationRequest.allowed_tools` defaults to an empty set, and an empty list restricts nothing, so a delegation that names no tool gives the child every tool on its own `tools` list. For a task from the orchestrator the wire forces that reading, since it cannot tell an empty list from an unset one. For a delegation built in Python nobody chose it; the other reading is that such a delegation allows no tool. | `agents/sub_agents/delegation.py`, `agents/sub_agents/spawner.py`, `task_allows` in `agents/tools/tool_list.py` |
| `allowed_tools` of the wrong shape in a request | `DelegationRequest.from_context_value` builds a set from whatever the JSON holds. A bare text (`"file_read"`) becomes a set of its letters, so the sub-agent is refused every tool whose name is longer than one letter. A list inside the list raises `TypeError: unhashable type`, not the contract's own error. A list that mixes text and numbers raises `TypeError` from `sorted` when the spawner writes the request. | `agents/sub_agents/delegation.py`, `agents/sub_agents/spawner.py` |

The last row was read while fixing the same kind of fault in a result's
`tags`, and run on 2026-11-26: the three shapes behave as the row says.
The row before it was run the same day: a delegation left at its
default offered the child both of its tools, and both ran.

## Impact

Proposed low. Nothing is reachable: no persona, workflow or REST route
leads to the spawner. The risk is the day something does. A sub-agent
would then run under its own config, which may grant more than its
parent's, and a sub-agent that never answers would hold its parent's
turn for as long as the parent's own limit allows. The severity is
proposed here, not ruled.

## Proposed fix / investigation path

Before anything calls the spawner:

1. Decide what "no more than its parent" means when the child has a
   config of its own: refuse a child whose `permissions` block grants
   something the parent's does not, or run the child under the
   intersection of the two.
2. Wrap `child.handle(task)` in the delegation's time limit, with zero
   meaning no limit of its own, as the field's docstring says.
3. Check the shape of `allowed_tools` where the request is read and where
   it is sent, and raise `DelegationContractError`.
4. Decide what a delegation that names no tool means: every tool the
   child lists, as today, or none.
5. Or delete the fields that stay unbuilt, so the contract says only what
   holds.

The docs that still present delegation as working are listed in
[ISSUE-0272](ISSUE-0272-other-docs-still-present-delegation-and-permission-checks-as-working.md).

## Notes

> 2026-11-26 — filed by
> [#1148](https://github.com/mkhomutov/Persatrix/pull/1148), which
> resolves ISSUE-0214. In that pull request the two docstrings that
> said the rule was enforced (`agents/tools/permissions.py`,
> `PersonaAgent.spawn_sub_agent`) now say it is not built and name this
> issue.
>
> The row "A delegation that names no tool" was added the same day, after
> that pull request's review: the first text said the delegation's list
> holds a sub-agent and left out that its default holds nothing.

> 2026-11-28 — severity ruled `low`, as proposed: the maintainer's
> ruling at the
> [strategy review of 2026-11-28](../project-strategy-review-2026-11-28.md#3-the-maintainers-rulings),
> ratified by ruling (ab) of the
> [sequencing Amendment 2026-11-28](../v0.4.x-sequencing-2026-11-28.md#rulings).
