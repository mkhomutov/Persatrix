---
id: ISSUE-0184
summary: "Fact recall reads only the newest 20 live facts about a subject, and the confidentiality gate and the audience check run after that read. So 20 newer facts a turn may not see hide every older fact it may, and the prompt says nothing about the subject. Since the ISSUE-0181 fix one conversation can leave that many facts under one topic; before, a topic needed five rooms. Nothing bounds how many facts one extraction leaves under a subject, and inside one extraction the first-listed facts are the first cut."
status: open
severity: low
area: memory
created: 2026-10-05
refs:
  - agents/persona_runtime/facts_section.py
  - agents/persona_runtime/memory_context.py
  - agents/memory/facts.py
  - evaluators/shadow_measurement.py
  - docs/rfcs/0026-declarative-facts-tier.md
  - docs/rfcs/0037-memory-confidentiality-channel-classification.md
  - docs/issues/ISSUE-0181-facts-extracted-together-supersede-each-other.md
  - https://github.com/mkhomutov/Persatrix/pull/1034
---

# ISSUE-0184: Fact recall's 20-row cap runs before the gate

## Summary

For each subject a turn asks about, fact recall reads the newest 20 live
facts (`FACTS_RECALL_LIMIT` in
[`facts_section.py`](../../agents/persona_runtime/facts_section.py)).
Only then do the [RFC 0037](../rfcs/0037-memory-confidentiality-channel-classification.md)
§D gate and the audience check drop the facts the turn may not see
([`memory_context.py`](../../agents/persona_runtime/memory_context.py)).
Nothing reads further to replace what was dropped. If the 20 newest
facts about a subject are all withheld, every older fact the turn may
see is already gone, and the prompt says nothing about the subject.

## Context

Found on 2026-10-05 by the review of the
[ISSUE-0181](ISSUE-0181-facts-extracted-together-supersede-each-other.md)
fix. The mechanism is older than that fix. What the fix changes is how
many live facts one conversation can leave under a subject:

- **A topic.** Before, one live fact per predicate, so four per room;
  filling the cap took five rooms. Now one extraction that lists 20
  facts about the topic is enough.
- **A person.** One conversation with 20 different predicates about a
  person already filled it.

Nothing bounds how many facts one extraction stores under a subject or
a key. The facts that fit are also cut in a fixed order. Recall lists a
subject's facts newest first, and the facts section admits them in that
order until its token budget, 200 by default, is used. Facts of one
extraction share a time and are listed last-written first, so the ones
the extraction listed first are the first cut.

The shadow measurement assumes the same width. Its `bounded_volume`
check reads more than 20 admitted facts in one turn as seed-flooding
(`DEFAULT_TIER_BOUNDS` in
[`shadow_measurement.py`](../../evaluators/shadow_measurement.py)), a
number chosen when a subject and predicate held one live fact. A turn
that seeds the persona, the sender and three topics can now pass it
with no flood. Nothing runs in shadow today, and this was read from the
code, not run; check the number before the next shadow measurement of
fact recall.

## Impact

A persona can go silent on a subject it holds visible facts about,
because newer withheld facts took every recall slot. Nothing leaks: the
gate still withholds what it should. EXP-001's first practice run was
far from the cap: the largest subject in arm D's stores holds six facts,
and all 121 rows carry the default level.

## Proposed fix / investigation path

Test first: 20 newer withheld facts and one older visible fact about a
subject; the visible fact must reach the prompt. Then read past the cap
when the gate drops rows, rather than filtering by level in the query:
the §G tripwire is built from the candidates the gate withholds, so it
must keep seeing them. Whether one extraction's facts should print in
the order they were listed, and whether a subject needs its own share
of the token budget, are separate choices in the same place.

## Notes

> 2026-10-05 — filed from the review of the ISSUE-0181 fix. Not slotted.

> 2026-10-06 — [ISSUE-0180](ISSUE-0180-topic-facts-reachable-only-by-their-exact-subject.md)'s
> fix reads a subject the room's description names from both ends, so
> for such a subject the cap keeps the ten newest and the ten oldest
> rows, not the twenty newest (three of each for the room's first
> subject on a turn whose message names a subject of its own). The cap
> still runs before the gate. What
> newer withheld facts can crowd out there is the middle of the list,
> no longer its oldest end; every other subject is read as before.
