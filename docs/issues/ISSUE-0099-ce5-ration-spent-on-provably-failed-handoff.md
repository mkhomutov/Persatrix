---
id: ISSUE-0099
summary: "CE5 spends the escalation ration on a provably-empty chair hand-off (mentions resolve to no floor-capable member — a respond:never observer or the chair itself) with no refund/synthesize fallback, so that interaction can only die idle. Re-scoped to low 2026-06-13: post-ISSUE-0096/0098 the common case (chair hands off the whole arc) is gone — the chair now synthesizes — leaving only this narrow empty-target residue"
status: resolved
resolution: "Closed by the synthesize-instead re-dispatch (option 2), #630 + #631. On the publish-time-PROVABLE misfire — the escalation chair's forced-turn reply names a target that lifts to a real id but is not floor-capable, so `resolveFloorMentions` comes up empty — the orchestrator re-forces ONE synthesize-only turn (the `chair_escalation_resynthesize` wire field + a synthesize-only framing snippet) carrying the STASHED original non-chair stimulus, bounded to once per interaction. Option 1 (refund), this issue's prior recommendation, proved INERT: the misfired reply is itself chair-authored, so a refunded re-escalation trips `maybeEscalateStall`'s `self_stimulus` guard and never fires — the re-force must re-send a non-chair stimulus, which only option 2 does. **Live-proven on the REAL chair 2026-06-14 (@ b817344, no injection):** a natural stalled arc escalated to nova-sparrow whose forced-turn reply reached the misfire (an `end_interaction_vote` that published with a non-floor-capable mention and no vote metadata) → `chair hand-off misfired; re-forced a synthesize-only turn` → `chair_escalation{outcome=resynthesized}=1` → the re-forced turn cast a clean synthesis → `end_votes` close → structural summary; a second arc with a clean prose synthesis correctly did NOT re-force (dormant-correct), reconfirmed on the natural happy path @ c348d18. Two siblings found and fixed in the same effort: (1) the #631-review vote-guard — a correctly-PARSED synthesis-in-vote that `@`-mentions the still-outstanding (non-floor-capable) voice is outcome (a) already achieved, not a misfire, so `misfired` now excludes any `end_interaction_vote` publish (deterministically pinned by `TestChairResynthesize_EndToEnd_VoteReplyDisarmsNoReForce`; the CLI has no `--vote` to reproduce it live); (2) ISSUE-0101 — a one-line json-fenced end-vote the action parser dropped, which published the vote as raw text and made a genuine synthesis read as a misfire (the visible double-synthesis), now resolved. See the MT-CHANNEL-GOV-004 Test Results 2026-06-14 rows."
closed: 2026-06-14
closed_pr: 631
severity: low
area: channels
created: 2026-06-12
refs:
  - docs/rfcs/0030-amendment-chair-stall-escalation.md
  - docs/rfcs/0030-amendment-floor-capable-directedness.md
  - docs/issues/ISSUE-0096-display-name-mentions-resolve-to-nobody.md
  - docs/issues/ISSUE-0098-chair-completeness-fixation-blocks-synthesis.md
  - docs/issues/ISSUE-0101-action-parser-drops-one-line-json-fence.md
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
(refund)** — *superseded; see Resolution below*. The reasoning was that
the rare residue did not justify option 2's extra dispatch marker, so the
smaller refund would do.

**Resolution (2026-06-14): option 2 (synthesize-instead re-dispatch),
shipped in #630 + #631. Option 1 (refund) is INERT and the prior
recommendation was wrong.** Trace: refund clears `chairEscalated` so the
"next stalled round may re-escalate" — but the next stalled round's
stimulus *is the chair's own misfired reply* (the reply re-fanned as open
floor and drew passes), so `maybeEscalateStall` reaches its
`self_stimulus` guard (`chairID == msg.SenderID`) and withholds without
re-escalating. The refunded ration is never re-spent; the interaction
still dies idle. The fix to converge has to **re-force a turn built from
a non-chair stimulus** — exactly what option 2 does by stashing the
original stalled stimulus at first-escalation time and re-sending it under
the resynthesize marker. The "extra LLM call" objection was also moot:
option 1's intended re-escalation is itself an LLM call, so both pay one;
the real difference was only the framing, and only option 2's actually
fires. A PR-2 review additionally found that a misfire is indistinguishable
at the floor-mention seam from a *synthesis-in-vote that `@`-mentions the
outstanding voice*, so the trigger now excludes any `end_interaction_vote`
publish (see Resolution front-matter + the MT row).

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
  residue; the re-force (option 2) targets it.
- The same empty-`resolveFloorMentions` signal also fires for a *synthesis-
  in-vote* whose `content` `@`-mentions the still-outstanding voice (the
  framing invites it, and that voice is typically the non-floor-capable
  operator/observer). That is outcome (a) already achieved, not a misfire —
  so the re-dispatch trigger excludes any publish carrying an
  `end_interaction_vote` (#631 review). Without that guard the chair gets a
  spurious second synthesize-only turn after it has already voted.
