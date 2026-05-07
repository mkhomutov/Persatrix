---
id: ISSUE-0007
summary: "RateLimiter/CircuitBreaker audit emits use context.Background(); plumb request context for trace correlation"
status: resolved
severity: low
area: security
created: 2026-05-04
closed: 2026-05-07
refs:
  - docs/rfcs/0009-security-sandboxing.md
  - docs/observability.md
---

## Summary

`RateLimiter.emit` and `CircuitBreaker.emit` call the auditor with
`context.Background()`. Trace IDs from the inbound request are dropped,
breaking trace correlation for `rate_limit.throttled` and
`circuit_breaker.opened` events.

## Context

- Files: [internal/security/ratelimit.go](../../internal/security/ratelimit.go),
  [internal/security/circuitbreaker.go](../../internal/security/circuitbreaker.go).
- Compare `Server.emitAudit`, which uses `context.WithoutCancel(r.Context())`
  precisely to keep trace metadata propagating.
- `Allow()` and `RecordViolation()` do not currently take a `context.Context`.

## Impact

Observability — quarantine and throttle events cannot be correlated to the
triggering request span. No security or correctness impact.

## Proposed fix / investigation path

Add a `context.Context` parameter to `Allow` and `RecordViolation` (and to
the gRPC / REST middleware call sites). Use `context.WithoutCancel(ctx)`
when handing off to the auditor so the emit is not cancelled if the request
is.

Aligns with the PR 4 follow-up scope.

## Notes

> 2026-05-04 — captured during PR #244 deep review (R3, finding N-R3-01).
>
> 2026-05-07 — resolved. `RateLimiter.Allow`, `CircuitBreaker.RecordViolation`,
> and `CircuitBreaker.Unquarantine` now take a leading `ctx context.Context`.
> Both `emit` helpers detach the parent ctx via `context.WithoutCancel(ctx)`
> before handing off to the auditor, mirroring `Server.emitAudit`. Nil ctx
> falls back to `context.Background()` for non-request paths (background
> sweeps, tests). REST and gRPC middleware now thread `r.Context()` /
> the inbound gRPC ctx through. Pinned by four new unit tests:
> `TestRateLimiter_AllowPropagatesRequestCtxToAuditor`,
> `TestRateLimiter_AuditEmitNotCancelledWithRequestCtx`,
> `TestCircuitBreaker_RecordViolationPropagatesCtxToAuditor`,
> `TestCircuitBreaker_AuditEmitNotCancelledWithRequestCtx`.
