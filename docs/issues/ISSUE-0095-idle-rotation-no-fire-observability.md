---
id: ISSUE-0095
summary: "One live idle-rotation no-fire (700 s gap vs 600 s window, unreproduced); decision + startup-window-map instrumentation landed, awaiting next occurrence for root-cause"
status: open
severity: medium
area: channels
created: 2026-06-12
refs:
  - docs/rfcs/0030-interaction-id-producer-pr-plan.md
  - docs/manual-tests/MT-CHANNEL-GOV-004.md
---

## Summary

During the 2026-06-12 MT-CHANNEL-GOV-004 run, one lazy idle rotation that
should have fired did not: a publish to `group:planning` at
03:26:30.728Z arrived **700.6 s** after the channel's last persisted
publish (03:14:50.098Z) against the default **600 s** window, and the
resolver kept the open interaction (`e780b8e1`) — no rotation, no
`previous_interaction_*` stamp, no idle-close log. The same stack later
rotated correctly at a **679.8 s** gap (03:41:09.585Z → 03:52:29.352Z),
and a 5 s-window repro against the same binary rotated at an 8 s gap.
One occurrence, unreproduced.

## Context

Found while executing MT-CHANNEL-GOV-004 (the failure burned the
interaction's CE5 escalation ration and forced a re-run). Ruled out at
the time: config (container `channels.yaml` md5-identical to repo; no
per-channel override; fleet default absent → 600 s), wiring
(`ResolveInteractionIdleTimeouts` called at startup; constructor default
600 s), binary staleness (idle-rotation log string present; 5 s repro
fired), hidden traffic (SQLite `messages` table shows zero rows in the
gap), VM monotonic-clock drift (measured 120.16 s host vs 120.23 s VM
over 2 min). The rotation condition is
[`interaction_resolver.go`](../../internal/channels/interaction_resolver.go)
line ~165; `lastActivity`'s only writer is `settleInteraction` on
persisted publishes.

## Impact

A missed rotation silently extends an interaction across what operators
and governance counters treat as a boundary: stale reply budgets and
end-vote windows persist, and (post chair-stall-escalation) the CE5
ration stays spent, so a stalled successor topic cannot escalate. Worse,
the failure is **invisible** — the rotation path logs only when it
fires; a no-fire leaves no trace to diagnose (this report exists only
because the MT was watching).

## Proposed fix / investigation path

Observability first (the bug is unreproduced; make the next occurrence
self-diagnosing):

1. Debug-log every resolve-time rotation *decision* on a committed
   entry: channel, resolved window, `now`, `lastActivity`, the computed
   gap, fired/not-fired. Rate is one line per publish at debug — cheap.
2. Log the per-channel window map once at startup (the
   `ResolveInteractionIdleTimeouts` outcome), so a wrong resolved window
   is visible without a repro.
3. Optionally a `channel.conversation.rotation_skipped` counter for
   gap>window no-fires (which should be impossible — a nonzero count IS
   the bug signal).

If instrumentation surfaces the cause, the fix is a follow-up to this
issue.

## Notes

> 2026-06-12 — initial capture during the MT-CHANNEL-GOV-004 live run
> (build main @ 113c728). Full timestamp trail in the MT's Test Results
> row.

> 2026-06-13 — observability steps 1 & 2 landed (the bug stayed
> unreproduced across the 2026-06-13 MT-CHANNEL-GOV-004 re-runs, so the
> instrument-first plan stands).
> [`interaction_resolver.go`](../../internal/channels/interaction_resolver.go)
> now emits a `channels: interaction idle-rotation decision` debug line on
> every eligible resolve — committed, non-thread, window>0 — that *could*
> idle out (channel, window, now, last_activity, gap, rotated), and
> `ResolveInteractionIdleTimeouts` logs the resolved per-channel window map
> once at startup (`channels: interaction idle windows resolved`) so a
> wrong resolved window is visible without a repro. Step 3 (the
> `rotation_skipped` counter) deliberately skipped: the decision line
> already carries the gap+window context a bare counter would lack.
>
> **Reading the decision line (corrected).** An earlier draft of this note
> claimed a `gap > window` line that reads `rotated=false` is the no-fire
> signature. That pairing is *unreachable*: the resolver sets `rotated`
> exactly when `gap > window`, so the boolean is fully derivable from the
> `gap`/`window` on the same line and never contradicts them. A real
> no-fire — wall-clock idle past the *intended* window, yet no rotation —
> shows up as a **wrong field**, not a contradiction: `window` larger than
> the configured value (a mis-resolved window — cross-check against the
> startup window map), or `gap`/`last_activity` out of step with wall clock
> (a phantom publish advanced `last_activity`, or `now` is skewed). Two
> known blind spots remain: (a) a no-fire whose cause is *ineligibility*
> (an entry left uncommitted when it should hold history) takes the mint
> path and logs no decision line at all; (b) the decision line is Debug, so
> it is invisible at the staging/production InfoLevel — the live MT stack
> runs `--env development`, where it shows. Kept open: the next live no-fire
> should now be self-diagnosing *for the in-window-resolution failure mode*;
> root-cause is the follow-up.
