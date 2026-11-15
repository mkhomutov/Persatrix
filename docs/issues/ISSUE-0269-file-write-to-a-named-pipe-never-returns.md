---
id: ISSUE-0269
summary: "`file_write` opens whatever the path names. When that is a named pipe with nothing reading it, the write never returns: the tool call waits until something cancels it, and the worker thread is never given back. Twelve such writes on an eight-core machine use up the agent's thread pool, after which `file_read`, `file_write` and every name lookup wait for ever, and the agent process does not exit by itself."
status: open
severity: low
area: agents/tools
created: 2026-11-16
refs:
  - agents/tools/builtin.py
  - agents/tools/bounded_io.py
  - docs/issues/ISSUE-0215-tools-buffer-whole-output-before-the-cap.md
  - docs/issues/ISSUE-0266-a-cancelled-file-write-still-writes-its-file.md
  - docs/issues/ISSUE-0242-file-tools-check-a-path-and-then-open-it-by-name.md
  - https://github.com/mkhomutov/Persatrix/pull/1132
---

# ISSUE-0269: `file_write` to a named pipe never returns

## Summary

`file_read` refuses anything that is not a regular file: it opens the
path without waiting, looks at what it opened and raises for a pipe, a
socket or a device (`read_text_file` in `agents/tools/bounded_io.py`).
`file_write` has no such check. `write_text_file` calls
`Path.write_text`, which opens the path for writing, and opening a
named pipe for writing waits until something opens it for reading.

With nothing reading, the open never returns.

## Context

Shown with a throwaway script that makes a named pipe in the agent's
write folder and calls the tool, each call under a limit of 0.3 s:

| | What happens |
|---|---|
| `main` at `111b041d`, one write | The write is on the event loop. Nothing else in the agent runs again; the limit never fires. The process had to be killed |
| With the write in a thread ([#1132](https://github.com/mkhomutov/Persatrix/pull/1132)), one write | The call is cancelled by its limit and the agent's other coroutines go on. The thread stays in the open. `file_read`, `file_write` of another file and a name lookup still answer. When the agent's main coroutine returns, the process does not exit: Python waits for the thread |
| The same, twelve writes | The default pool has `min(32, cores + 4)` threads, 12 on the eight-core machine used. With all of them in an open, `file_read`, `file_write` and `loop.getaddrinfo`, which is how `http_request` and the provider SDKs look a host name up, got no answer in 2 s each |

So this is better than it was, where one such write stopped the agent
for good, and it is still open:

- A stuck write holds a worker thread for the life of the process.
- When the pool is used up, the file tools and every name lookup stop,
  which stops `http_request` and every model call that opens a new
  connection.
- The process needs a kill to end.
- Writes to one path now wait for each other
  ([ISSUE-0266](ISSUE-0266-a-cancelled-file-write-still-writes-its-file.md)).
  A second write to the same pipe therefore waits behind the first, in
  a thread of its own, and never returns either.

How a pipe gets into the write folder: a command the agent is allowed
to run makes one (`mkfifo`), or it is there already. The shipped
`code-writer` lists no shell command.

## Impact

One write to a pipe costs a thread and a clean exit; a dozen stop the
agent's file tools and its network calls until it is restarted.
Proposed low: it needs a named pipe inside the agent's write allowlist,
which an agent with no shell command cannot make, and the tracker's
neighbours are low ([ISSUE-0266](ISSUE-0266-a-cancelled-file-write-still-writes-its-file.md),
[ISSUE-0242](ISSUE-0242-file-tools-check-a-path-and-then-open-it-by-name.md)).
On `main` the same write was worse and belonged to
[ISSUE-0215](ISSUE-0215-tools-buffer-whole-output-before-the-cap.md),
medium; the move to a thread took the edge off it. The severity is
proposed here, not ruled.

## Proposed fix / investigation path

Open as `file_read` does: `os.open` with `O_WRONLY | O_CREAT |
O_NONBLOCK`, without `O_TRUNC`; look at what was opened; raise
`NotARegularFileError` for anything that is not a regular file; then
truncate and write through that descriptor. Opening a pipe for writing
without waiting fails at once when nothing reads it, and the check on
the open file, not on the path, is the one a swap of the path cannot
get round ([ISSUE-0242](ISSUE-0242-file-tools-check-a-path-and-then-open-it-by-name.md)).
A test with a pipe, bounded by a time limit, that fails on today's
code by running into the limit.

## Notes

> 2026-11-16 — found by the review of [#1132](https://github.com/mkhomutov/Persatrix/pull/1132),
> which moved the write to a worker thread. Filed there; the write's
> open is not changed by that pull request.
