---
id: ISSUE-0031
summary: "selectChannelDispatcher silently returns NoopDispatcher when registry is nil — no Info log makes the disabled state visible"
status: resolved
severity: low
area: cmd/orchestrator
created: 2026-05-05
closed: 2026-05-07
closed_pr:
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
>
> 2026-05-07 — closed. `selectChannelDispatcher` emits an `Info` line on
> the nil-registry branch; `TestSelectChannelDispatcher_NilRegistryReturnsNoop`
> uses a `zap/zaptest/observer` to pin the message snippet, and
> `TestSelectChannelDispatcher_NonNilRegistryDoesNotLogDisabled` guards
> the negative case so a future refactor that lifts the log out of the
> nil branch is caught at test time.
