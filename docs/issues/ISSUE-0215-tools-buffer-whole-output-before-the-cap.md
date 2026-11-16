---
id: ISSUE-0215
summary: "Code half fixed: the shell, HTTP and file-read tools used to load a command's whole output, a whole response body or a whole file into memory before cutting it to the output cap, and now keep no more than the cap; file reads and file writes run off the event loop, a read refuses a named pipe, and a response body is read no further than about 4 MB of its own bytes. Still open: no compose file sets a memory or process limit on the agent containers, so nothing bounds an agent's memory from outside. Two faults of the same kind are filed apart: a write to a named pipe never returns (ISSUE-0269), and a response body is decoded on the event loop in whatever charset the server names (ISSUE-0270)."
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
  - tests/unit/python/test_tool_bounded_http_body.py
  - docs/issues/ISSUE-0242-file-tools-check-a-path-and-then-open-it-by-name.md
  - docs/issues/ISSUE-0243-a-cancelled-shell-command-keeps-running.md
  - tests/unit/python/test_tool_bounded_http_body_bytes.py
  - tests/unit/python/test_tool_file_write_thread.py
  - docs/issues/ISSUE-0266-a-cancelled-file-write-still-writes-its-file.md
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

What remains, as of 2026-11-15 (the first two points above are done):

1. Memory and process limits on the agent services in the compose
   files. Sizing them needs a real compose run under load.

Filed apart since, and named here because they are the same kind of
fault, a tool call that stops more than itself:

- `file_write` still opens a named pipe, and with nothing reading it
  the write never returns
  ([ISSUE-0269](ISSUE-0269-file-write-to-a-named-pipe-never-returns.md)).
  The agent's other coroutines now go on, which they did not when the
  write held the event loop; the worker thread is never given back.
- `http_request` decodes a body on the event loop, in whatever charset
  the server names, and one of Python's decoders takes minutes on a
  few hundred kilobytes
  ([ISSUE-0270](ISSUE-0270-http-request-decodes-on-the-event-loop-in-any-charset-the-server-names.md)).
- The bound on a body's bytes ends an endless body only when it
  arrives faster than about 135 KB a second. A slower one still ends
  as the 30-second timeout, with no status and no headers. Not filed:
  it is what the timeout is for, and what a slow endless body gave
  before.

Two more points stood here from 2026-11-13 and are done, see the note
of 2026-11-15: `file_write` wrote on the event loop, and a response body
that decodes to almost no text was read until it ended or the request
timed out.

This issue is not resolved until the compose limits are set or ruled
out.

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
> What remains is listed under the proposed fix above: the compose
> limits, `file_write` on the event loop, and a body that decodes to
> almost no text. The last two were first recorded only in this note;
> they are in the list so the issue cannot be closed without them.

> 2026-11-13 — after review of the code half. The first version decoded
> a response body piece by piece, and that changed results under the
> cap: a body labelled UTF-16 or UTF-32 with no byte-order mark failed
> the whole call, where `resp.text()` reads it in the machine's byte
> order; a charset that is a codec and not a text encoding (`base64`,
> `hex`, `zlib`) failed with an empty message where there had been a
> clear one; and a few malformed bodies decoded with one replacement
> character more or fewer. A body's bytes are now kept, up to a few
> times the cap, and decoded in one go as before; the piece-by-piece
> decoder only measures how much text has arrived. Sixty thousand
> generated bodies in nineteen charsets now give what `bytes.decode`
> gives, error or text, with no difference.
>
> Two things found on `main` beside it are filed apart:
> [ISSUE-0242](ISSUE-0242-file-tools-check-a-path-and-then-open-it-by-name.md)
> and
> [ISSUE-0243](ISSUE-0243-a-cancelled-shell-command-keeps-running.md).
>
> Where a memory bound is claimed above, it is this code's own. A
> compressed body is inflated by aiohttp before the tool sees it.

