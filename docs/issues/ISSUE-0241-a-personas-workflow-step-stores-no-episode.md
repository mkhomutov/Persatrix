---
id: ISSUE-0241
summary: "A persona never remembers a workflow step it served. `PersonaAgent.handle` wraps the task object itself into the event, the episode's context is then not JSON, and storing the episode fails with `TypeError: Object of type TaskInput is not JSON serializable`. The failure is logged as a WARNING and swallowed, so the step completes and the persona's episodic memory holds nothing of it."
status: open
severity: medium
area: agents/persona
created: 2026-11-13
refs:
  - agents/persona.py
  - agents/persona_runtime/episode_routing.py
  - agents/memory/episodic_writes.py
  - docs/issues/ISSUE-0206-action-parser-raises-on-wrong-shaped-json.md
  - https://github.com/mkhomutov/Persatrix/pull/1105
---

# ISSUE-0241: A persona's workflow step stores no episode

## Summary

When a persona serves a workflow step, `PersonaAgent.handle`
(`agents/persona.py`) turns the task into an event whose payload is
`{"task": task}`, with `task` the `TaskInput` object. At the end of the
turn the runtime stores an episode whose context is
`{"event": event.payload, "sender": ...}`
(`agents/persona_runtime/episode_routing.py`), and the episodic store
writes that context with `json.dumps`
(`agents/memory/episodic_writes.py`). A `TaskInput` is not JSON, so the
write raises, the runtime logs it and carries on.

## Context

Reproduced on `main` at `0f3eb3ac`, and unchanged on the branch that
filed this: a persona with a model double that completes the task,
`handle` called once on a temporary database.

- The step ends `COMPLETED` with the model's result.
- The log has one WARNING with a traceback, `Failed to store episode
  for agent ember-owl (event_type=task_assigned)`, caused by
  `TypeError: Object of type TaskInput is not JSON serializable`.
- The `episodes` table has no row.

The line that builds the event dates from the project's first commit.
Channel messages and ticks are not affected: their payloads are plain
JSON values.

## Impact

Every workflow step a persona serves, not an error path. The persona's
episodic memory, and whatever is recalled or derived from it later,
holds no trace that the step happened or what it produced. An operator sees a WARNING with a
traceback for each step and nothing else fails.

Proposed medium, by likeness to the second effect of
[ISSUE-0206](ISSUE-0206-action-parser-raises-on-wrong-shaped-json.md)
(an inbound event that leaves no episode). The severity is proposed
here, not ruled.

## Proposed fix / investigation path

Give the event a payload that is JSON: the task's fields (its id, type,
input and metadata) and not the object. Check what the prompt builder
reads from `payload["task"]` first, since it formats the same payload
for the model. Then a test that a persona's workflow step leaves one
episode.

## Notes

> 2026-11-13 — found by an independent review of the fix for
> [ISSUE-0206](ISSUE-0206-action-parser-raises-on-wrong-shaped-json.md),
> while running whole persona turns through a workflow step. Not part
> of that fix: the failure is on `main` and has nothing to do with the
> shape of the model's reply.
