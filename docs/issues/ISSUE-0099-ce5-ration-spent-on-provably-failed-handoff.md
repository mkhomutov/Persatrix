---
id: ISSUE-0099
summary: "CE5 spends the escalation ration on a provably-empty chair hand-off (mentions resolve to no floor-capable member — a respond:never observer or the chair itself) with no refund/synthesize fallback, so that interaction can only die idle. Re-scoped to low 2026-06-13: post-ISSUE-0096/0098 the common case (chair hands off the whole arc) is gone — the chair now synthesizes — leaving only this narrow empty-target residue"
status: open
severity: low
area: channels
created: 2026-06-12
refs:
  - docs/rfcs/0030-amendment-chair-stall-escalation.md
  - docs/rfcs/0030-amendment-floor-capable-directedness.md
  - docs/issues/ISSUE-0096-display-name-mentions-resolve-to-nobody.md
  - docs/issues/ISSUE-0098-chair-completeness-fixation-blocks-synthesis.md
  - docs/manual-tests/MT-CHANNEL-GOV-004.md
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

When it fires, the amendment's contract — a stall ends in a recorded
decision — fails: the chair's hand-off misfires and the orchestrator
watches it happen with enough information to know.

**Re-scoped 2026-06-13** (MT-CHANNEL-GOV-004 clean re-run, main @
7b2de85): this is no longer the *common* failure. The original evidence
was a three-for-three hand-off run (ISSUE-0098), but post-0098 prompt
calibration (#622) + post-0096 mention-lifting (#619) the chair now
reliably takes outcome (a) — synthesis-in-vote — even on the default
roster with a standing `respond: addressed` voice present (the
historical hand-off trigger). Two live arcs on that exact roster both
synthesized and closed on `end_votes`; the failed-hand-off path was
never entered. What remains is the narrow residue in Notes below — hence
the drop to `low`.

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

**Recommendation (2026-06-13, post-re-scope):** prefer **option 1
(refund)**. With the common hand-off case closed, the provable signal
now fires only for the rare observer/self empty-target residue, which
does not justify option 2's extra LLM call, new dispatch marker, and
two-state ration. Refund clears the escalated flag on the provable empty
hand-off (bounded to once per interaction, preserving the loop guard) so
the next stalled round may re-escalate. Option 2 was the stronger choice
only while the hand-off path was the *default* behaviour, which the
re-run shows it no longer is.

## Notes

- ISSUE-0096 (#619) and ISSUE-0098 (#622) both landed 2026-06-13:
  display-name resolution now makes a hand-off to a current floor-capable
  member succeed, and the calibrated chair prompt steers to synthesis by
  default. Both reconfirmed live in the MT-CHANNEL-GOV-004 re-run.
- Precision on the provable surface (corrects this issue's original
  draft): a *departed or hallucinated* name does **not** lift — lifting
  is membership-scoped — so it yields empty `mentions`, indistinguishable
  at the publish seam from a no-mention synthesis attempt, and the
  `mentions name no floor-capable member` line never fires. So the
  departed-member example is *not* provable. The genuinely-provable
  residue is narrower: the chair @-mentions someone who lifts to a real
  id but is **not** floor-capable — a `respond: never` observer, or the
  chair itself. That is the only case where `resolveFloorMentions` comes
  up empty with non-empty `mentions`. The guard exists precisely for this
  residue; option 1 (refund) is the proportionate close.
