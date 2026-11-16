---
id: ISSUE-0270
summary: "`http_request` decodes a response body on the agent's event loop, in whatever charset the server's header names, and accepts every text encoding Python knows. Python's `punycode` decoder takes time that grows with the square of the input: 200,000 bytes take ten seconds, during which nothing else in the agent runs and the request's own time-out cannot fire. One small response from an allowed host can pause every persona in the process for about a minute."
status: open
severity: low
area: agents/tools
created: 2026-11-16
refs:
  - agents/tools/bounded_io.py
  - agents/tools/builtin.py
  - docs/issues/ISSUE-0215-tools-buffer-whole-output-before-the-cap.md
  - https://github.com/mkhomutov/Persatrix/pull/1132
---

# ISSUE-0270: `http_request` decodes on the event loop in any charset the server names

## Summary

`read_body_text` (`agents/tools/bounded_io.py`) takes the charset from
the response's `Content-Type` header and decodes with it twice: piece
by piece as the body arrives, to measure how much text there is, and
once more in one go at the end, over the bytes it kept (up to 475,136).
Both run in the tool's coroutine, on the event loop. Any codec Python
registers as a text encoding is accepted.

Most decoders are fast. `punycode` is not: its time grows with the
square of the input.

## Context

Measured on a development laptop, Python 3.12, 2026-11-16:

| Input, bytes of `9` labelled `charset=punycode` | Time |
|---|---|
| 25,000, through the tool against a local server | 0.4 s with no other coroutine running |
| 50,000, the same | 1.3 s |
| 100,000, the same | 4.0 s |
| 200,000, the decoder alone, in one go | 10.0 s |
| one 64 KB read, the decoder alone, piece by piece | 1.1 s |

- The pause is the whole agent's: every persona in the process, its
  heartbeat to the orchestrator and its other tool calls wait.
- The request's 30-second time-out is a timer on the same loop. It
  cannot fire while the loop is in the decoder, so it does not bound
  the pause.
- The bytes kept for the last decode are bounded at 475,136. At the
  square law that is about a minute in one stretch. The review of
  [#1132](https://github.com/mkhomutov/Persatrix/pull/1132)
  measured 14.5 s for one response of 180 KB.
- A body that goes on is read piece by piece up to the bound on bytes,
  about 4 MB: some sixty reads of 64 KB at about a second each, with a
  turn for other coroutines between them at best.
- The host must be on the agent's `network.allow` list. A host that is
  allowed and hostile, or one that serves content it does not control,
  is enough.

The same code is on `main` at `111b041d`; the decoding has been on the
loop since the tool was written, and aiohttp's own `resp.text()` did
the same before it.

## Impact

One response of a few hundred kilobytes pauses an agent process for
tens of seconds, and can be repeated with every request. Nothing is
lost or disclosed. Proposed low: it needs a response from an allowed
host that names an unusual charset on purpose, and the agent recovers
by itself. Against that, it is the same kind of fault as
[ISSUE-0215](ISSUE-0215-tools-buffer-whole-output-before-the-cap.md),
which is medium: one tool call that stops more than itself. The
severity is proposed here, not ruled.

## Proposed fix / investigation path

Two parts, either of which closes it:

- Accept only the charsets a web server has reason to send, the
  encodings of the WHATWG Encoding Standard, say, and read anything
  else as UTF-8 with replacement, which is what a browser does with a
  label it does not know.
- Decode in a worker thread, as the file tools read and write, so that
  a slow decoder costs its own call and not the agent. The time-out
  can then end the wait, though not the thread.

A test with a body labelled `punycode`, bounded by a time limit, during
which another coroutine must run.

## Notes

> 2026-11-16 — found by the review of [#1132](https://github.com/mkhomutov/Persatrix/pull/1132),
> which bounded the bytes this code reads. Filed there; the decoding is
> not changed by that pull request.