> 2026-11-15 — points 2 and 3 of the list of 2026-11-13 are done in
> [#1132](https://github.com/mkhomutov/Persatrix/pull/1132); the compose
> limits remain, so this stays `in_progress`.
>
> `file_write` now writes, and makes the folders above the file, in a
> worker thread, as `file_read` reads. On `main` at `111b041d` a write
> held up on purpose kept every other coroutine of the agent from
> running for as long as it lasted. A thread cannot be cancelled, so a
> write cancelled part-way now finishes after its call has returned:
> [ISSUE-0266](ISSUE-0266-a-cancelled-file-write-still-writes-its-file.md).
>
> A response body is read no further than 40 times the cap in its own
> bytes (4,161,536 bytes with the 64 KB of one read) while its text is
> still under the cap. On `main` a body of escape sequences that never
> ended was read until the request's 30-second timeout, and the call
> failed with `HTTP request timed out`. It now succeeds, with the
> status, the headers and the text read so far, and the body ends with
> `... [truncated: the response was not read past 4064 KB]`.
>
> Why 40. The bound must not cut a body that is a page of text, in any
> charset the tool decodes, and charsets differ in how many bytes they
> spend on a byte of UTF-8 text. Measured over the 110 text encodings
> Python 3.12 ships, each with its own encoder, on every character
> alone and, for the encodings that switch modes, beside an ASCII
> letter: 4 at most (UTF-32, and control characters in
> `unicode_escape`). A
> body written by hand can spend more: 5 with one UTF-7 shift sequence
> for each character, 10 with `unicode_escape`'s eight-digit form, and
> 39.5 with its `\N{...}` form and the longest character name. Forty is
> the first whole number above that. A body that passes the bound has
> therefore spent most of its bytes on something that is not text.
>
> One thing found beside it and fixed with it: a charset that cannot be
> decoded piece by piece was already stopped at the bound on kept
> bytes, 475,136, and when those bytes held less than a page of text
> the body came back short with nothing to say so. It now carries the
> same notice. No charset Python ships gets there with real text; the
> test registers one.

> 2026-11-16 — corrections after the review of
> [#1132](https://github.com/mkhomutov/Persatrix/pull/1132).
>
> The words around 40. "The most bytes any charset can spend on a
> character" holds when each character is spelt once. Some charsets let
> a sequence that adds no text be repeated as often as the sender
> likes: an ISO-2022 escape, HZ's `~{~}`, a backslash and a newline in
> `unicode_escape`. A letter followed by fourteen `ESC ( B` costs 43
> bytes in `iso2022_jp`; ten `~{~}` or twenty backslash-newlines cost
> 41. A body spelt like that is stopped at the bound with less than a
> full page, and carries the notice. The figure stays 40 (the
> maintainer's decision after the review): a smaller one would cut
> bodies spelt once in UTF-7 or with escapes, and no figure covers a
> sequence that can be repeated without end.
>
> What the bound does not do. It is 4,161,536 bytes and the request's
> timeout is 30 seconds, so it ends an endless body only when the body
> arrives faster than about 135 KB a second. A slower one still fails
> with `HTTP request timed out`. And the bytes counted are the body's
> after aiohttp has inflated it: an 8 KB gzip response that inflates
> past the bound is cut the same way.
>
> `file_write`'s move to a thread had two effects the first text of
> this note did not have. Two calls for one path that overlap ran in
> two threads at once: with a text of 60,000 bytes and one of 20, 382
> of 3,000 rounds left a file that was neither, both calls reporting
> success (0 of 3,000 on `main`). And a retry could be replaced by the
> cancelled call it retried. `write_text_file` now takes a lock for the
> path it writes: 0 of 3,000, and the retry stays. What is left of the
> cancelled write is in
> [ISSUE-0266](ISSUE-0266-a-cancelled-file-write-still-writes-its-file.md).
>
> The first run of the new tests on `main` gave 9 failed and 47 passed,
> not the 7 and 47 this pull request's text said.
