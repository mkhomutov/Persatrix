---
id: ISSUE-0156
summary: "A standing autonomous channel must declare an aggregate bound (RFC 0052 §E), but `validate` checks that only on the channel's `schedule_interval_seconds`, which starts nothing: the timer that fires is a hand-written `kind: convene` entry in the convener's agents.yaml, and `ConveneChannel` cannot tell its call from a person's and applies each ceiling only when it is positive. So a convene timer aimed at an armed channel with no bound opens one discussion after another, each capped, the total held only by the fleet's dollar budget. Also tracked: nothing arms timers from channel config (the producer and the writer have no production caller), nothing reports standing spend, and the force-fresh / two-openers race had no issue"
status: open
severity: medium
area: channels
created: 2026-09-11
refs:
  - https://github.com/mkhomutov/Persatrix/pull/930
  - docs/rfcs/0052-autonomous-agent-channels.md
  - docs/rfcs/0052-pr-plan.md
  - docs/guides/autonomous-channels.md
  - docs/manual-tests/v0.3.11-execution-report.md
  - docs/v0.3.11-release-checklist.md
  - docs/v0.3.11-release-prep-plan.md
  - internal/channels/convene.go
  - internal/channels/config_validate.go
  - internal/channels/config_autonomous.go
  - internal/channels/config_autonomous_standing.go
  - internal/channels/convening_counter.go
  - internal/channels/standing_budget.go
  - internal/channels/standing_schedule.go
  - internal/server/auth_policy.go
  - internal/server/channel_convene_handlers.go
  - internal/server/channel_config_autonomous.go
  - internal/server/channel_types.go
  - agents/tick.py
  - agents/convene_client.py
  - agents/convene_timer_writer.py
  - agents/server_persona_timers.py
---

## Summary

An autonomous channel can run on a schedule: a timer on its convener opens a
new discussion every interval, with no one pressing Convene. RFC 0052 §E makes
such a **standing** channel declare an aggregate bound — a maximum number of
convenings (`max_convenings`), a total token budget (`standing_budget_tokens`),
or both — because the per-discussion cap limits each discussion, not how many
the timer starts.

The check sits on the wrong object. `validate` demands a bound when the
channel's `autonomous.schedule_interval_seconds` is positive, but that setting
starts nothing. What fires is a timer entry in the convener's
`config/agents.yaml`, and the convene endpoint cannot tell that timer's call
from a person's. So a timer aimed at an armed channel that declares no bound
opens one discussion after another for as long as it runs: each discussion is
capped, the total is not. With priced models, only the orchestrator's dollar
budget stops it in the end, by refusing every call that budget covers.

Three smaller gaps sit next to it and are tracked here too:

- **Nothing arms the timer.** The Go producer and the Python writer that would
  turn channel config into convener timers have no production caller, so every
  standing schedule is a hand edit plus a restart.
- **Nothing reports standing spend.** The config read surface shows the
  convening count only, so a channel bounded by spend alone hits `429` with no
  warning.
- **Force-fresh had no issue.** The v0.3.11 release documents call the
  two-openers race a tracked follow-up, but nothing tracked it, and the code
  now says no plan does.

## Context

Line numbers are at `7ec52ac1`.

### Gap 1 — the bound is checked on a setting that starts nothing (medium)

