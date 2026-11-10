---
id: ISSUE-0208
summary: "Every REST call with no agent id writes one audit line that is flushed to disk before the call continues, including calls the rate limiter then refuses. A flood the limiter rejects still costs one disk flush per request, and the first-party CLI, which sends no agent id, adds thousands of lines a day to a log that is never rotated."
status: open
severity: medium
area: security
created: 2026-11-10
refs:
  - internal/security/ratelimit.go
  - internal/security/audit_event.go
  - internal/security/audit.go
  - internal/security/middleware.go
  - internal/server/auth_handlers.go
---

# ISSUE-0208: Every anonymous REST call writes one synced audit line, refused calls included

## Summary

`RateLimiter.Allow` (`internal/security/ratelimit.go`) emits a
`rate_limit.unauthenticated_caller` audit event for every call that
carries no agent id. It does so before it uses the allow-or-deny result.
The event is in the security class (`internal/security/audit_event.go`),
and the audit logger syncs the file after each security-class line while
holding its mutex.

The neighbouring `rate_limit.violated` event is throttled to one per
window. This one is not.

## Context

- 500 anonymous `GET`s against a limit of 60 per 60 seconds gave 60
  successes, 440 refusals and 500 synced audit lines in 1.9 seconds.
- The CLI sends no `X-Agent-ID`. One `persatrix channel watch`, which
  polls every five seconds, adds about 17 000 lines a day.
- The audit log has no rotation.
- A comment in `internal/server/auth_handlers.go` acknowledges the
  per-call line; no issue tracked it.

## Impact

- The rate limiter does not protect the disk: a refused request still
  costs a flush, and every other audit writer waits behind it.
- On a normal single-user setup the audit log fills with lines that say
  only that the CLI was used, which buries the events that matter.

## Proposed fix / investigation path

Throttle this event per caller and window, as `rate_limit.violated`
already is, or move it out of the security class so it is not synced per
line. Decide separately whether the CLI should identify itself.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; the counts come from a throwaway Go test against the
> middleware.
