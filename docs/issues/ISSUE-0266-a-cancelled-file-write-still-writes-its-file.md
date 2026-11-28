---
id: ISSUE-0266
summary: "`file_write` and `file_read` do their file work in a worker thread, which a cancellation cannot stop. A `file_write` cancelled while it writes returns at once and its file is still written a moment later, after the caller has been told the call was cancelled. A retry now waits for that write and is not replaced by it; anything else that runs next can still find the file absent, empty or complete, and a read that overlaps a write can read an empty file."
status: open
severity: low
area: agents/tools
created: 2026-11-15
refs:
  - agents/tools/builtin.py
  - agents/tools/bounded_io.py
  - agents/server_servicers.py
  - agents/persona_runtime/__init__.py
  - agents/event_loop.py
  - docs/issues/ISSUE-0215-tools-buffer-whole-output-before-the-cap.md
  - docs/issues/ISSUE-0243-a-cancelled-shell-command-keeps-running.md
  - docs/issues/ISSUE-0269-file-write-to-a-named-pipe-never-returns.md
  - tests/unit/python/test_tool_file_write_one_at_a_time.py
  - https://github.com/mkhomutov/Persatrix/pull/1132
---

# ISSUE-0266: A cancelled `file_write` still writes its file

## Summary

`file_write` hands the write to a worker thread
(`asyncio.to_thread(bounded_io.write_text_file, …)` in
`agents/tools/builtin.py`), as `file_read` hands over its read. That is
what keeps a slow disk from pausing the whole agent
([ISSUE-0215](ISSUE-0215-tools-buffer-whole-output-before-the-cap.md)).
A thread cannot be cancelled. When the tool call is cancelled, the call
ends at once and the thread carries on.

What cancels a tool call, each read in the code on 2026-11-16:

- the time limit of a workflow task: `ExecuteTask` runs the agent under
  `asyncio.wait_for` with the request's `timeout_seconds`
  (`agents/server_servicers.py`);
- a persona's event time-out, 300 seconds unless the persona's config
  says otherwise (`agents/persona_runtime/__init__.py`). The turn's
  lock is released as the time-out fires, so the persona's next turn
  can start while the thread of the cancelled one is still writing;
- the time limit of a chat dispatch, 30 seconds by default and 300 at
  most (`agents/server_servicers.py`);
- the agent stopping: its event loop cancels the tasks still running
  (`agents/event_loop.py`);
- the caller cancelling the RPC. gRPC then cancels the handler; that is
  gRPC's behaviour and was not run here.

## Context

Shown with a throwaway script, a write slowed to half a second and
cancelled after a tenth, followed at once by a second write of the same
path:

| | The first call | The file |
|---|---|---|
| Before the write moved to a thread | not cancelled: the write held the event loop, so the cancellation had to wait and the call returned its result at 0.5 s | the first text at 0.5 s, then the second |
| With the write in a thread and no lock (the first version of [#1132](https://github.com/mkhomutov/Persatrix/pull/1132)) | cancelled at 0.1 s | absent at 0.1 s; the second text when the second call returned; **the first text 0.8 s later** |
| Now: one write at a time for a path | cancelled at 0.1 s | absent at 0.1 s; the second call waits for the first write to end, then writes; the second text, and it stays |

So the lock closes the worst of it: a later write that reported success
can no longer be replaced by the cancelled one. Four things are left.

1. **The caller is told "cancelled" while the write is still to come.**
   Whatever runs next and does not write that path, a read, a shell
   command, another agent on the same folder, can find the file absent,
   empty or complete.
2. **A retry waits.** The second write takes as long as the cancelled
   one still needs, on top of its own. For a write that never ends, a
   named pipe nobody reads, it waits for good
   ([ISSUE-0269](ISSUE-0269-file-write-to-a-named-pipe-never-returns.md)).
3. **A read that overlaps a write of the same path can read an empty
   file.** Writing truncates the file and then fills it; a read in
   another thread can land in between. Measured with a text of 60,000
   bytes, 5,000 rounds each:

   | | The read is started first | The write is started first |
   |---|---|---|
   | `main` at `111b041d` | empty in 119 | empty in 0 |
   | Now | empty in 83 | empty in 439 |

   On `main` the read has been in a thread since the first fix of
   ISSUE-0215, so a read started first could already see the file
   truncated. The second column is new: with the write in a thread too,
   a read can run while it writes. The lock is for writes and does not
   make a read wait. (The review of #1132 measured 0 on `main` for both
   orders; on this machine the first order is not 0.)
4. **Two names for one file take two locks.** The lock is on the path
   as resolved. A hard link, or the same name in another letter case on
   a disk that ignores case, is a second path to the same file.

A cancelled `file_read` also reads on in its thread. That costs a
little time and changes nothing.
[ISSUE-0243](ISSUE-0243-a-cancelled-shell-command-keeps-running.md) is
the same shape for `shell_exec`: the call is cancelled and the command
runs on.

## Impact

After a cancelled write, the agent's next step can read a file that the
cancelled step is still writing, and a read beside any write can come
back empty with `success`. Proposed low: the first needs a cancellation
that lands inside a write, the second two tool calls of two turns on
one file at the same instant (the tool calls of one turn run one after
another); the write is the one the model asked for, and it finishes
within the time the disk takes. The severity is proposed here, not
ruled.

## Proposed fix / investigation path

Write to a temporary file beside the target and rename it into place,
in the same thread and under the path's lock. A reader then finds the
old file or the new one and never an empty or half-written one, which
closes point 3 and the "empty" of point 1. It does not make the file
appear before the caller is told "cancelled", and by itself it would
not have stopped a cancelled write from replacing a retry: the lock
does that.

For point 1, decide whether a cancelled call should wait for its
thread before it reports the cancellation. That is the old behaviour
for the write, without the paused agent.

## Notes

> 2026-11-15 — noticed while moving `file_write` off the event loop for
> [ISSUE-0215](ISSUE-0215-tools-buffer-whole-output-before-the-cap.md) in
> [#1132](https://github.com/mkhomutov/Persatrix/pull/1132).

> 2026-11-16 — rewritten after the review of [#1132](https://github.com/mkhomutov/Persatrix/pull/1132),
> which showed two things this text did not say: a retry that had
> reported success could be replaced afterwards by the cancelled
> write, and two overlapping writes could leave a file that was
> neither text. Both are closed in the same pull request by a lock for
> each path in `write_text_file`. This text now lists what can cancel a
> tool call and what is left once writes to a path wait for each other.

> 2026-11-28 — severity ruled `low`, as proposed: the maintainer's
> ruling at the
> [strategy review of 2026-11-28](../project-strategy-review-2026-11-28.md#3-the-maintainers-rulings),
> ratified by ruling (ab) of the
> [sequencing Amendment 2026-11-28](../v0.4.x-sequencing-2026-11-28.md#rulings).
