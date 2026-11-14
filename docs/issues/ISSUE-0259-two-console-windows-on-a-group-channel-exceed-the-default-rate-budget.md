---
id: ISSUE-0259
summary: "Two web console windows that both show a group channel are refused requests under the default rate budget: every console window spends from one bucket of 60 calls per 60 seconds while sign-in is off, and one window's timeline alone makes 40 a minute. Measured after the fix for ISSUE-0224: 18 of 136 requests refused in two minutes, and the `Live updates paused` notice up for about 40% of the time in each window."
status: open
severity: low
area: web
created: 2026-11-15
refs:
  - https://github.com/mkhomutov/Persatrix/pull/1130
  - web/src/panels/ConversationFeed.svelte
  - web/src/lib/http.js
  - cmd/orchestrator/ratelimit.go
  - internal/security/middleware.go
  - docs/issues/ISSUE-0224-console-polling-exceeds-its-default-rate-budget.md
---

# ISSUE-0259: Two console windows on a group channel exceed the default rate budget

## Summary

With `auth.mode: disabled`, every data request from every web console
window counts against one rate-limit bucket (the console's own agent id,
`web-console`), 60 calls per 60 seconds by default.

One window that shows a group channel makes 46 requests a minute since
the fix for
[ISSUE-0224](ISSUE-0224-console-polling-exceeds-its-default-rate-budget.md):

| Source | Requests a minute |
|---|---|
| The timeline: a history read every 3 s | 20 |
| The timeline: an activity read every 3 s | 20 |
| The interaction summary | 6 |

Two such windows need 92. A window in a background tab makes none.

## Context

Measured on a real orchestrator with the default environment and no
agents, two visible windows on the shipped `planning` channel, for two
minutes after a 70-second warm-up, counted at the browser's network
layer:

| | Requests | Refused (429) | "Live updates paused" on screen |
|---|---|---|---|
| Before the ISSUE-0224 fix | 159 | 49 | 75 s and 74 s of 120 s |
| After it | 136 | 18 | 48 s and 50 s of 120 s |

After the fix the summary reads were all served (24 of 24); the refused
requests were the timelines' (9 history reads and 9 activity reads of 56
each). Both timelines back off when refused, so each made 28 history
reads where an unhindered window makes 40.

Two more things seen in the same runs:

- **A window opened while the budget is spent may not start at all.**
  In two runs that started less than a minute after another run, the
  second window made no history read in the whole two minutes, while
  its summary reads went on. The likely cause was read from the code
  and not seen on screen: its first history read was refused, which
  leaves "Could not load history: …" with a **Retry** button, and the
  timeline polls only after a first page has loaded.
- Nothing on screen says that another window is the cause.

Not looked at: how the bucket is keyed under `auth.mode: enabled`.

## Impact

Two people at one deployment with sign-in off, or one person with two
windows, see the "Live updates paused" notice come and go, and their
timelines update more slowly than every three seconds. Nothing is lost:
the reads are retried.

## Proposed fix / investigation path

The choice is between the limiter and the console, and the limiter's
defaults are a security setting:

1. Size the console's budget for more than one window, or key its
   bucket per browser instead of one shared id
   (`cmd/orchestrator/ratelimit.go`, `internal/security/middleware.go`).
2. Make one window cheaper: read the activity less often than the
   history, or serve both from one request (a REST change), or stop the
   activity read while nobody is thinking.
3. Push instead of polling (the channel push API that RFC 0048 defers).

## Notes

> 2026-11-15 — found while measuring the fix for ISSUE-0224, which
> brings one window inside the budget and says plainly that two are not.
