---
id: ISSUE-0161
summary: "On a booted offline roundtable, a persona that answered a turn it was handed without a bid with the do_nothing action (silence) left the discussion open with nothing further dispatched for twenty minutes — no floor-turn timeout recorded, no stall, no convener advance, no chair escalation, no bounded close, no idle rotation. The floor metrics stopped at the first round (2 turns replied, 1 round, no timeout) although a reply in the second round was persisted. An in-process router test with a silent second speaker, with and without reply mentions, closes normally, so the cause is not isolated. A real model may stay silent on such a turn: the reply-discretion prompt invites silence in group channels."
status: open
severity: medium
area: channels
created: 2026-09-15
refs:
  - internal/channels/autonomous_continuation.go
  - internal/channels/floor_control.go
  - internal/channels/fanout.go
  - agents/persona_runtime/channel_reply.py
  - prompts/runtime/safety/reply-discretion.md
  - docs/issues/ISSUE-0110-autonomous-productive-round-continuation-stall.md
  - docs/issues/ISSUE-0160-offline-roundtable-only-the-convener-speaks.md
---

# ISSUE-0161: A silent reply on a handed-over autonomous turn left a booted discussion open with nothing dispatched

## Summary

While fixing [ISSUE-0160](ISSUE-0160-offline-roundtable-only-the-convener-speaks.md),
one variant of the offline mock answered a turn it had no new point for with
the persona action schema's `do_nothing` — silence. On the booted
`make demo-autonomous` stack the `roundtable` then stopped after four
messages: the convener's opener, both participants' first replies, and the
convener's reply to one of them (cascade depth 3). The orchestrator logged the
autonomous continuation re-fanning a participant's reply at 12:59:31 UTC, the
second participant fetched its window for the turn and stayed silent, and
nothing further happened for the twenty minutes the run was watched.

## Context

What the stack showed during those twenty minutes:

- No `POST` to the channel and no agent fetch after the silent turn's window
  read; every container idle; the console's activity list empty.
- No orchestrator line for a floor-turn timeout (45 s default), a stall, a
  convener agenda advance, a chair escalation, a bounded close or an idle
  rotation.
- `channel_conversation_floor_turn_total{outcome="replied"}` at 2 and
  `channel_conversation_floor_round_duration_milliseconds_count` at 1 — the
  first round's numbers. The second round's persisted reply never recorded a
  turn, and no timeout was ever recorded.

A throwaway test in `internal/channels` with the router's own harness — three
members, floor control on, an armed autonomous channel, one member silent
after the opener, replies stamped with cascade depth and @-mentioning their
stimulus — ran the same shape and closed the interaction normally, with and
without the mentions. So the wedge needs something the in-process harness does
not model: the gRPC dispatcher, real agent processing time, the chair and
synthesis wiring, or the agent side of a `do_nothing` turn.

## Impact

Not reachable on the shipped offline demo: the ISSUE-0160 fix never answers a
handed-over turn with silence. It is reachable in principle with a real model:
the reply-discretion prompt tells every persona that silence is a valid outcome
in a group channel. If the wedge is the orchestrator's, an unattended discussion
would sit open with no synthesis and no signal, the failure shape
[ISSUE-0110](ISSUE-0110-autonomous-productive-round-continuation-stall.md)
closed for productive rounds.

## Proposed fix / investigation path

Reproduce on the booted stack first: a mock variant whose handed-over turn
returns `[{"action_type": "do_nothing", "payload": {}}]`, the orchestrator at
debug level, and a goroutine dump (no pprof endpoint is exposed today). Then
decide whether the floor round, the continuation or the agent side holds the
turn, and pin the shape in `autonomous_continuation_test.go`.

## Notes

> 2026-09-15 — filed from the ISSUE-0160 fix's booted verification; not a
> v0.3.16 tag blocker (unreachable on the shipped mock), listed in the release
> checklist's Known Gaps.

> 2026-10-20 — **not slotted** (the [v0.4.0 plan](../v0.4.0-plan.md#v0316-follow-up)'s
> follow-up). Ruling (q) of [v0.4.0's shape](../v0.4.x-sequencing.md#rulings)
> boards an issue only when a phase that boards fixes it, and none does. The
> plan's goal audit proposes it for a later amendment, since a stalled
> autonomous turn stops a society that is meant to keep working.

> 2026-11-13 — more turns are silent on a group channel now. Since the
> fixes for
> [ISSUE-0199](ISSUE-0199-persona-failures-travel-as-completed-work.md) and
> [ISSUE-0239](ISSUE-0239-a-wallet-denial-posts-its-error-sentence-into-a-group-channel.md),
> a persona whose turn fails, or whose model call the wallet refuses,
> posts nothing there and the cause is only logged. Two of those cases
> used to post a sentence that the round took for the speaker's reply:
> running out of model calls, and a wallet denial. To the orchestrator
> each is now the silence this issue is about, so a real fleet can reach
> it without the model choosing to stay quiet: an agent over its budget
> is silent on every turn.
>
> What that costs, read from the source; no orchestrator was run for
> either fix:
>
> - In a floor-control round a silent speaker holds the floor for its
>   turn timeout (`floor_turn_timeout_seconds`, 45 by default) before
>   the next is granted it (`internal/channels/floor_control.go`).
> - A wallet outage, a spent shared budget or a discussion at its cost
>   ceiling denies every speaker. A round of K speakers then takes
>   K × 45 seconds for each message, with nothing posted. The round
>   holds the channel's floor meanwhile, so later messages wait behind
>   it, and the console shows each speaker as thinking for up to 90
>   seconds (`internal/channels/activity.go`).
> - A round in which nobody replies is a stall
>   (`internal/channels/chair_escalation.go`). Where the channel names
>   an `escalation_chair_id`, as the shipped `config/channels.yaml`
>   does, the discussion's one escalation goes to a forced chair turn,
>   which the wallet denies too. Before the fixes each denial line
>   counted as a reply, so there was no stall.
> - On an autonomous channel a discussion continues only from a reply
>   (`internal/channels/autonomous_continuation.go`). It used to go on
>   from each denial line until `max_rounds` closed it. Now the stall's
>   convener or chair turn is denied as well and nothing carries the
>   discussion on: no bounded close, no synthesis and no close
>   notification until another message is published. That is this
>   issue's stall, reached by a spent budget.
>
> The maintainer accepted this as a known gap when ruling that a group
> channel is posted nothing. Telling the orchestrator that a turn
> failed, so it can move on at once, needs a signal on the wire, which
> the typed events of
> [RFC 0041](../rfcs/0041-typed-event-taxonomy-lifecycle-callbacks.md)
> would carry.
