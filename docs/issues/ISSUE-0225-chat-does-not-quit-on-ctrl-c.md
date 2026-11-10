---
id: ISSUE-0225
summary: "`persatrix chat` tells the user to press Ctrl-C to quit, but at the prompt it keeps running until Enter is pressed as well, and while a reply is pending Ctrl-C does nothing until the reply or the 300-second timeout arrives."
status: open
severity: medium
area: cli
created: 2026-11-10
refs:
  - cli/src/commands/chat.rs
  - docs/issues/ISSUE-0073-cpu-local-model-chat-exceeds-default-timeout.md
---

# ISSUE-0225: `persatrix chat` does not quit on Ctrl-C

## Summary

`cmd_chat` (`cli/src/commands/chat.rs`) installs a Ctrl-C handler that
only sets a flag. The loop checks the flag at the top of each pass. In
between it sits in a blocking `read_line`, or awaits the HTTP request,
and neither is interrupted by the signal.

A comment in the loop says SIGINT interrupts the blocking read and makes
it return an error. It does not: the standard library retries a read
that was interrupted by a signal.

## Context

- The banner says "Type exit or Ctrl-C to quit."
- Run in a pseudo-terminal, the existing binary was still alive three
  seconds after Ctrl-C at the prompt and exited only after a following
  Enter.
- During a turn, the request is not raced against the flag. The CLI
  allows 300 seconds for a reply, while the server's default chat
  timeout is 30
  ([ISSUE-0073](ISSUE-0073-cpu-local-model-chat-exceeds-default-timeout.md)).

## Impact

The first thing many people do in a terminal chat that seems stuck is
press Ctrl-C. Here it appears to do nothing, in the command the README's
first example uses.

## Proposed fix / investigation path

Read stdin on a blocking task and `select!` it against
`tokio::signal::ctrl_c()`. Do the same for the in-flight request, so
Ctrl-C cancels a pending turn. Remove the incorrect comment.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; reproduced with the built CLI in a pseudo-terminal.
