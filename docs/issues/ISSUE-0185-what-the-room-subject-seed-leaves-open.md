---
id: ISSUE-0185
summary: "ISSUE-0180's fix recalls a topic subject that a channel's description names. Five things stay open with no other tracker: a subject no description names is still reached only by its exact wording; the extractor can file facts under the channel's own name, which seeds wherever a description contains that name; a fact whose source channel's roster cannot be fetched is admitted, now on every turn; the read from both ends prints an old value without its later correction; and one owner is kept per organisation."
status: open
severity: low
area: memory
created: 2026-10-06
refs:
  - agents/persona_runtime/topic_seeds.py
  - agents/persona_runtime/audience.py
  - agents/persona_runtime/facts_section.py
  - agents/memory/facts.py
  - agents/memory/_facts_supersede.py
  - prompts/runtime/safety/fact-extractor-suffix.md
  - evaluators/eval_channel_roster.py
  - config/channels.yaml
  - docs/rfcs/0026-amendment-topic-subject-predicates.md
  - docs/issues/ISSUE-0180-topic-facts-reachable-only-by-their-exact-subject.md
  - docs/issues/ISSUE-0183-overlapping-fact-writes-leave-no-live-fact.md
  - docs/issues/ISSUE-0184-fact-recall-cap-runs-before-the-gate.md
  - https://github.com/mkhomutov/Persatrix/pull/1035
---

# ISSUE-0185: What the room-subject seed leaves open

## Summary

[ISSUE-0180](ISSUE-0180-topic-facts-reachable-only-by-their-exact-subject.md)
is resolved: a persona now recalls a topic subject that its channel's
description names, whatever the message says. The review of that fix
found five things it leaves, each real and none tracked anywhere else
once ISSUE-0180 is closed.

1. **A subject no description names is still reached only by its exact
   wording.** This is ISSUE-0180's own defect, for every subject the
   fix does not cover: a named plan ("spring gala"), or an adviser's
   reply filed under "event planning". Options 2 and 4 of ISSUE-0180
   were measured and not taken.
2. **The channel's own name can become a subject that is always
   recalled.** When a record's text names no organisation, the new
   extractor sentence sometimes takes the channel name from the prompt's
   own header (`advice-2`, three tuples a pass in the practice
   re-extractions, none with the shipped wording). Such a subject seeds
   in any channel whose description contains that name as a word. The
   shipped `planning` channel is one: its description ends "product
   planning discussion".
3. **The audience check admits a fact it cannot check, now on every
   turn.** A fact from another channel is withheld when the acting
   channel holds someone its source channel does not, and admitted when
   the source channel's roster cannot be fetched
   (`ENFORCED_VERDICTS` in
   [`audience.py`](../../agents/persona_runtime/audience.py)). Before
   the fix such a fact reached the check only when a message named its
   subject. Under a subject the description names it reaches it on
   every turn.
4. **The read from both ends prints an old value without its
   correction.** A fact replaces another only inside one channel. When
   a later channel corrects what the first channel said, both stay
   live, and once the subject's list outgrows the caps the first value
   is printed and the correction is not. A fact line carries no date.
5. **One owner per organisation.** `topic.owned_by` keeps one value per
   subject. With the part moved into the object, two owners of two
   parts of one organisation, stated in one record, leave one.

## Context

Found on 2026-10-06 by the review of
[#1035](https://github.com/mkhomutov/Persatrix/pull/1035). The fix's
own list of what it leaves is in ISSUE-0180, with the measurements.

Item 3 is a policy the audience check has had since it went live in
v0.3.16: a fact whose source roster is unknown is recorded and admitted,
because withholding it would quietly degrade a persona whenever a
roster is missing. The fix does not change the policy. It changes how
often the policy is relied on, from the turns that name a subject to
every turn in a channel whose description names it.

## Impact

- Items 1 and 2 are about reach: some facts are still not recalled, and
  some would be recalled where nobody meant them to be. Item 2 was read
  from the extractor's replies for other channels; whether the
  extractor files a subject `planning` for the shipped channel needs a
  model call and was not run.
- Item 3 is about who sees a fact. While a DM's roster request fails, or
  once the DM is removed, a fact told in it can be printed in a group
  channel whose description names its subject, on every turn and with
  nobody naming it. With the roster available the same fact is
  withheld.
- Item 4 can make a persona state a superseded fact as current, in a
  channel whose subject has more facts than the caps keep (about a
  dozen lines).
- Item 5 was not seen: no practice record names two owners. In the
  re-extractions 5 of 13 and 4 of 12 owner tuples sit under the
  organisation, none of 18 with the shipped wording.

As measured, item 2 does not touch EXP-001's arm D: no practice
channel's description contains a channel's name, so the `advice-2`
tuples seed nowhere there.

## Proposed fix / investigation path

Each is a separate choice, and items 3 and 4 are the maintainer's.

1. A deterministic match looser than the whole subject was measured
   under ISSUE-0180 and rides on single words; a subject alias list
   (RFC 0026's open question 3) is the other route.
2. Keep the channel's name out of the description match, or reword the
   sentence so the extractor does not take a name from the header. A
   rewrite of the topic sentence did not, in its one pass. Confirm on a
   `group:planning` record first.
3. Fail closed for a fact that only a standing seed brought in, or for
   every unknown roster, or cache a channel's last known roster. The
   first needs the recall to say which seed returned a row. A narrower
   rule: keep a fact told in a DM out of a standing seed's read in any
   other channel. With the roster known, the check already withholds
   such a fact from every channel that holds a third member.
4. Decide which predicates hold one value across channels (a deadline,
   an owner), so that a newer fact replaces an older one there, or
   print the date a fact was stated.
5. Take `topic.owned_by` off the one-value list, or have the extractor
   keep the owned part as the subject of an owner tuple.

Also open from the same review, as test coverage: no golden trace
exercises the new seeding. The eval harness's roster carries no
description
([`eval_channel_roster.py`](../../evaluators/eval_channel_roster.py)),
and a recipe cannot declare one.

## Notes

> 2026-10-06 — filed from the review of the ISSUE-0180 fix. Not slotted.
> Severity is left at `low` for the maintainer, who may rate item 3
> higher: it is the only one about who sees a fact.
