---
id: ISSUE-0098
summary: "Chair never synthesizes while any member is silent — completeness-fixation makes escalation outcome (a) unreachable, and with ISSUE-0096 the whole arc deadlocks to idle"
status: open
severity: high
area: persona
created: 2026-06-12
refs:
  - docs/rfcs/0030-amendment-chair-stall-escalation.md
  - docs/manual-tests/MT-CHANNEL-GOV-004.md
  - docs/issues/ISSUE-0096-display-name-mentions-resolve-to-nobody.md
  - prompts/runtime/safety/chair-escalation.md
---

## Summary

In every live escalation observed so far (three forced turns across two
MT-CHANNEL-GOV-004 sessions, 2026-06-12), the chair chose outcome (b) —
hand off to the member best placed — and never outcome (a), the
synthesis-in-vote. Each time the trigger was the same: one channel
member (`ember-owl`, disposition `addressed`) had not yet spoken, and
the chair treats an incomplete enumeration as "not mine to close". The
forced-turn framing in
[`chair-escalation.md`](../../prompts/runtime/safety/chair-escalation.md)
steers hard toward synthesis, but completeness beats it whenever any
voice is outstanding.

## Context

Observed live on build main @ d47385d (Anthropic provider, demo
personas). Both interactions in the second session followed the same
trajectory:

1. Honest stall → `chair_escalation{outcome=dispatched}` (orchestrator
   half correct).
2. Chair's forced turn: "We're still missing Ember's risk — that's the
   one open item. @Ember Owl — can you give us your one-sentence
   risk…" — outcome (b), named by display name.
3. The display-name mention resolves to no member (ISSUE-0096); the
   `addressed`-only target never hears it; the hand-off dies into
   silence.
4. CE5's one-ration guard correctly blocks re-escalation; concurrence
   nudges draw honest Tier B passes (there is no synthesis to agree
   with); the interaction can only die into idle rotation.

Step 4 is the exact unrecorded-outcome death the amendment exists to
prevent, restored via the failed hand-off. Each link is individually
defensible — the cycle is the bug, and this issue plus ISSUE-0096 are
its two halves.

## Impact

The amendment's headline contract — *a stalled discussion ends in a
recorded decision* — is unmet in practice: outcome (a) is unreachable
whenever any member is silent, which is precisely what a stall implies.
MT-CHANNEL-GOV-004's step 2/3 (synthesis-in-vote → close → recorded
summary) could not be exercised live in three attempts; the §C item 3
acceptance remains open.

## Proposed fix / investigation path

Prompt-side calibration of `chair-escalation.md`: when the missing
voice has already been asked (by the stalled stimulus or earlier in the
window) and stayed silent, the chair should synthesize what it has —
"a partial synthesis on the record beats a complete one that never
arrives." Concretely: make outcome (b) conditional on the chair naming
a member who has NOT yet been asked, and steer everything else to
outcome (a) with the gap noted inside the synthesis ("Ember's risk is
still open — closing with the two we have").

Fixing ISSUE-0096 (display-name mention resolution) is the other half:
it makes outcome (b) functional when the chair does choose it. Both are
needed; neither alone closes the cycle (a working hand-off to a member
with nothing to add still stalls, and a synthesis-biased chair that
does hand off still reaches nobody).

## Fix (prompt calibration landed — live verification pending)

The two-outcome steer in
[`chair-escalation.md`](../../prompts/runtime/safety/chair-escalation.md)
was recalibrated along the investigation path above:

- **Outcome (a)** now explicitly licenses a *partial* synthesis: when a
  voice is still outstanding, close with what the room has and name the
  gap inside the vote's `content` ("Ember's risk is still open — closing
  with the two we have"). "A partial synthesis on the record beats a
  complete one that never arrives."
- **Outcome (b)** is narrowed to a member who has **not yet been asked**
  on the question — not merely one who has been silent. If the member the
  chair would name already had the floor and stayed quiet, the steer
  routes back to (a). Hand-off survives for its legitimate use: bringing
  in a genuinely-uninvited voice (e.g. an `addressed` member never
  mentioned), which now actually reaches them because ISSUE-0096 is
  resolved.
- **The one-shot cost is named (PR 622 review).** CE5 grants the
  interaction exactly one escalation, so a hand-off that itself draws
  silence ends the discussion with nothing on the record — the very
  unrecorded-outcome death this issue is about — with no second
  escalation to recover it. The steer now spells this out and tells the
  chair to hand off only when it has real reason to expect the named voice
  will answer; otherwise prefer (a) (still @-mentioning the missing voice
  inside the vote's `content` if useful). Pinned by
  `test_snippet_warns_handoff_spends_the_only_escalation`.

**The narrowing alone does not redirect the observed failure.** The three
live hand-offs all named `ember-owl` — an `addressed` member never
mentioned, i.e. a *genuinely-unasked* voice — so the narrowed (b) still
*licenses* exactly that hand-off; what made it the wrong move was the
one-shot cost above (and, before #617–#619, the dead mention), not that
Ember had "already been asked." So on the **current** demo roster a clean
re-run may still legitimately land on (b). Exercising outcome (a)
end-to-end live therefore also needs the all-`participant` MT variant from
the Notes below (mention every member in the opener, leave no
`addressed`-only voice as the standing gap) — not the prompt edit on its
own.

The calibration is pinned by
`test_snippet_gates_handoff_on_an_unasked_voice` and
`test_snippet_warns_handoff_spends_the_only_escalation` in
[`test_chair_escalation_agent.py`](../../tests/unit/python/test_chair_escalation_agent.py).
This is a prompt steer against an LLM disposition, so it cannot be proven
by the suite — the issue stays **open** until a clean live MT-CHANNEL-GOV-004
run (on the all-`participant` roster) exercises outcome (a)
(synthesis-in-vote → close → recorded summary, §C item 3).

## Notes

- Three-for-three on outcome (b) is also a topic artifact: the demo
  channel keeps one `addressed` member who never speaks unprompted, so
  every bounded-enumeration prompt leaves a visibly incomplete set. A
  channel whose roster is all `participant` would likely exercise (a)
  more readily — worth doing in the MT (mention every member in the
  opener) independent of the prompt fix.
