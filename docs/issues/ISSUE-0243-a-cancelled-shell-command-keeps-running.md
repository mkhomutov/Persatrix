---
id: ISSUE-0243
summary: "When the task running `shell_exec` is cancelled, the tool stops waiting and the command it started goes on running, with no time limit any more. A persona's event time-out cancels a turn's tool calls this way, so a command the model started can outlive the turn that started it."
status: open
severity: low
area: agents/tools
created: 2026-11-13
refs:
  - agents/tools/builtin.py
  - agents/tools/bounded_io.py
  - agents/persona_runtime/__init__.py
  - docs/issues/ISSUE-0215-tools-buffer-whole-output-before-the-cap.md
  - https://github.com/mkhomutov/Persatrix/pull/1106
---

# ISSUE-0243: A cancelled shell command keeps running

## Summary

`shell_exec` (`agents/tools/builtin.py`) starts a command in a session of
its own and waits for it under `asyncio.wait_for`. Its own time limit is
handled: the command's process group is stopped, then killed. A
cancellation of the calling task is not handled. The wait ends, nothing
stops the command, and the time limit that would have stopped it is gone
with the wait.

## Context

Reproduced on `main` at `0f3eb3ac`, and unchanged on the branch that
filed this: `shell_exec` with a one-second time limit, running a command
that sleeps two seconds and then writes a file; the task cancelled after
0.4 seconds. The call ended at once with `CancelledError`, and the file
appeared two seconds later, past the command's own limit.

A persona runs each event under a time-out (`event_timeout`, 300
seconds by default) that cancels the turn, tool calls included. The
task servicer's limit on a workflow step is another such cancellation.

## Impact

A command survives the turn that started it. It still ends by itself or
at its CPU limit where one is set (`resource_limits.shell_cpu_seconds`),
but a command that waits and does not compute, or one started with no
CPU limit, has nothing left to stop it until the agent's container
stops. Its output is read by nobody.

Proposed low: it takes a cancellation at the right moment and a command
that would not end by itself. The severity is proposed here, not ruled.

## Proposed fix / investigation path

Catch the cancellation around the wait, stop the command's process group
the way the time-limit path does, and raise the cancellation again. A
test can cancel the call and check that the command's marker file never
appears.

## Notes

> 2026-11-13 — found by an independent review of the fix for
> [ISSUE-0215](ISSUE-0215-tools-buffer-whole-output-before-the-cap.md).
> On `main` already; that fix did not change the cancellation path.
