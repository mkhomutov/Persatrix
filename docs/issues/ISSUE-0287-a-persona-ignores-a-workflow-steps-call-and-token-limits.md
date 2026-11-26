---
id: ISSUE-0287
summary: "A persona that serves a workflow step does not read the step's `max_llm_calls` and `max_tokens`. The orchestrator sends both with every task, and a task agent holds its turn to them; a persona's turn uses the limits in its own config and never looks at the task's. Read in the code, not run. Its system prompt is not told the task's tool list either, so it asks the model for a note tool the task left out (run)."
status: open
severity: low
area: agents/persona_runtime
created: 2026-11-26
refs:
  - agents/persona_runtime/action_loop.py
  - agents/base.py
  - agents/persona.py
  - agents/task_types.py
  - agents/server_servicers.py
  - internal/executor/dispatch.go
  - proto/task.proto
  - docs/issues/ISSUE-0214-declared-tool-permissions-are-never-enforced.md
  - https://github.com/mkhomutov/Persatrix/pull/1148
---

# ISSUE-0287: A persona ignores a workflow step's call and token limits

## Summary

Every task the orchestrator sends carries three limits in its
`TaskConfig`: `max_llm_calls`, `max_tokens` and `timeout_seconds`. A task
agent applies all three. A persona applies only the time limit: the
number of model calls in its turn and the size of each reply come from
the persona's own config, whatever the task says.

## Context

| Step | Task agent | Persona |
|---|---|---|
| The orchestrator sets `MaxLlmCalls`, `MaxTokens` and `TimeoutSeconds` on the request | `internal/executor/dispatch.go` | the same request |
| The agent's gRPC service copies the first two into the task and wraps the whole call in the time limit | `agents/server_servicers.py` (`ExecuteTask`) | the same |
| The turn reads the task's limits | `BaseAgent._run_llm_loop` (`agents/base.py`): `task.config.max_llm_calls or self.config.get("max_llm_calls", …)`, and the same for `max_tokens`; a negative value fails the task | `_on_event_inner` (`agents/persona_runtime/action_loop.py`): `self.config.get("max_llm_calls", 10)` and `self.config.get("max_tokens", 4096)`. The task is in the event's payload and is not consulted |

A zero means "not set" on the wire, and a task agent then falls back to
its own config, so the two kinds differ only when the orchestrator sends
a limit of its own.

Found while a third field of the same message, `allowed_tools`, was being
enforced for both agent kinds
([ISSUE-0214](ISSUE-0214-declared-tool-permissions-are-never-enforced.md)).
That fix reads the task off a persona's `TASK_ASSIGNED` event to get its
tool list; the two limits sit beside it and are left unread.

This was read in the code on 2026-11-26 and not run: no persona was
given a task with a limit lower than its own config's. Where the
orchestrator takes the limits from was not traced past the dispatch.

One more thing a persona does not take from a task, of the same kind.
A persona's system prompt tells the model to call its note tools
(`store_note`, `recall_notes`, `update_note`, `delete_note`) whenever
the persona has them, and is not told the task's `allowed_tools` list
(`_build_system_prompt` in `agents/persona_runtime/prompt_assembly.py`,
the `memory-tool-usage` snippet). A task whose list leaves the note
tools out is therefore not offered them, and a call to one answers
`Unknown tool`, while the prompt still asks for the call. Latent: the
orchestrator never sets the list. This part was run on 2026-11-26: a
persona given a task that allows one other tool was offered that tool
alone, `recall_notes` answered `Unknown tool: recall_notes`, and the
system prompt of the same call named `store_note` and `recall_notes`.

## Impact

Proposed low. A persona still stops: its own config bounds the calls of
a turn, the step's time limit applies, and the wallet refuses a call the
budget does not cover. What is lost is a tighter limit set for one step
or one run: it binds a task agent and not a persona, and nothing says
so. A negative limit, which fails a task agent's task as a
misconfiguration, passes a persona unnoticed. The severity is proposed
here, not ruled.

## Proposed fix / investigation path

1. Run it first: send a persona a task with `max_llm_calls: 1` and a
   model that keeps asking for tools, and count the calls.
2. In the persona's turn, read the task's two limits where the task's
   tool list is now read, with the task agent's rule: the task's value
   when it is set, the persona's config otherwise; refuse a negative
   value the same way.
3. Decide whether the task's limit may raise a persona's own or only
   lower it. A task agent lets it do either.

The workflow engine is frozen (`internal/scheduler`, `internal/planner`,
`internal/executor`, `internal/state`), and this needs no change there:
the orchestrator already sends the limits. The fix is on the agents'
side.

## Notes

> 2026-11-26 — filed by
> [#1148](https://github.com/mkhomutov/Persatrix/pull/1148), the fix for
> ISSUE-0214, where it was noticed and left alone. The paragraph on the
> note tools and a task's `allowed_tools` was added the same day, after
> that pull request's review.
