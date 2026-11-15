---
id: ISSUE-0224
summary: "By its own polling intervals the web console sends up to about 76 requests a minute while it shows a group channel with three personas, against a default budget of 60 calls per 60 seconds for the console's shared bucket. Worked out from the code at first; measured on a running stack on 2026-11-15, where one window attempted 65 a minute and 5 a minute were refused."
status: resolved
severity: low
area: web
created: 2026-11-10
closed: 2026-11-15
closed_pr: 1130
refs:
  - web/src/panels/InteractionSummary.budget.test.js
  - web/src/panels/ChannelTimeline.budget.test.js
  - docs/issues/ISSUE-0259-two-console-windows-on-a-group-channel-exceed-the-default-rate-budget.md
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

> 2026-11-15 — measured, reproduced and fixed in the summary alone.
>
> **Measured on `main` at `dd2cceb1`**: the orchestrator as `make
> run-ui` starts it, default environment, no agents, the shipped
> `planning` channel in one visible window, two minutes after a
> 70-second warm-up, counted at the browser's network layer.
>
> | Endpoint | Requests | Refused (429) |
> |---|---|---|
> | Channel history | 32 | 4 |
> | Channel activity | 32 | 4 |
> | Closed interactions, three personas | 66 | 2 |
> | **All** | **130 (65 a minute)** | **10 (5 a minute)** |
>
> Sixty a minute were served, which is the budget. The "Live updates
> paused" notice was on screen for 36 of the 120 seconds. A second run
> gave 132 requests, 15 refused and 34 seconds. Two runs made for the
> review of this fix gave 130 requests with 12 refused, and the notice
> up for about 12 seconds: the totals repeat, and the time the notice
> shows depends on which reads the refusals fall on. The console
> attempts 65 a minute, not the 76 of the table above, because each
> refusal makes the timeline and the summary wait longer before their
> next read.
>
> **The fix.** The summary is read at a pace that costs the same on
> average whatever the size of the channel: each persona is asked once
> every ten seconds per persona asked. That is a round every 30 seconds
> for three personas and every 10 seconds for a DM's one: six requests
> a minute on average. After a round with a refused read the wait
> doubles, up to five minutes (the cap was 30 seconds), and never falls
> below the channel's own pace. While the tab is hidden nothing is
> read, and a tab shown again is read within five seconds.
>
> **Where "six a minute" stops.** It is an average. One round's reads
> are sent together, and the limiter counts a sliding 60 seconds: ten
> personas are asked ten at once every 100 seconds. Beside the
> timeline's 40 a minute, a round of more than 20 reads does not fit,
> so a channel of more than 20 personas has part of every round
> refused. From 30 personas up the channel's own pace is as long as the
> cap, and a refusal no longer stretches the wait. Asking one persona
> every ten seconds in turn would make the figure true at any size; it
> is an option in ISSUE-0259.
>
> **What an operator waits for.** A closed interaction's summary shows
> at the first round after it is stored: within 30 seconds on the
> shipped channels and 10 seconds in a DM, where it was 5. Opening the
> conversation, or a change of its members, reads at once and times
> the rounds afresh. All of that holds while the reads are served. A
> refused read stretches the wait as described above, and nothing on
> screen says that the summary is behind: "Live updates paused" is the
> timeline's notice.
>
> **Measured again, the same way, with the fix.**
>
> | Endpoint | Requests | Refused (429) |
> |---|---|---|
> | Channel history | 40 | 0 |
> | Channel activity | 40 | 0 |
> | Closed interactions, three personas | 12 | 0 |
> | **All** | **92 (46 a minute)** | **0** |
>
> The notice did not show. That leaves 14 requests a minute for a
> person's own clicks. The fix proposed for ISSUE-0222 in a pull
> request of its own would use some of them, once it is merged: up to
> three more history reads, once, when a hidden tab is shown again.
>
> **The limit of the fix.** Every console window shares one bucket
> while sign-in is off. Two windows on `planning` at once: 49 of 159
> requests refused before the fix, 18 of 136 after it, and the notice
> on screen for about 40% of the time in each window. Filed as
> [ISSUE-0259](ISSUE-0259-two-console-windows-on-a-group-channel-exceed-the-default-rate-budget.md).
>
> Not changed: the timeline's own 40 requests a minute, the limiter and
> its defaults. With no agent running every summary read answers 404;
> it is counted all the same.

> 2026-11-15 — after the review of the fix, in the same pull request.
>
> - **The rounds now follow the conversation.** The timer was started
>   once for the component, so the wait one conversation had reached
>   was served out in the next one. From a ten-persona channel to a
>   three-persona one, the first round came up to 99 seconds after the
>   switch, not 30. After four refused rounds and a switch, the new
>   conversation was not read again for up to 299 seconds. From a DM to
>   a ten-persona channel, a second full round followed the read on
>   opening within 9 seconds. Opening a conversation now reads it at
>   once and times its rounds from that moment; all three cases give
>   one round, one ordinary wait after the switch.
> - **Two windows for ten minutes**, measured with that change: 96 of
>   598 requests refused, all of them the timelines', and the notice on
>   screen for about half of the time in each window. The numbers and
>   what else a spent budget does are in ISSUE-0259.
> - The texts an operator reads now give the condition under which a
>   summary shows within one round, and the figures above that came
>   from one run say so.