**The rule.** RFC 0052 Goal 5 says a standing channel cannot be created
without an aggregate bound, rejected at config-validation time
([RFC 0052](../rfcs/0052-autonomous-agent-channels.md#goals) line 84; §E,
line 197). The code checks it in two places, both through
`standingBoundMissing`
(`internal/channels/config_autonomous_standing.go:53`–`55`), which is true
only when `schedule_interval_seconds` is above zero:

- loading `config/channels.yaml`: `Config.Validate`
  (`internal/channels/config_validate.go:306`–`309`);
- a config edit: `validateAutonomous`
  (`internal/channels/config_autonomous.go:457`–`460`).

A channel left at `0` is one-shot and needs no bound. That is by design: a
person convenes it, and each convening is a deliberate act.

**The timer.** Timers come from each persona's `autonomy.timers` list in
`config/agents.yaml`, read only when its `autonomy.level` is
`semi-autonomous` or `autonomous` (`agents/server_persona.py:389`, `:393`,
`:447`–`450`). A convene timer is an entry with `kind: convene` and
`id: convene-<channel name>`. When it fires, `TickScheduler` hands it to
`_handle_convene_wake` (`agents/tick.py:267`–`268`, `:319`), which reads the
channel from the timer id and calls `HTTPConveneClient.convene` (`:357`). That
sends `POST /api/v1/channels/{id}/convene` with no body and nothing that marks
it as a timer's call (`agents/convene_client.py:108`–`110`). The agent schema
accepts any `interval_seconds` from `1.0` up
(`schemas/agent.schema.json:317`, `agents/event_loop.py:108`), and nothing in
it refers to the channel's settings.

**The endpoint.** `ChannelRouter.ConveneChannel`
(`internal/channels/convene.go:137`) checks that the channel exists, is armed
and idle, and has a convener, an audience, a chair and a topic. It never reads
`ScheduleIntervalSeconds`. It applies each aggregate ceiling only when that
ceiling is positive: spend through `standingBudgetReached` (`convene.go:241`;
`internal/channels/standing_budget.go:113`–`114`) and count through
`reserveConvening` (`convene.go:258`;
`internal/channels/convening_counter.go:92`).

**Put together.** Take an armed channel with `schedule_interval_seconds: 0`
and neither bound — valid, since it is one-shot — and add a convene timer for
it to the convener's `agents.yaml`. Nothing compares the two. Every fire
passes every check; the convening count still rises, but no ceiling stops it.
A fire during a running discussion gets `409` (`convene.go:155`–`156`), so
discussions follow one another instead of overlapping: a new one opens at the
first fire after the last one closes, for as long as the timer runs. (A
discussion that fizzles out without a close keeps the channel busy, so later
fires get `409` too.) A milder form reaches bounded channels. The timer's own
`interval_seconds` sets the pace, not the channel's, so a timer faster than
the declared schedule uses up the bound sooner than planned.

**Any caller, not only a timer.** The convene route is `policyPublic`
(`internal/server/auth_policy.go:73`, commented "convene timer callback"):
agents hold no accounts, so the fleet's REST calls stay open when auth is
turned on (`:14`–`20`). The route's only gate of its own is
`config_edit_enabled` (`internal/server/channel_convene_handlers.go:52`),
which the bundled `config/ui.yaml` sets to `true` (`:50`). The rate limiter in
front of every `/api/v1` route (`internal/server/server.go:397`–`398`) paces
the calls but does not limit how many discussions they open. So any client
that can reach the orchestrator and calls the endpoint in a loop gets the same
result as the timer.

**What the comments promise.** Four places say a timer's convening meets the
same §E ceilings a person's does (`agents/convene_client.py:16`–`21`,
`agents/convene_timer_writer.py:11`–`14`,
`internal/channels/standing_schedule.go:21`–`22`, `agents/tick.py:325`–`327`),
and the first two add that the timer "must never bypass" them. That holds for
a channel that declares a bound. For one that declares none, there is nothing
to meet. Two recent changes say so:
[#930](https://github.com/mkhomutov/Persatrix/pull/930) added an operator
warning, now in the
[autonomous channels guide](../guides/autonomous-channels.md#standing-channels--convening-on-a-schedule)
("Do step 1 first"), and [#935](https://github.com/mkhomutov/Persatrix/pull/935)
wrote the gap into the `standing_schedule.go` header: a hand-written timer
"skips [deriveConveneTimer]'s bound check" (`:29`–`31`).

### Gap 2 — nothing arms the timer from channel config (low)

RFC 0052 chose a config round-trip: a channel's schedule reaches its
convener as an `autonomy.timers` entry written into the convener's
`agents.yaml`. §E sets out the options (line 196), the open-question
resolutions record the choice (line 267), and Phase 3 includes "the
channel-config→convener-timer wiring" (line 232). Both halves shipped, and
neither is called:

- the producer, `ChannelRouter.StandingConveneTimers`
  (`internal/channels/standing_schedule.go:210`), whose file header says
  "Nothing consumes" it yet and "no production code arms a timer"
  (`:25`–`26`);
- the writer, `merge_convene_timers` (`agents/convene_timer_writer.py:182`),
  whose docstring says "nothing calls this in-tree yet" (`:86`–`88`).

Outside tests, no Go, Python, Rust or TypeScript code calls either, and the
`wire_convene_clients` docstring says the writer "has no production caller"
(`agents/server_persona_timers.py:62`–`63`). So every standing schedule is a hand
edit to `agents.yaml` and a persona restart. The v0.3.11 release-prep run did
exactly that: MT-AUTONOMOUS-003 Step 2 ran the real writer, pasted its output
into `config/agents.yaml` by hand, and reverted it after the run
(`docs/manual-tests/v0.3.11-execution-report.md:110`, `:115`).

The [RFC 0052 PR plan](../rfcs/0052-pr-plan.md) marks the writer slice,
7c-ii-b, merged. Its residuals cover the per-process bounds (`:120`) and the
dropped first fire (`:122`), but none covered wiring the round-trip, and no
issue did either.

The producer already refuses an unbounded channel: `deriveConveneTimer`
yields no timer when both bounds are zero (`standing_schedule.go:188`–`190`).
A wired round-trip would therefore close gap 1 for every timer it writes.

### Gap 3 — nothing reports standing spend (low)

`GET …/config` carries an `autonomous_runtime` block with two fields,
`convening_count` and `convenings_remaining`
(`internal/server/channel_types.go:365`–`368`, filled by `autonomousRuntime`
in `internal/server/channel_config_autonomous.go:171`–`181`).
`ChannelRouter.StandingSpend` (`standing_budget.go:103`) holds the running
total the spend ceiling measures. Its comment says the config readout "does
not include it" (`:100`–`101`), and nothing outside `internal/channels`
calls it. So a
channel bounded only by `standing_budget_tokens` reaches its `429` with no
warning, as the
[autonomous channels guide](../guides/autonomous-channels.md) now says.

### Gap 4 — force-fresh and the two-openers race (info)

`ConveneChannel` refuses a channel with a live discussion. Until the
convener's first reply commits, though, the channel still looks idle, so two
convenes can both send an opener into what becomes one discussion. The code
names the missing piece: PR 7 "never built a force-fresh convene", "no plan
tracks one", and "nothing closes that two-openers race" (`convene.go:29`–`37`,
wording from [#935](https://github.com/mkhomutov/Persatrix/pull/935)); the
race is "NOT closed" (`convening_counter.go:50`–`51`). The v0.3.11 changelog
calls it "still deferred" (`CHANGELOG.md:248`), and the v0.3.11 release
checklist lists "the two-openers idle race" among its "tracked follow-ups"
(`docs/v0.3.11-release-checklist.md:112`), as does the release-prep plan
(`docs/v0.3.11-release-prep-plan.md:26`). No issue or PR-plan residual
tracked it.

With a count bound the race is safe: it can only use up `max_convenings`
early. Without one, nothing caps the extra openers, each an uncapped call,
and a timer whose interval is shorter than the convener's reply time can land
a second convene inside that window.

## Impact

- **Spend (gap 1).** Each discussion stays under its
  `interaction_budget_tokens` cap, which every armed channel must set
  (`config_validate.go:283`–`284`, `config_autonomous.go:423`–`425`). Each
  convening also makes one uncapped call, the convener's opener, whose lease
  comes before the cap applies (RFC 0052 §B, line 150). Nothing in the
  channel limits how many discussions the timer starts. What stops the spend
  in the end is the orchestrator's dollar budgets
  (`config/optimization.yaml:118`–`126`: stock $100 in all, $10 per workflow
  and $5 per agent), checked on every lease (`internal/wallet/wallet.go:215`)
  and never reset while the orchestrator runs (nothing calls `ResetDaily`;
  see `cmd/orchestrator/main.go:240`). Persona calls carry no workflow id
  (`agents/llm_client.py:196`), so all of them count against one shared
  workflow total (`wallet.go:252`–`253`, `internal/cost/cost.go:265`–`275`),
  whose $10 limit then covers every persona's calls together. The budgets
  stop the loop bluntly: once one is spent, every model call it covers is
  refused, on every channel. With unpriced local models (`mock`, `ollama`)
  they never bind.
- **Memory.** The wallet keeps an entry per discussion that nothing removes,
  and today only the aggregate bound keeps that list finite (RFC 0052 §E,
  line 198; PR plan `:130`).
- **Who can reach it.** An operator who writes a convene timer without doing
  the guide's step 1, or who stops a schedule by setting the interval back to
  `0` and clearing the bound but leaves the timer in place (`0` is a valid
  one-shot setting, `schemas/channel.schema.json:214`); and any client that
  can reach the orchestrator's REST port while `config_edit_enabled` is on,
  the shipped default. The stock compose file publishes that port on
  `127.0.0.1` only (`docker-compose.yaml:91`), which limits the second group
  to the host and the other containers on the compose network.
- **Why medium.** The review process counts "a gate that cannot catch what it
  claims" as medium ([review process](../methodology/review-process.md#severity)),
  and the §E gate is one. It is not high: every discussion stays capped,
  discussions run one at a time, a priced fleet's dollar budget stops it in
  the end, and reaching the loop takes an operator's config mistake or
  network access to the orchestrator.
- **Gaps 2–4** add no spend on their own. They cost operator effort (every
  schedule is a hand edit), a surprise stop (a spend-bounded channel ends
  without warning), and an untracked race.

**Workaround until a fix lands.** Give every armed channel a
`max_convenings` or a `standing_budget_tokens`, one-shot channels included.
Validation rejects only negative values, on load
(`config_autonomous.go:200`–`205`) and on edit (`:319`–`324`), and
`ConveneChannel` enforces either bound whenever it is positive, so a stray
timer or a looping client stops at the bound — until the orchestrator
restarts, which resets both totals (PR plan `:120`). The bound also counts a
person's convenings, so leave room for them, and raise it with
`persatrix channel config set` when it runs out; the web panel does not edit
these settings. Setting `config_edit_enabled: false` closes the endpoint too,
but it also stops every standing schedule and all config editing.

## Proposed fix / investigation path

The candidates are not ranked here; the choice belongs to slotting, and they
can be combined.

1. **Wire the round-trip.** Derive the timer set with `StandingConveneTimers`
   and write it with `merge_convene_timers`, at boot, on a channel-config
   change, or both. The channel setting then becomes the one source of the
   schedule, and since the producer refuses an unbounded channel, no written
   timer can skip the bound. The writer also reconciles: a convene entry with
   no matching channel is dropped (`convene_timer_writer.py:43`–`48`), so a
   run over every persona would remove a hand-written timer aimed at an
   unbounded channel. Open points: the producer is Go in the orchestrator and
   the writer is Python against the persona's file, so something must carry
   the timer list across (an orchestrator endpoint the persona reads at boot,
   or an operator CLI step); a level bump needs the persona restarted, as
   MT-AUTONOMOUS-003 did; and the dropped first fire (PR plan `:122`), which
   already hits hand-written timers, would hit every derived one, so its fix
   belongs in the same change. A hand edit made after the round-trip, and a
   client calling the endpoint directly, stay unchecked.
2. **Tell a timer from a person.** Have `HTTPConveneClient` mark its call — a
   request header, say — and have `ConveneChannel` refuse a marked call for a
   channel that declares no bound. That covers every timer, hand-written or
   derived, and leaves a person's convening of a one-shot channel as it is.
   The mark is not a credential: the route is public, so a client that leaves
   it off passes as a person, and the looping client stays possible until
   agents can prove who they are (RFC 0009 agent tokens,
   `auth_policy.go:17`–`19`). It also adds to the contract between persona
   and orchestrator, so it needs a test on each side.
3. **Report standing spend.** Add the spend total and the budget left to
   `autonomous_runtime` (`channel_types.go:365`), beside the convening count,
   and show them where the count is shown: `persatrix channel config get`
   (`cli/src/commands/channel_config_autonomous.rs`,
   `cli/src/commands/channel_config_render.rs`) and the web *Autonomous
   channel* panel (`web/src/panels/AutonomousSettings.svelte`). This closes
   gap 3 only.

Gap 4 has no candidate yet beyond the piece the code names, a force-fresh
convene. It is tracked here so the release documents' "tracked follow-up"
has a home. Candidate 2 would also keep timers on unbounded channels out of
the race, since their convenings would be refused.

Whichever lands:

- write the failing test first — for example, a timer's convening of an
  unbounded channel refused (candidate 2), or a round-trip that writes no
  timer for one (candidate 1);
- correct the comments the change makes untrue: the four listed under gap
  1, the writer's "nothing calls this in-tree yet", and the text #935 wrote
  to describe today's hand-written timer (the `standing_schedule.go` header,
  the `wire_convene_clients` docstring, and the `convene.go` header if
  force-fresh lands);
- update the operator guide's
  [standing-schedule instructions](../guides/autonomous-channels.md#standing-channels--convening-on-a-schedule),
  which today tell operators to write the timer by hand and warn about gaps 1
  and 3.

**Slot.** This issue is docs only and can merge now. A code fix is outside
v0.3.16's scope as ratified by the
[2026-08-19 sequencing amendment](../v0.3.x-sequencing.md#amendment-2026-08-19--v0315--v0316-attribution-and-audience-before-the-v040-train),
so the next amendment slots it. Under the
[version-train gate](../methodology/process-glossary.md#version-train-gate),
a fix slotted for a later version can be written and reviewed now but merges
only after v0.3.16 is tagged.

## Notes

> 2026-09-11 — filed from a code reading at `604fab72`; every file and line
> named above was checked there. Docs only: no code changes. Found by the
> review of [#930](https://github.com/mkhomutov/Persatrix/pull/930), which
> rewrote the channels guide's standing-schedule section; its body records
> gap 1 as F-3, gap 2 as F-4 and gap 4 as F-5, and folds gap 3 into F-3 under
> F-10. [#935](https://github.com/mkhomutov/Persatrix/pull/935), the comment
> follow-up from the same review, merged while this was drafted and rewrote
> several comments quoted above; the quotes are its wording. The RFC 0052
> PR plan's residual list now links here.
