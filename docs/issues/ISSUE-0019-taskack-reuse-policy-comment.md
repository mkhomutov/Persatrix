---
id: ISSUE-0019
summary: TaskAck is named generically but used by exactly one RPC; add a proto reuse-policy comment to prevent scope-creep coupling
status: resolved
severity: low
area: build/proto
created: 2026-05-04
closed: 2026-05-08
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

## Resolution

> 2026-05-08 — resolved. Reuse-policy guidance added to the `TaskAck`
> doc-comment in
> [`proto/task.proto`](../../proto/task.proto): TaskAck stays generic;
> caller-specific reasons go in a future `oneof reason` field rather
> than as new scalar fields, and richer ack shapes for a single caller
> get their own message (e.g. `ChannelMessageAck`) rather than
> extending `TaskAck`. The rationale (renaming-after-second-consumer is
> a breaking change; bolted-on scalars create caller-dependent
> semantics) is preserved verbatim in the proto comment so future
> contributors hit it on any read of the message. The committed
> guidance ripples through both
> [`internal/generated/taskpb/task.pb.go`](../../internal/generated/taskpb/task.pb.go)
> and
> [`agents/generated/task_pb2.pyi`](../../agents/generated/task_pb2.pyi)
> via the standard `make proto` regen, so the policy surfaces in IDE
> hovers on both sides. No wire change; field numbers and types are
> untouched.
