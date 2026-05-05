---
id: ISSUE-0033
summary: "chat-as-DM single-shot replyWaiter drops multi-message agent replies (tool_call → tool_result → final_answer pattern)"
status: open
severity: medium
area: internal/channels
created: 2026-05-05
refs:
  - internal/channels/waiter.go
  - internal/channels/router.go
  - internal/server/chat_handler.go
  - docs/rfcs/0011-amendment-chat-as-dm.md
---

## Summary

The chat-as-DM façade (RFC 0011 PR 4a-ii-β-2) parks a `replyWaiter`
keyed on `(channelID, awaitFromAgentID)` and resolves it on the FIRST
matching `SEND_CHANNEL_MESSAGE` from the agent. Agents that emit
multiple messages in response to a single chat turn (e.g. a
`tool_call → tool_result → final_answer` plugin pattern, or a
streaming-style chunked answer) surface only the first message to the
chat caller; the remainder are persisted to the DM history but never
delivered to the synchronous chat response.

## Context

Captured during the PR #251 deep review. The single-shot semantics are
documented in [`internal/channels/waiter.go`](../../internal/channels/waiter.go)
on the `Notify` doc-comment and pinned by
`TestHandleChat_MultiMessageReplyReturnsFirst` in
[`internal/server/chat_handler_test.go`](../../internal/server/chat_handler_test.go).
The persona-runtime in v0.3.0 always emits a single
`SEND_CHANNEL_MESSAGE` per chat turn, so the constraint is invisible
today. It becomes user-visible the moment any agent adopts a
multi-step reply pattern — likely a v0.4+ surface concern as tools
and sub-agent fan-in/fan-out land.

## Impact

- Silent data loss from the chat caller's perspective: the agent's
  full reply is in the DM history but `chatResponse.reply` carries
  only the first chunk. Callers using the REST chat surface (Rust
  CLI, future web client) get a truncated answer with no error.
- Workflow regression risk: any future workflow step that wraps
  `PublishAndAwait` (today only the chat handler does) inherits the
  same constraint without further code review.
- Diagnostic difficulty: the discrepancy between persisted history
  and chat response is invisible without a side-by-side comparison;
  operators are unlikely to notice until a user complains.

## Proposed fix / investigation path

The waiter doc-comment already lists two candidate strategies; both
need design work before a v0.4 implementation lands:

1. **Agent-side fold** — collapse the multi-message reply pattern
   into a single `SEND_CHANNEL_MESSAGE` with structured content
   (e.g. JSON parts list). Cleanest from the chat caller's
   perspective but pushes complexity into every agent and breaks the
   "messages are atomic publish events" mental model the channels
   subsystem relies on.

2. **Wait-on-history** — replace the `(channelID, senderID)` waiter
   with a higher-level "wait for agent turn to complete" primitive
   that watches for an end-of-turn marker (e.g. a metadata flag or a
   bounded silence window) and returns the concatenated message
   range. Keeps agents simple but introduces a new "agent turn"
   concept that needs its own contract.

Either option is a v0.4 RFC item (or an amendment to RFC 0011).
Whichever is chosen, the chosen design MUST also extend the chat
handler's response shape to surface multi-part replies (the current
`chatResponse.reply` is a single string).

## Notes

> 2026-05-05 — captured during PR #251 deep review. Anchors the
> "future fixes" hint in the `replyWaiter` doc-comment to a tracked
> issue so the constraint surfaces in `docs/issues/INDEX.md` rather
> than relying on contributors to grep the source.
