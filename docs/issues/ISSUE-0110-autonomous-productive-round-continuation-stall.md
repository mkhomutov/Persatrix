---
id: ISSUE-0110
summary: "RFC 0052 autonomous discussion stalled permanently after its FIRST productive floor round on a live provider: in-round replies are floor-speaker-suppressed (never re-fanned), the anti-collapse tail reacts only to SILENT rounds, and idle rotation is lazily evaluated on a next publish that never comes — so the interaction sat open forever with no convener advance, no bounded close, and no synthesis. Never caught before: the offline mock roster cannot produce valid salience bids (always silent → every round took the stall path) and the Go acceptance suite's 1ms floor timeout made every in-test round all-silent with replies published manually BETWEEN rounds."
status: resolved
severity: high
area: channels
created: 2026-07-20
closed: 2026-07-20
closed_pr: 759
refs:
  - docs/rfcs/0052-autonomous-agent-channels.md
  - docs/rfcs/0052-pr-plan.md
  - docs/manual-tests/MT-AUTONOMOUS-001.md
  - docs/issues/ISSUE-0109-rfc0052-autonomous-defaults-calibration.md
---

## Summary

On an `autonomous.enabled` floor-controlled channel, a floor round in which the
roster **actually replied** ended the discussion permanently. The in-round
replies were committed and billed but — per the RFC 0030 Layer 2.5 D1
deferred-fanout contract — their fanout was suppressed (the round loop is the
sole dispatcher), and **nothing re-established the cascade afterwards**: the
RFC 0052 anti-collapse tail (`maybeAdvanceAgenda` / `maybeEscalateStall`)
no-ops on a replied round by design, and idle rotation is evaluated lazily
inside `resolveInteractionID` — i.e. only on the *next publish*, which never
arrives on a fully quiet channel. Net: no member ever received the replies
(the opener's author was never dispatched again at all), no round 2, no
`max_rounds`/budget close, no chair synthesis — an immortal-but-inert
interaction. This violates the RFC 0052 §Risk model, which explicitly expects
the discussion to be carried by the "A's post wakes B, whose post wakes A…"
fanout cycle bounded by the Layer-0 cascade-depth cap and the Layer-2 reply
budget.

## Context

Found live 2026-07-20 — the first autonomous run on a real provider
(claude-sonnet-4-6, the `roundtable` demo channel): opener (depth 1), one
substantive reply each from the two responders (depth 2, both auto-mentioning
the opener's author), then permanent silence; orchestrator logs show only the
reply's wallet settle followed by reaper passes, and the mentioned member's
agent received nothing. Two blind spots hid it: the **offline mock roster is
always silent** (the mock provider cannot produce valid reasoning-mode
salience bids, so the bias-to-silence gate mutes every mock persona — each
round stalls and the convener-advance path fires, which is why
`make demo-autonomous` "worked"), and the **Go acceptance suite** ran floor
rounds with a 1ms turn timeout (all-silent) publishing replies manually
*between* rounds, so the in-round-reply suppression was never composed with
the autonomous tail.

## Resolution

`internal/channels/autonomous_continuation.go` adds the missing wake at the
fanout tail, scoped to `autonomous.enabled` (human channels byte-for-byte,
pinned): after a **productive** floor round on an armed channel, the round's
last reply is re-fanned as the next open-floor stimulus on a tracked detached
goroutine. Every shipped bound composes unchanged — each continued round
advances the §D round tally (`max_rounds`) and the wallet soft-budget read at
its own tail, and the Layer-0 cascade-depth cap is honoured, never bypassed:
when the would-be continuation stimulus sits **at the cap** (or an at-cap
publish is fanout-suppressed on the concurrent path), the unattended
discussion has crossed a terminal bound with no human to continue past it, so
it takes the §D structural close (chair-synthesis-armed when chaired) instead
of wedging open. Companion fix: the `publishCommit` cascade-cap branch now
notifies the reply waiter before suppressing (the latch branch's documented
Notify-then-suppress posture) so an at-cap in-round reply no longer burns the
full turn timeout per remaining speaker, mislabeled `floor_turn{timeout}`.

Pinned by `internal/channels/autonomous_continuation_test.go`: the
productive-round continuation runs to the `max_rounds` close; the human-channel
round still ends with the round; the at-cap continuation closes structurally on
both paths; the at-cap reply advances the round via Notify.

## Follow-ups (out of scope here)

- The global `max_cascade_depth` default (5) binds before the roundtable demo's
  `max_rounds: 12` — an autonomous discussion closes after ~4 productive
  rounds. A per-channel depth override (or an autonomous-aware default) is an
  ISSUE-0109-class calibration question for the live MT runs.
- Mock-provider salience-bid support, so the offline demo can exercise the
  productive-round path it has never covered.
- A time-based liveness watchdog for open autonomous interactions (eager idle
  rotation), so *any* future quiet-channel gap degrades to a bounded close
  with artifacts instead of an immortal interaction.
