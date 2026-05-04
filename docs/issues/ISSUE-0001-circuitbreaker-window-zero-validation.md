---
id: ISSUE-0001
summary: "CircuitBreaker silently disables threshold when Window: 0; add validator + Disabled flag"
status: open
severity: medium
area: security
created: 2026-05-04
refs:
  - docs/rfcs/0009-security-sandboxing.md
  - docs/rfcs/0009-pr-plan.md
  - docs/pr-reviews/pr-244-deep-review-r3.md
---

## Summary

`NewCircuitBreaker` accepts `ThresholdRule{Window: 0}` without complaint. A
zero-duration window makes `now.Add(-rule.Window) == now`, so the
`t.After(cutoff)` filter in `RecordViolation` drops every prior entry and the
rolling count never exceeds 1 — the breaker is permanently disabled for that
violation type.

## Context

- File: [internal/security/circuitbreaker.go](../../internal/security/circuitbreaker.go) — `NewCircuitBreaker`, `RecordViolation`.
- The `ThresholdRule` godoc mentions the invariant; nothing enforces it.
- Test code intentionally exploits `Count: 1, Window: 0` as a "trip on first
  call" seam (e.g. `breakerWithThreshold` in
  [internal/server/server_unquarantine_test.go](../../internal/server/server_unquarantine_test.go)),
  so a strict validator would break tests unless paired with an explicit opt-out.

## Impact

A misconfigured `cmd/orchestrator/ratelimit.go` env override (e.g.
`SECURITY_BREAKER_WINDOW=0s`) silently disables the per-violation-type
breaker. The failure mode only surfaces during an incident — when the breaker
is expected to quarantine a flooding agent and does not.

## Proposed fix / investigation path

1. Add a `Disabled bool` field to `ThresholdRule` for the test seam.
2. Have `NewCircuitBreaker` reject `Window <= 0` (and `Count <= 0`) for any
   non-`Disabled` threshold; return an error rather than panicking.
3. Migrate `breakerWithThreshold` and any other test sites to the new flag.
4. Add a unit test asserting `NewCircuitBreaker` rejects the bad config.

## Notes

> 2026-05-04 — captured during PR #244 deep review (R3, finding M-R3-02).
