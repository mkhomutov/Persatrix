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
