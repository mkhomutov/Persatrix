---
id: ISSUE-0248
summary: "A signed-in account that is not an operator gets `403` on an operator route, and every such request writes one `authz.denied` audit line that is synced to disk. The check runs outside the rate limiter, so nothing ever answers `429`: one account can make the orchestrator sync its audit log once per request for as long as it likes."
status: open
severity: low
area: security
created: 2026-11-14
refs:
  - internal/server/auth_middleware.go
  - internal/server/server.go
  - internal/security/audit_event.go
  - docs/issues/ISSUE-0208-every-anonymous-rest-call-writes-a-synced-audit-line.md
  - docs/issues/ISSUE-0245-the-audit-log-has-no-rotation-and-no-size-bound.md
  - https://github.com/mkhomutov/Persatrix/pull/1112
---

# ISSUE-0248: A refused operator route writes a synced audit line per request

## Summary

`enforcePolicy` (`internal/server/auth_middleware.go`) answers `403` to
a known account that is below the role a route needs, and writes an
`authz.denied` audit event for it. That event is security-class, so the
audit log syncs the file to disk after each one.

The auth middleware wraps the whole handler tree, outside the rate
limiter (`internal/server/server.go`): a request it refuses never
reaches the limiter. So these refusals are never counted, never
answered with `429`, and each one costs a line and a disk sync.

This is the shape of
[ISSUE-0208](ISSUE-0208-every-anonymous-rest-call-writes-a-synced-audit-line.md)
on another event.

## Context

Found in review of the ISSUE-0208 fix, with a throwaway test through
the server's handler: 500 requests to an operator route from a
signed-in non-operator, with a limiter of 30 calls a minute wired, gave
500 answers of `403` and 500 `authz.denied` lines. Read again from the
code when this was filed; the test was not re-run.

It needs `auth.mode: enabled` and an account, so it is not open to an
anonymous caller. The login routes have limiters of their own; this
path is after login.

## Impact

- One low-privilege account can hold the audit writer busy: every other
  audit event queues behind the syncs.
- The audit log has no rotation
  ([ISSUE-0245](ISSUE-0245-the-audit-log-has-no-rotation-and-no-size-bound.md)),
  so the lines also stay.

Proposed low: it needs a valid account, and the refusals are themselves
the record of who is doing it. The severity is proposed here, not
ruled.

## Proposed fix / investigation path

Throttle the event the way `rate_limit.violated` and
`rate_limit.unauthenticated_caller` are throttled: one line per account
and window, carrying the number of refusals it stands for. Or count
refused requests against the caller's budget in the limiter, so a
repeat offender gets `429` before the role check. The first keeps the
record; the second also bounds the work.

## Notes

> 2026-11-14 — filed from the review of the fix for ISSUE-0208.
