---
id: ISSUE-0181
summary: "Facts extracted together overwrite each other. Supersession keys on (subject, predicate) and never on the object, and one extraction stamps every fact with the same time, so several distinct facts under one subject and predicate leave only the last one live: three 'event planning | topic.decided' facts from one briefing became one. In EXP-001's first practice run, all 14 superseded rows in arm D's four stores were siblings from the same conversation, not updates."
status: resolved
severity: medium
area: memory
created: 2026-10-02
closed: 2026-10-05
closed_pr: 1034
refs:
  - agents/memory/_facts_supersede.py
  - agents/memory/_facts_write.py
  - agents/persona_runtime/fact_extractor.py
  - docs/rfcs/0026-declarative-facts-tier.md
  - docs/issues/ISSUE-0180-topic-facts-reachable-only-by-their-exact-subject.md
  - docs/issues/ISSUE-0079-cross-session-supersede-not-scoped.md
  - tests/unit/python/test_fact_store_written_together.py
  - tests/unit/python/test_facts_written_together.py
---

# ISSUE-0181: Facts extracted together supersede each other

## Summary

When a fact is stored, older live rows with the same subject and predicate
are pointed at it as superseded (`apply_supersession` in
[`_facts_supersede.py`](../../agents/memory/_facts_supersede.py)). The key
is the agent, subject, predicate, session, principal and
epoch; the object, what the fact says, is never part of it. Equal times
count as older, so the later arrival wins.

Every fact from one extraction gets the same time, the close time of the
conversation it came from
([`fact_extractor.py`](../../agents/persona_runtime/fact_extractor.py),
line 444, passed to the whole batch at lines 456–460, one store per fact
at lines 270–283). So when a close yields several distinct facts under one
subject and predicate, each supersedes the one before it and only the last
stays live. A briefing that set three constraints produced three
`event planning | topic.decided` facts: "Saturdays in May are
unavailable", "ticket price ceiling is $25" and "lighting crew limited to
two people". Only the lighting crew survived. A scratch call of
`store_extracted_facts` with those three tuples reproduces it: three rows
stored, one live.

## Context

Found in EXP-001's first paid practice run, on 2026-10-01, while tracing
why arm D never recalled its briefing
([ISSUE-0180](ISSUE-0180-topic-facts-reachable-only-by-their-exact-subject.md)).
All 14 superseded rows in arm D's four memory stores were superseded by a
row from the same conversation at the same instant. Not one was a real
update. Two of the lost rows were true facts outside the briefing: a gala
deadline of 9 May 2037 gave way to a February financial checkpoint from
the same meeting, and in another store one deadline replaced another the
same way.

RFC 0026 §F keeps one live row per subject and predicate on purpose, so a
fact contradicted later loses its weight
([RFC 0026](../rfcs/0026-declarative-facts-tier.md), lines 156–162). It
also says ties are "unreachable in the hot path" (line 160), which is no
longer true: every batch ties. The supersede module accepts ties only for
siblings restating the same claim (its paragraph that begins
"Equal-timestamp ties"). Nothing covers siblings
that say different things, or predicates that hold several true values at
once, such as `topic.decided` or `topic.has_deadline`.
[ISSUE-0079](ISSUE-0079-cross-session-supersede-not-scoped.md) scoped the
chain by session and did not touch the object.

## Impact

A persona keeps only the last of several things it learned together about
one topic. Person predicates with several true values (`has_child_named`,
`prefers`, `committed_to`) probably lose rows the same way; that follows
from the code and was not run. In EXP-001, the $25 cap could not reach any
prompt in two of arm D's four stores. By the maintainer's call of
2026-10-02, this is fixed before the scored run.

## Proposed fix / investigation path

Test-first, smallest change first:

1. Never let a row supersede a sibling from the same source conversation;
   siblings coexist, and a later conversation still supersedes them.
2. Or treat multi-valued predicates as sets, superseding only on the same
   object.

Then correct RFC 0026 §F's line about ties.

## Fix

Option 1. The store's shape does not change: no schema, index or
migration, and no change to the extractor, its prompt or the chain key
(agent, subject, predicate, session, principal, epoch). Rows already
superseded stay superseded.

The rule, in
[`_facts_supersede.py`](../../agents/memory/_facts_supersede.py): a new
fact no longer supersedes a live fact that the same extraction wrote and
that says something different. "The same extraction" means the same
source interaction and the same `asserted_at`, which is how one close
stamps its batch. The rest is as before. A later conversation still
replaces the whole earlier set, an older write that arrives late is
still superseded, and rows from another source, or with no source, keep
the tie rule.

Three choices go beyond the letter of option 1. Each is one line and
can be dropped on its own:

- **A word-for-word repeat still leaves one row.** If one extraction
  lists the same object twice, the second copy supersedes the first, so
  the prompt does not print one fact twice.
