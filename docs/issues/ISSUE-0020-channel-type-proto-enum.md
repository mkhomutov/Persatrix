---
id: ISSUE-0020
summary: ChannelMessageEvent.channel_type is a string with a closed value set ({group, dm, thread}); promote to proto enum
status: open
severity: low
area: build/proto
created: 2026-05-04
refs:
  - proto/task.proto
  - internal/channels/channels.go
  - docs/issues/ISSUE-0018-channel-message-event-receiver-bounds-enforcement.md
---

## Summary

`ChannelMessageEvent.channel_type` (added in PR #246,
[proto/task.proto](../../proto/task.proto)) is declared `string` whose
value space is exactly `{group, dm, thread}` — mirroring the
`ChannelType` const block in
[internal/channels/channels.go](../../internal/channels/channels.go).
A proto enum would collapse the membership check from N receiver-side
string comparisons to a single language-agnostic schema constraint.

## Context

Captured during PR #246 deep review (Should-fix #1 / Issues table M1
and Nice-to-have #2). The current proto doc-comment lists the three
values as a receiver MUST and pairs them with the
`channel_id`-prefix invariant (orchestrator validates on publish,
receivers MUST drop on mismatch).

## Impact

- Each receiver (Python, Go, future Rust) re-implements the same
  three-value validation. Enforcement is a runtime check rather than a
  schema constraint.
- 4th-value drift risk: a future code path that adds a new `ChannelType`
  in Go without coordinating with Python receivers will silently route
  to the default arm of every receiver switch.
- Counter-argument (why deferred in PR 3): adding a bridge-only
  `channel_type` in v0.5 may want forward-compat behavior that
  `string` provides freely; an enum requires a `UNKNOWN = 0` sentinel
  and a documented migration path.

## Proposed fix / investigation path

1. Define `enum ChannelType { CHANNEL_TYPE_UNSPECIFIED = 0;
   CHANNEL_TYPE_GROUP = 1; CHANNEL_TYPE_DM = 2; CHANNEL_TYPE_THREAD = 3; }`
   in `proto/task.proto`.
2. Replace `ChannelMessageEvent.channel_type` (string) with the enum.
   This is a wire-incompatible change — schedule for a phase boundary
   (RFC 0011 PR 4 or PR 5) before any external consumers attach.
3. Update `internal/channels/router.go` to map the orchestrator-side
   `ChannelType` const to the proto enum on publish; receivers reject
   `CHANNEL_TYPE_UNSPECIFIED`.
4. Drop the corresponding string-validation half from
   [ISSUE-0018](ISSUE-0018-channel-message-event-receiver-bounds-enforcement.md).

## Notes

> 2026-05-04 — initial capture during PR #246 deep review (M1 + NTH-2).
> Defer to PR 4 or PR 5 design discussion; not a v0.3.0 blocker.
