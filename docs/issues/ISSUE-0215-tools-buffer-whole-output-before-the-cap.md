---
id: ISSUE-0215
summary: "Code half fixed: the shell, HTTP and file-read tools used to load a command's whole output, a whole response body or a whole file into memory before cutting it to the output cap, and now keep no more than the cap; file reads run off the event loop and refuse a named pipe. Still open: no compose file sets a memory or process limit on the agent containers, so nothing bounds an agent's memory from outside."
status: in_progress
severity: medium
area: agents/tools
created: 2026-11-10
refs:
  - agents/tools/builtin.py
  - docs/rfcs/0009-security-sandboxing.md
  - docs/rfcs/0009-phases-3-4-pr-plan.md
  - docker-compose.yaml
  - agents/tools/bounded_io.py
  - tests/unit/python/test_tool_bounded_output.py
---

# ISSUE-0215: Tools hold a whole output in memory before cutting it to the cap

## Summary

Three built-in tools in `agents/tools/builtin.py` apply their output cap
after reading everything:

| Tool | Call that reads it all |
|---|---|
| `shell_exec` | `proc.communicate()` |
| `http_request` | `resp.text()` |
| `file_read` | `Path.read_text()`, which is also a blocking call inside an `async` function |

The cap limits what the model is shown, not what the agent process holds.

## Context

- With a 4 KB output cap and a command that prints 300 MB, the model
  received 4,096 bytes and the agent's peak memory rose from 68 MB to
  1,286 MB.
- The shipped `code-writer` may run `python` with a 30-second limit, so
  one command chosen by the model is enough.
- `file_read` on a large allowed file does the same and stops every
  other coroutine in the agent while it reads. A named pipe in the
  workspace would stall it indefinitely.
- RFC 0009's resource limits cap a tool's CPU time and the text handed to
  the model. They do not bound what is buffered.
- No compose file sets a memory or process limit, a read-only root or
  dropped capabilities on the agent containers.

## Impact

A steered or simply mistaken model can run an agent container out of
memory. That takes the agent down until its container restarts; each
agent runs in a process of its own.

## Proposed fix / investigation path

- Keep streams and files only up to the cap plus one byte. For a
  command, go on reading and throw the rest away until it ends or its
  time limit passes, so its exit code is still reported.
- Do file I/O in a worker thread, and first refuse anything that is not
  a regular file; a size check alone does not catch a named pipe.
- Add memory and process limits to the agent services in the compose
  files.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; the memory figures come from a scratch probe that calls
> `shell_exec` directly.

> 2026-11-12 — the shipped `code-writer` no longer lists `python` or any other
> command (ISSUE-0196's config half). The buffering is unchanged for an
> agent whose config lists one, and for `file_read` and `http_request`.

> 2026-11-12 — the code half is done; the compose limits remain, so this
> stays `in_progress`. Reproduced first on `main` at `0f3eb3ac`, one
> tool call handling 300 MB with a 4 KB output cap, peak memory of the
> agent process before the call and at its highest: `shell_exec` 70 MB
> to 1,294 MB, `http_request` 71 MB to 1,292 MB, `file_read` 71 MB to
> 671 MB. After the change: 70, 73 and 71 MB. `file_read` on a named
> pipe waited until something wrote to it.
>
> What changed (`agents/tools/bounded_io.py`): a command's two streams
> are read at once, one byte past the cap of each is kept and the rest
> is read and dropped, so the command still ends by itself or by its
> time or CPU limit, with its exit code; a response body is decoded as
> it arrives and reading stops once the text is over the cap; a file is
> read in a worker thread, one character past the cap, and only if it
> is a regular file. A result under the cap is unchanged, and so is the
> truncation notice.
>
> Two things the text above does not say. The body's bound cannot be
> counted in the body's own bytes, nor the file's in bytes on disk: a
> UTF-16 body is twice the size of its text, and a file with Windows
> line endings is longer on disk than the text it reads as, so a reader
> that stopped at the cap in raw bytes would drop the tail and add no
> notice. And one result changes for a file over the cap: bytes that
> are not text, far past the cap, are no longer reached, so the file is
> returned cut where it used to be refused as binary.
>
> What remains: memory and process limits on the agent services in the
> compose files. Sizing them needs a real compose run under load, which
> the change above did not have.
>
> Two things seen beside it and left as they are. `file_write` still
> writes on the event loop; what it writes is the model's own output,
> so it is a pause and not a memory fault. And a response body that
> decodes to almost no text is still read until it ends or the
> 30-second timeout, though no longer held.