- **A late older write points at the last fact inserted.** Several rows
  can now be live at the newest instant. The row that supersedes a late
  older write is the last one inserted, the one recall lists first among
  that key's rows.
- **An empty source id is stored as no source.** The rule reads a
  source id as "written by one extraction", so an empty string must
  not become a source that unrelated rows share. No extraction writes
  one; only a direct caller can.

Measured on arm D's four practice stores: replaying their 121 fact rows
through the fixed write path leaves none superseded, where the run had
14. Eleven subject-and-predicate keys now hold two or three live rows.
The three `event planning | topic.decided` facts are live in each of
the three stores that hold them. The fourth store filed the same
constraints under `event plan`, and both `topic.decided` rows it wrote
there are live too. The same replay through the unfixed code reproduces
the 14.

Tests:
[`test_fact_store_written_together.py`](../../tests/unit/python/test_fact_store_written_together.py)
pins the rule under each write order.
[`test_facts_written_together.py`](../../tests/unit/python/test_facts_written_together.py)
follows three facts from the extractor, through the recall step the
runtime uses, to the prompt and the audit trail. Fourteen of the
nineteen new tests fail on the unfixed code; the rest pin cases the
rule must leave alone. No existing test changed its outcome, and the
six stable golden traces replay unchanged.

## Slot: merges before EXP-001, by the maintainer's call of 2026-10-02

- **No plan is needed.** Ruling (a) of the
  [sequencing Amendment 2026-09-12](../v0.3.x-sequencing.md#amendment-2026-09-12--close-v0316-small-then-measure-before-any-train-opens)
  opens no plan before EXP-001 reports; like
  [#1014](https://github.com/mkhomutov/Persatrix/pull/1014) and
  [#1030](https://github.com/mkhomutov/Persatrix/pull/1030), this is a
  standalone fix. Ruling (f) holds: no store migration.
- **Ruling (b)** covers memory isolation, attribution and audience work.
  This is none of them: the session, principal and epoch parts of the
  chain key do not change.
- **EXP-001.** Arm D is the only arm whose prompt carries injected
  memory, so only its prompts can change. More fact lines now compete
  for the facts section's token budget.

## Notes

> 2026-10-02 — filed from EXP-001's first practice run. The maintainer
> ruled the same day that it is fixed before the scored run, with
> ISSUE-0159 and ISSUE-0180.

> 2026-10-05 — limits the fix leaves, each as before it unless stated:
>
> 1. A correction inside one conversation ("Mira... no, Lila") leaves
>    both values live until a later conversation speaks about the key.
>    Before the fix the last-listed value won. The store cannot tell a
>    correction from an addition; none of the 121 practice rows was one.
> 2. A later conversation replaces the whole earlier set, including
>    facts it did not mention.
> 3. Two records closed at the same instant, as a room-close fan closes
>    them, still replace each other's facts on a shared key, by arrival
>    order. Two such writes that overlap can supersede each other and
>    leave no live row: two gathered store calls did so in 100 runs of
>    100, with and without this fix, and the close path's second phase
>    runs outside the agent lock
>    ([ISSUE-0183](ISSUE-0183-overlapping-fact-writes-leave-no-live-fact.md)).
>    The practice run had no such pair. It
>    would become routine if
>    [ISSUE-0180](ISSUE-0180-topic-facts-reachable-only-by-their-exact-subject.md)
>    makes records reuse a stored subject, so the two are decided
>    together.
> 4. Nothing bounds how many facts one extraction leaves under a key,
>    and the facts section shares one token budget across subjects.
>    What does not fit is cut oldest first, and inside one extraction
>    the facts it listed first are the first cut. Recall also reads
>    only a subject's 20 newest live facts before the confidentiality
>    gate runs, and one conversation can now fill those
>    ([ISSUE-0184](ISSUE-0184-fact-recall-cap-runs-before-the-gate.md)).
> 5. In arm D each meeting is its own session, so a later meeting never
>    supersedes an earlier meeting's fact.
> 6. Option 2 above, predicates that hold several values by design,
>    stays open.
> 7. A repeat counts as one only when the text is identical. A change
>    of case or a trailing space makes a second live fact.
> 8. The exception is not scoped to the session. A `legacy` row that
>    carries a named session's source and time is kept beside that
>    session's facts. No extraction writes that; a direct caller can.
>
> "Conversation" in limits 1 and 2 means one interaction record: one
> speaker's turns in one channel between an open and a close. A group
> channel holds one record per speaker heard, and a speaker can have
> more than one in a meeting. So "a later conversation" includes
> another record of the same meeting that closes later, and facts two
> speakers state under one subject and predicate still replace each
> other. Supersession is also per session, so per channel: a later
> conversation in another channel replaces nothing.
