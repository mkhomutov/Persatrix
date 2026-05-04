---
id: ISSUE-0022
summary: ChannelMessageEvent.timestamp uses RFC 3339 string while ChatResponse/TaskProgress use int64 epoch; add a cross-reference proto comment
status: open
severity: low
area: build/proto
created: 2026-05-04
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
