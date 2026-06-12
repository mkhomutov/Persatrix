---
id: ISSUE-0099
summary: "CE5 spends the escalation ration on a hand-off the orchestrator can prove reached nobody — no refund or synthesize-instead fallback, so the interaction can only die idle"
status: open
severity: medium
area: channels
created: 2026-06-12
refs:
  - docs/rfcs/0030-amendment-chair-stall-escalation.md
  - docs/rfcs/0030-amendment-floor-capable-directedness.md
  - docs/issues/ISSUE-0096-display-name-mentions-resolve-to-nobody.md
  - docs/issues/ISSUE-0098-chair-completeness-fixation-blocks-synthesis.md
---

## Summary

CE5 deliberately rations escalation to one forced turn per interaction
and does not refund failures — a sound loop guard. But one failure mode
is provable at publish time: the chair's forced-turn reply names a
hand-off target whose mentions resolve to no floor-capable member
(`resolveFloorMentions` comes up empty; the orchestrator already logs
`channels: mentions name no floor-capable member`). The publish is
reclassified to open floor, the just-passed Tier B bids pass again, and
the hand-off provably reached nobody — yet the ration stays spent, so
the stalled interaction's only remaining exit is idle rotation with its
outcome unrecorded.

## Context

Observed live on 2026-06-12 (build main @ d47385d, MT-CHANNEL-GOV-004):
in both escalated interactions the chair handed off by display name
(ISSUE-0096), the directedness gate logged the no-floor-capable-member
debug line, and the interaction then sat dead for the full 600 s idle
window — concurrence nudges drew honest passes because no synthesis
existed to concur with. CE5's `already_escalated` disposition fired
(metric-only; see the MT doc's step 2 note) on every subsequent stalled
round.

## Impact

The escalation arc has no recovery path from its most common live
failure (three-for-three chair turns chose hand-off; see ISSUE-0098).
The amendment's contract — a stall ends in a recorded decision — fails
exactly when the chair's hand-off misfires, and the orchestrator
watches it happen with enough information to know.

## Proposed fix / investigation path

Design question, two candidate shapes (in `maybeEscalateStall` /
the chair-escalation dispatch path, `internal/channels/`):

1. **Refund**: when the chair's forced-turn reply is reclassified to
   open floor because its mentions named no floor-capable member,
   clear the interaction's escalated flag so the *next* stalled round
   may escalate again. Bounded: the refund itself happens at most once
   per interaction (a refunded re-escalation that fails again stands),
   keeping the loop guard.
2. **Synthesize-instead re-dispatch**: on the same provable-failure
   signal, re-dispatch one forced turn with an amended framing — "your
   hand-off reached nobody; synthesize what you have and cast your
   end-vote" — i.e., force outcome (a) as the fallback. Costs one extra
   LLM call; converges harder.

Either keeps CE5's intent (no unbounded escalation loops) while closing
the watched-it-die gap. Option 2 pairs naturally with the ISSUE-0098
prompt calibration; option 1 is smaller and provider-agnostic.

## Notes

- Out of scope until ISSUE-0096 lands: display-name resolution will
  make most current hand-offs succeed, shrinking this to the genuinely
  empty-target case (e.g. chair names a departed member). It stays a
  real gap — the guard exists precisely for the residue.
