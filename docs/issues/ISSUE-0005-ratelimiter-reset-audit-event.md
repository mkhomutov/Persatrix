---
id: ISSUE-0005
summary: "RateLimiter.Reset() does not emit an audit event; add rate_limit.reset when surfaced to operators"
status: resolved
severity: low
area: security
created: 2026-05-04
closed: 2026-05-08
refs:
  - docs/rfcs/0009-security-sandboxing.md
  - docs/rfcs/0009-pr-plan.md
---

## Summary

`RateLimiter.Reset()` is currently called only by tests, but the public API
invites future production use (e.g. an operator endpoint analogous to the
unquarantine handler). Once that exists, a `rate_limit.reset` audit event
should fire, mirroring `agent.unquarantined`.

## Context

- File: [internal/security/ratelimit.go](../../internal/security/ratelimit.go) → `Reset()`.
- Audit-event registry: [internal/security/audit_event.go](../../internal/security/audit_event.go).

## Impact

No current operational impact. Pre-emptive consistency for the
to-be-implemented executor / operator endpoints in PR 4.

## Proposed fix / investigation path

Pick up alongside the executor endpoint work in RFC-0009 PR 4: register
`rate_limit.reset` in the audit-event registry and emit from `Reset()`.

## Notes

> 2026-05-04 — captured during PR #244 deep review (R3, finding L-R3-04).

> 2026-05-08 — resolved on `feature/v030-rfc0009-issue0005-ratelimit-reset-audit`.
> `RateLimiter.Reset` now takes `(ctx, agentID, actor)`, returns `bool`,
> and emits `rate_limit.reset` (security-class, fsync'd) when an actual
> reset occurs. Mirrors `CircuitBreaker.Unquarantine` →
> `agent.unquarantined`. The pre-emptive consistency promised here is
> now in place, so the future operator endpoint can wire through Reset
> without further audit plumbing.
