---
id: ISSUE-0019
summary: TaskAck is named generically but used by exactly one RPC; add a proto reuse-policy comment to prevent scope-creep coupling
status: open
severity: low
area: build/proto
created: 2026-05-04
refs:
  - proto/task.proto
  - docs/rfcs/0011-pr-plan.md
---

## Summary

`message TaskAck` (added in PR #246, [proto/task.proto](../../proto/task.proto))
is currently the response type of exactly one RPC,
`AgentService.ReceiveChannelMessage`, but its name suggests a generic
fire-and-acknowledge envelope reusable across any RPC. Without an
explicit reuse-policy comment, the next RPC author who needs an ack is
likely to reach for `TaskAck` and extend it with channel-specific
fields, coupling unrelated RPCs through a shared message.

## Context

Captured during PR #246 deep review (Should-fix #4). The PR description
acknowledges that the original RFC 0011 PR plan claimed `TaskAck` was
"existing" but the author had to introduce it; the genericness was
inherited from that mis-recollection rather than chosen.

## Impact

- Scope creep: a future PR adding a second fire-and-acknowledge RPC may
  bolt channel-specific fields onto `TaskAck` rather than introduce a
  new message, leading to optional fields whose meaning depends on the
  caller. Wire-shape archaeology becomes harder.
- Renaming after a second consumer attaches is a breaking proto change.

## Proposed fix / investigation path

Add a one-line proto comment above `message TaskAck` declaring intent:

```proto
// Generic ack reused only by fire-and-acknowledge RPCs whose response
// carries no payload beyond success/error. Channel-specific or
// RPC-specific reasons MUST go in a future `oneof reason` field rather
// than as additional scalar fields here. If a richer ack shape is
// needed for one caller, define a new message (e.g. ChannelMessageAck)
// rather than extending TaskAck.
```

Alternative (slightly heavier): rename to `ChannelMessageAck` while it
is still single-use, and let the next consumer either reuse it or
introduce its own. Renaming costs a regenerate cycle but eliminates the
ambiguity entirely. Defer the choice to the PR 4 reviewer.

## Notes

> 2026-05-04 — initial capture during PR #246 deep review (Should-fix
> #4). Pure documentation / naming concern; no runtime impact in v0.3.0.
