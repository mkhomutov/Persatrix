---
id: ISSUE-0259
summary: "Two web console windows that both show a group channel are refused requests under the default rate budget: every console window spends from one bucket of 60 calls per 60 seconds while sign-in is off, and one window's timeline alone makes 40 a minute. Measured after the fix for ISSUE-0224: 96 of 598 requests refused in ten minutes, and the `Live updates paused` notice up for about half of the time in each window. With the budget spent a person's own post is refused too, and a window opened at that moment stops at an error until `Retry` is clicked."
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

In those two minutes after the fix the summary reads were all served
(24 of 24); the refused requests were the timelines' (9 history reads
and 9 activity reads of 56 each). Both timelines back off when refused,
so each made 28 history reads where an unhindered window makes 40.

**Two minutes are too short to judge by.** The same two windows for ten
minutes, with the fix as it stands after its review:

| | Ten minutes, two windows |
|---|---|
| Requests | 598 |
| Refused (429) | 96: 48 history reads and 48 activity reads |
| History reads per window | 120 and 119, where an unhindered window makes 200 |
| "Live updates paused" on screen | 312 s and 314 s of 600 s |
| Summary reads | 120, none refused, a round every 30 s in each window |

Whether a summary read is refused depends on how the two windows'
timers fall. In that run none was. In a ten-minute run made for the
review of the fix, 11 of 87 summary reads were refused: one window's
summary rounds were refused in 6 of 9, and its wait went to 120 s twice
and then to 240 s. A refused summary read doubles the wait before the
next one, up to five minutes, and nothing on screen says that the
summary is behind.

**What a spent budget does besides slowing the polls.** Probed on the
same orchestrator, with the console's budget spent:

- **A person's own post is refused too.** Every method counts against
  the bucket. A `POST` of a message as the console was answered `429`
  with `Retry-After: 60`, and nothing was stored. In the console the
  text stays in the message box and an error is shown; it has to be
  sent again.
- **A window opened at that moment does not start.** It showed "Could
  not load channels: /api/v1/channels?limit=1000 responded 429" with a
  **Retry** button, made no request at all in the next 70 seconds, and
  showed the same after them. It does not recover by itself: a click
  on **Retry** loaded the channels once the budget had room. In two
  earlier runs the channel list loaded and the first history read was
  the one refused: that window's timeline made no read for two
  minutes, which is the same stop one step later ("Could not load
  history: …" with **Retry**; the timeline polls only after a first
  page has loaded).
- The two start-up endpoints (`/api/v1/ui/config` and
  `/api/v1/ui/context`) are not counted, so the page itself always
  loads.
- Nothing on screen says that another window is the cause, and the
  refusal reads as a path and a number, not as "rate limit exceeded".

**With sign-in on** (`auth.mode: enabled`) the budget is kept per
signed-in account, not per console: the limiter's key is the account's
participant id (`sessionRateLimitKey` in
`internal/server/auth_middleware.go`), and the console's own
`X-Agent-ID` is ignored there (`restAgentID` in
`internal/server/agent_token_check.go`). Two people then have a budget
each. One person with two windows still shares one. Read from the
code, not run.

## Impact

Two people at one deployment with sign-in off, or one person with two
windows in any mode:

- see the "Live updates paused" notice for about half of the time, and
  their timelines update at about 60% of the three-second pace;
- can have a post refused, which they must send again;
- can open a window that shows only an error until they click
  **Retry**;
- can wait up to five minutes longer for an interaction's summary,
  with no sign of it.

No stored message is lost: the polls that are refused are made again,
and the timeline catches up.

## Proposed fix / investigation path

The choice is between the limiter and the console, and the limiter's
defaults are a security setting:

1. Size the console's budget for more than one window, or key its
   bucket per browser instead of one shared id
   (`cmd/orchestrator/ratelimit.go`, `internal/security/middleware.go`).
2. Make one window cheaper: read the activity less often than the
   history, or serve both from one request (a REST change), or stop the
   activity read while nobody is thinking.
3. Ask for the interaction summary one persona at a time: one read
   every ten seconds, each persona in turn. The summary's six requests
   a minute are an average today, because a round's reads are sent
   together; a channel with more than 20 personas has part of every
   round refused even in one window. In turn, the figure holds in
   every minute and at any size. The price is that one conversation's
   summaries are no longer read at one moment.
4. Push instead of polling (the channel push API that RFC 0048 defers).
5. Whatever is chosen, let a window that failed to load try again by
   itself, and let a refused post say that the budget is spent.

## Notes

> 2026-11-15 — found while measuring the fix for ISSUE-0224, which
> brings one window inside the budget and says plainly that two are not.

> 2026-11-15 — corrected after the review of that fix, which showed
> this file to understate. "Nothing is lost: the reads are retried" was
> wrong for a post and for a window opened while the budget is spent;
> both were then probed and are described above. "The summary reads
> were all served" held for two minutes and not for ten. How the bucket
> is keyed with sign-in on was not looked at; it has been read now.
> Severity `low` is proposed, not ruled. For `low`: a default
> deployment is one person on their own machine, nothing stored is
> lost, and every stop has a **Retry**. For `medium`: a second window
> is an ordinary thing to open, and it costs a refused post and a
> window that does not start.
