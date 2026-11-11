---
id: ISSUE-0224
summary: "By its own polling intervals the web console sends up to about 76 requests a minute while it shows a group channel with three personas, against a default budget of 60 calls per 60 seconds for the console's shared bucket. Worked out from the code; not yet observed on a running stack."
status: open
severity: low
area: web
created: 2026-11-10
refs:
  - web/src/panels/ConversationFeed.svelte
  - web/src/panels/InteractionSummary.svelte
  - cmd/orchestrator/ratelimit.go
  - internal/security/middleware.go
  - internal/security/ratelimit.go
  - config/channels.yaml
---

# ISSUE-0224: Watching a group channel can exceed the console's default rate budget

## Summary

With the default `auth.mode: disabled`, every `/api/v1` data request from
the web console counts against one rate-limit bucket, kept under the
console's stable agent id (`rest:web-console`). Static files, the two
start-up endpoints and the sign-in routes are not counted. The default is
60 calls per 60 seconds (`defaultRateLimitCalls` and
`defaultRateLimitWindowSeconds` in `cmd/orchestrator/ratelimit.go`). The
console is exempt from quarantine but not from the limit.

## Context

What one open group channel sends:

| Source | Interval | Requests per minute |
|---|---|---|
| `ConversationFeed`: history and activity | every 3 s, two requests | 40 |
| `InteractionSummary`: one request per persona member | every 5 s | 12 per persona |

The shipped `planning` and `roundtable` channels have three personas, so
the total is up to about 76 a minute. That is an upper bound: each timer
starts its next wait only after the answer arrives. The limiter counts a
sliding 60-second window and does not count a call it refuses, so about
16 calls a minute would be refused. A second console window that is also
visible doubles the load; a hidden tab adds nothing, because both loops
stop while the page is hidden.

Both components back off when they get a 429, so the expected effect is
a recurring "Live updates paused" notice and slower updates, not a
failure.

This notice has been fixed once before.
[#592](https://github.com/mkhomutov/Persatrix/pull/592) gave the console
its own bucket after the notice showed during normal use. At that commit
the feed polled history only, so the total was 20 + 36 = 56 a minute,
inside the budget. [#596](https://github.com/mkhomutov/Persatrix/pull/596)
added the activity read to every tick, which makes it 76.

**Not observed since.** The numbers are read from the code. To confirm:
run `make run-ui` with the default environment, watch `planning` for two
minutes, and count the 429 answers in the access log.

## Impact

If confirmed, the default demo shows a rate-limit notice during normal
use, and the timeline updates more slowly than its three-second design.

## Proposed fix / investigation path

Fetch the interaction summary once per channel instead of once per
member, read the activity signal less often than the history, or give
the console a budget sized for its own polling. Confirm the effect on a
running stack first.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; the intervals and the budget were read from the code, and
> the effect was not reproduced.
