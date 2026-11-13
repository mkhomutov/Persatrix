---
id: ISSUE-0208
summary: "Under the default `auth.mode: disabled`, every REST call with no agent id writes one audit line that is flushed to disk before the call continues, including calls the rate limiter then refuses. A flood the limiter rejects still costs one disk flush per request, and the first-party CLI, which sends no agent id, adds thousands of lines a day to a log that is never rotated."
status: resolved
severity: medium
area: security
created: 2026-11-10
closed: 2026-11-13
refs:
  - internal/security/ratelimit.go
  - internal/security/ratelimit_anonymous_event_test.go
  - internal/security/audit_event.go
  - internal/security/audit.go
  - internal/security/middleware.go
  - internal/server/auth_handlers.go
  - docs/issues/ISSUE-0152-one-quarantine-refuses-every-anonymous-caller.md
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

This is the default setup, `auth.mode: disabled`. Under `enabled` a
signed-in caller spends its own named budget and writes no such line, and
a call with no sign-in gets this far only on the routes that stay public.

## Context

- 500 anonymous `GET`s against a limit of 60 per 60 seconds gave 60
  successes, 440 refusals and 500 synced audit lines in 1.9 seconds.
- A comment in `internal/security/audit_event.go` says the cost of the
  per-event sync "is bounded by the rate limit itself". It is not: the
  line is written before the limiter decides.
- The CLI sends no `X-Agent-ID`. One `persatrix channel watch`, which
  polls every five seconds, adds about 17 000 lines a day.
  [ISSUE-0152](ISSUE-0152-one-quarantine-refuses-every-anonymous-caller.md)
  already tracks giving the CLI an id as the open half of its step 3,
  held by the Rust CLI freeze.
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
line. Whether the CLI should identify itself is ISSUE-0152's step 3.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; the counts come from a throwaway Go test against the
> middleware.

> 2026-11-13 — fixed by throttling. Checked against the code at
> `dd006cba` first: the Summary and Context hold as written, and the
> measurement reproduces (500 calls, 60 served, 440 refused, 500 lines in
> a real audit file).
>
> The limiter now writes at most one `rate_limit.unauthenticated_caller`
> line per window, as it does for `rate_limit.violated`. Each line
> carries `calls_since_last_event`: the calls with no agent id since the
> line before it, refused ones and this one included. So a refused flood
> costs one disk sync a window, and the lines still add up to the calls
> made, short of those after the last line.
>
> The other option, moving the event out of the security class, was not
> taken. It would have ended the sync per call, but kept one line per
> call: a log with no rotation would still gain about 17 000 lines a day
> from one `persatrix channel watch`, and every audit writer would still
> queue behind them. It would also have given up the reason the event
> is synced at all, which is that a flood is when a batched line is most
> likely to be lost in a crash.
>
> The throttle's state belongs to the limiter, not to the per-agent maps:
> those lose a key when its bucket is evicted or reset, and a flood of
> invented agent ids evicts the anonymous bucket at will.
>
> Unchanged: the CLI still sends no agent id (the open half of
> [ISSUE-0152](ISSUE-0152-one-quarantine-refuses-every-anonymous-caller.md)'s
> step 3), and the audit log still has no rotation.
