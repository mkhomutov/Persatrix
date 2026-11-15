---
id: ISSUE-0261
summary: "The security middleware answers a refused request with a nested error body (`error` holds an object with `code` and `message`), and the web console reads only the flat form the REST handlers write. So when the rate limiter refuses a console request, the notice shows the request's path and `responded 429` instead of `rate limit exceeded`."
status: open
severity: low
area: web
created: 2026-11-15
refs:
  - https://github.com/mkhomutov/Persatrix/pull/1128
  - web/src/lib/http.js
  - internal/security/middleware.go
  - internal/server/helpers.go
  - docs/issues/ISSUE-0224-console-polling-exceeds-its-default-rate-budget.md
---

# ISSUE-0261: The console cannot read the rate limiter's error body

## Summary

The orchestrator writes two shapes of error body:

- The REST handlers write a flat one, `{"error": "<text>", "code":
  "<CODE>"}` (`writeError` in `internal/server/helpers.go`).
- The security middleware writes a nested one, `{"error": {"code":
  "<CODE>", "message": "<text>"}}` (`writeJSONError` in
  `internal/security/middleware.go`). It does so for its three
  refusals: an invalid agent id (400), a quarantined agent (403) and a
  spent rate budget (429, `RATE_LIMITED`, "rate limit exceeded").

The console's client (`errorFromResponse` in `web/src/lib/http.js`)
takes `error` as the text only when it is a string, and reads `code`
from the top level. For the nested body it finds neither, and falls
back to the request's path and the status.

So the operator never reads "rate limit exceeded". A refused poll
shows, for example:

> Live updates paused: /api/v1/channels/planning/messages?limit=50
> responded 429

and a refused click on a hole marker shows the same path, with its
cursor, after "Could not load the missing messages:".

## Context

- On `main` at `dd2cceb1`. Read from the code; a throwaway test that
  feeds the client the limiter's body got the path-and-status text.
- The comment at the top of `web/src/lib/http.js` already quotes the
  banner as "… responded 429", so this text is what has been seen for
  some time.
- The console's tests answer a refused request with a text of their
  own, so none of them shows the real wording. One case of the pull
  request in `refs` now uses the text the client really produces.
- `code` stays undefined for these refusals. The panels that react to
  a refusal go by the status (429), so nothing misbehaves today.
- Not looked at: what the CLI and the Python agent client make of the
  nested body.

## Impact

Where a plain reason exists, the operator reads a URL with its query
string and a number. It matters most where refusals are common: more
than one console window on a group channel shares one request budget
(see
[ISSUE-0224](ISSUE-0224-console-polling-exceeds-its-default-rate-budget.md)).
The notice still appears and the read is still retried.

## Proposed fix / investigation path

Read both shapes in `errorFromResponse`: when `error` is an object,
take its `message` and `code`. That is a change in `web/` alone. The
other way is for the middleware to write the flat body; that changes
what every other client reads, so check the CLI and the agent client
first. Add a test that feeds the client the limiter's real body.

## Notes

> 2026-11-15 — found in the review of the fix for ISSUE-0222, whose
> description quoted "Live updates paused: rate limit exceeded" from a
> stand-in API. Severity `low` is proposed, not ruled: it is the
> wording of a notice that still does its job.
