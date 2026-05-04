---
id: ISSUE-0009
summary: channelFallbackWarnOnce package-level sync.Once is reassigned from tests; latent -race flake
status: resolved
severity: medium
area: internal/server
created: 2026-05-04
closed: 2026-05-04
closed_pr: 246
refs:
  - docs/rfcs/0011-channels.md
  - docs/rfcs/0011-pr-plan.md
---

## Summary

`channelFallbackWarnOnce` is a package-level `sync.Once` in
`internal/server/channel_handlers.go`. The review tests in
`internal/server/channel_handlers_review_test.go` reset it via
`channelFallbackWarnOnce = sync.Once{}` (and again in `t.Cleanup`).
Reassigning a package-level value from tests is a data race the moment any
test in `internal/server` adopts `t.Parallel()` and will be flagged by
`go test -race`.

## Context

Captured during PR #245 deep review (Should-Fix #1). The router-nil
fallback path emits a once-per-process WARN that signposts a missing
`channel_type` cross-validation; the test needs to verify the once-only
behaviour and therefore needs to reset the gate between cases.

Files:
- `internal/server/channel_handlers.go` — declares the var.
- `internal/server/channel_handlers_review_test.go` — reassigns it.

## Impact

- No production race today (single goroutine path), but `-race` will trip
  the moment any sibling test in `internal/server` opts into `t.Parallel()`.
- Encourages a bad pattern (test mutation of package-level sync primitives)
  that other reviewers may copy.

## Proposed fix / investigation path

Pick one:

1. Move the `sync.Once` onto the `Server` struct so each test constructs a
   fresh instance and no package-level mutation is needed. Preferred —
   matches the `Server`-scoped lifetime of the warning.
2. Wrap the once in a small helper exposing `Reset()` guarded by an
   internal mutex; tests call the helper instead of reassigning.
3. Replace with `atomic.Pointer[sync.Once]` and swap atomically.

Once removed, the router-nil fallback may itself be removable in PR 4 when
the router becomes mandatory — at which point both the var and the test
disappear.

## Notes

> 2026-05-04 — initial capture during PR #245 review (Should-Fix #1).
