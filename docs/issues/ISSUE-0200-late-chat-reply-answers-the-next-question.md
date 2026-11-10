---
id: ISSUE-0200
summary: "A chat reply is matched to its request by channel and sender only. After one chat request times out, the persona's late answer is returned to the next chat request on that DM, and every later answer on that DM is then one question behind for as long as replies outlast the timeout."
status: open
severity: medium
area: internal/channels
created: 2026-11-10
refs:
  - internal/channels/waiter.go
  - internal/channels/publish_and_await.go
  - internal/channels/floor_control.go
  - internal/server/chat_handler.go
  - docs/issues/ISSUE-0033-chat-as-dm-single-shot-waiter-multi-message-reply.md
  - docs/issues/ISSUE-0073-cpu-local-model-chat-exceeds-default-timeout.md
---

# ISSUE-0200: After a chat times out, the next chat gets the late answer to the previous question

## Summary

The chat endpoint publishes the person's message into the DM channel and
waits for the persona's reply. The wait is registered under the pair
(channel, sender) in `replyWaiter` (`internal/channels/waiter.go`).
Nothing ties a reply to the message it answers, so the first reply to
arrive satisfies whichever request is waiting at that moment.

## Context

A probe with a slow agent double:

1. Chat 1 asks "Q1" and times out.
2. Chat 2 asks "Q2: what is 2+2?".
3. The agent's late "A1: Paris" arrives. Chat 2 returns it, with no error.
4. The real answer to Q2 then finds no waiter.

The default chat timeout is 30 seconds, and
[ISSUE-0073](ISSUE-0073-cpu-local-model-chat-exceeds-default-timeout.md)
already records local models that take longer. The floor-control round
uses the same waiter, so a speaker's late reply from one round can be
taken as its turn in the next.
[ISSUE-0033](ISSUE-0033-chat-as-dm-single-shot-waiter-multi-message-reply.md)
covers a different limit of the same waiter, replies that come as several
messages.

## Impact

A person gets a confident answer to a question they did not ask, and the
conversation stays one answer behind until a reply beats the timeout. The
history in the channel store is correct; only the chat responses are
shifted.

## Proposed fix / investigation path

Match on the server side: accept only a reply stored after this request's
inbound message, stamped with the interaction it was dispatched under. A
test that times out one request and then sends a second would pin it.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; reproduced with a throwaway Go test mapped in with
> `go test -overlay`.
