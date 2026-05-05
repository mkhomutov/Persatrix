---
id: ISSUE-0031
summary: "selectChannelDispatcher silently returns NoopDispatcher when registry is nil — no Info log makes the disabled state visible"
status: open
severity: low
area: cmd/orchestrator
created: 2026-05-05
refs:
  - cmd/orchestrator/channels.go
---

## Summary

When `selectChannelDispatcher` is called with `reg == nil` it returns
`NoopDispatcher{}` without emitting a startup log line. The
"channels-cross-process delivery is disabled" state is then only
inferable from the absence of dispatcher logs.

## Context

Observed during PR #250 review (Nice-to-Have #2). A future refactor
that swaps init order in `cmd/orchestrator/main.go` (registry init
moved after channels init) would silently regress to the noop path.

## Impact

- Operators cannot tell from logs whether channels-cross-process is
  intentionally disabled or has degraded due to an init-order bug.
- Increases time-to-diagnose for a class of misconfiguration the
  initial PR already worked hard to make visible elsewhere
  (the auth-disabled WARN in `initChannels`).

## Proposed fix / investigation path

In `selectChannelDispatcher`, when `reg == nil` add:

```go
log.Info("channels: registry not available; cross-process dispatch disabled (NoopDispatcher in use)")
```

Pair the change with the test added by ISSUE-0028 (assert the log
line via `zaptest`/`observer`).

## Notes

> 2026-05-05 — initial capture during PR #250 review.
