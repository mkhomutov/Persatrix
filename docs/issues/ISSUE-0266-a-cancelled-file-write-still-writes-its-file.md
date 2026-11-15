---
id: ISSUE-0266
summary: "`file_write` and `file_read` do their file work in a worker thread, which a cancellation cannot stop. A `file_write` cancelled while it writes returns at once and its file is still written a moment later, after the caller has been told the call was cancelled."
status: open
severity: low
area: agents/tools
created: 2026-11-15
refs:
  - agents/tools/builtin.py
  - agents/tools/bounded_io.py
  - docs/issues/ISSUE-0215-tools-buffer-whole-output-before-the-cap.md
  - docs/issues/ISSUE-0243-a-cancelled-shell-command-keeps-running.md
---

# ISSUE-0266: A cancelled `file_write` still writes its file

## Summary

`file_write` hands the write to a worker thread
(`asyncio.to_thread(bounded_io.write_text_file, …)` in
`agents/tools/builtin.py`), as `file_read` hands over its read. That is
what keeps a slow disk from pausing the whole agent
([ISSUE-0215](ISSUE-0215-tools-buffer-whole-output-before-the-cap.md)).
A thread cannot be cancelled. When the tool call is cancelled, a turn
that runs out of time for one, the call ends at once and the thread
carries on.

## Context

Shown with a throwaway script, a write slowed to half a second and
cancelled after a tenth:

| | The call | The file |
|---|---|---|
| Before the write moved to a thread | not cancelled: the write held the event loop, so the cancellation had to wait and the call returned its result | written when the call returned |
| Now | cancelled at 0.1 s | absent at 0.1 s, written by 0.9 s |

- The caller is told the call was cancelled while the file is still to
  be written. Whatever runs next in the agent can find the file absent,
  half written or complete.
- A cancelled `file_read` also reads on in its thread. That costs a
  little time and changes nothing.
- [ISSUE-0243](ISSUE-0243-a-cancelled-shell-command-keeps-running.md) is
  the same shape for `shell_exec`: the call is cancelled and the command
  runs on.

## Impact

After a cancelled write, the agent's next step can read a file that the
cancelled step is still writing. Proposed low: it needs a cancellation
that lands inside a write, the write is the one the model asked for,
and it finishes within the time the disk takes. The severity is
proposed here, not ruled.

## Proposed fix / investigation path

Write to a temporary file beside the target and rename it into place in
the same thread, so that a reader finds the old file or the new one and
never half of either. Decide separately whether a cancelled call should
wait for its thread before it reports the cancellation: that is the old
behaviour for the write, without the paused agent.

## Notes

> 2026-11-15 — noticed while moving `file_write` off the event loop for
> [ISSUE-0215](ISSUE-0215-tools-buffer-whole-output-before-the-cap.md).
