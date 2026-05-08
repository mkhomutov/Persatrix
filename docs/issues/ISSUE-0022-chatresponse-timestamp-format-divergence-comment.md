---
id: ISSUE-0022
summary: ChannelMessageEvent.timestamp uses RFC 3339 string while ChatResponse/TaskProgress use int64 epoch; add a cross-reference proto comment
status: resolved
severity: low
area: build/proto
created: 2026-05-04
closed: 2026-05-08
refs:
  - proto/task.proto
---

## Summary

`ChannelMessageEvent.timestamp` (added in PR #246) is a `string`
(RFC 3339) so that the value can be forwarded verbatim to the SQLite
`messages.created_at` column. Other timestamp fields in
[proto/task.proto](../../proto/task.proto) — `ChatResponse.timestamp`
and `TaskProgress.timestamp` — are `int64` Unix epoch. The divergence
is deliberate and justified in the `ChannelMessageEvent.timestamp` proto
comment, but `ChatResponse.timestamp` and `TaskProgress.timestamp` carry
no reciprocal note. A casual reader scanning either file will trip over
the inconsistency.

## Context

Captured during PR #246 deep review (Issues table M2 / Nice-to-have #3).

## Impact

- Reviewer surprise: future proto contributors reading
  `ChatResponse.timestamp` may "fix" the inconsistency by changing
  `ChannelMessageEvent.timestamp` to int64, breaking the SQLite
  forwarding rationale.
- Documentation drift over time as the rationale fades from memory.

## Proposed fix / investigation path

Add a one-line comment to each of `ChatResponse.timestamp` and
`TaskProgress.timestamp` in `proto/task.proto`:

```proto
// Unix epoch seconds. NOTE: ChannelMessageEvent.timestamp is RFC 3339
// string by deliberate exception (forwarded to SQLite created_at).
int64 timestamp = N;
```

Pure comment change; no wire impact.

## Notes

> 2026-05-04 — initial capture during PR #246 deep review (M2 / NTH-3).

## Resolution

> 2026-05-08 — resolved. Cross-reference comments added to both
> `ChatResponse.timestamp` and `TaskProgress.timestamp` in
> [`proto/task.proto`](../../proto/task.proto). Each carries the same
> note: the field is Unix epoch seconds, but `ChannelMessageEvent.timestamp`
> is RFC 3339 string by deliberate exception (forwarded verbatim to the
> channel store's `messages.created_at` column — see field doc). The
> "do not 'harmonise' the two without re-reading that rationale" line
> targets the exact failure mode this issue calls out (reviewer surprise
> followed by an int64-cast cleanup that breaks SQLite forwarding). The
> guidance ripples through
> [`internal/generated/taskpb/task.pb.go`](../../internal/generated/taskpb/task.pb.go)
> and
> [`agents/generated/task_pb2.pyi`](../../agents/generated/task_pb2.pyi)
> via `make proto`, so IDE hovers on both sides surface the cross-reference.
> Pure comment change; no wire impact, no field-number movement.
