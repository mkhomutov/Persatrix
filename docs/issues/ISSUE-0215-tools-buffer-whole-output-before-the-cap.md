---
id: ISSUE-0215
summary: "The shell, HTTP and file-read tools load a command's whole output, a whole response body or a whole file into memory and only then cut it to the output cap. A command that prints 300 MB took the agent from 68 MB to 1,286 MB of memory while the model was shown 4 KB. File reads also block the agent's event loop."
status: open
severity: medium
area: agents/tools
created: 2026-11-10
refs:
  - agents/tools/builtin.py
  - docs/rfcs/0009-security-sandboxing.md
  - docs/rfcs/0009-phases-3-4-pr-plan.md
  - docker-compose.yaml
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
