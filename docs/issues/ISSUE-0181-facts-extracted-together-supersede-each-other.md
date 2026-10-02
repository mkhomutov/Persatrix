---
id: ISSUE-0181
summary: "Facts extracted together overwrite each other. Supersession keys on (subject, predicate) and never on the object, and one extraction stamps every fact with the same time, so several distinct facts under one subject and predicate leave only the last one live: three 'event planning | topic.decided' facts from one briefing became one. In EXP-001's first practice run, all 14 superseded rows in arm D's four stores were siblings from the same conversation, not updates."
status: open
severity: medium
area: memory
created: 2026-10-02
refs:
  - agents/memory/_facts_supersede.py
  - agents/memory/_facts_write.py
  - agents/persona_runtime/fact_extractor.py
  - docs/rfcs/0026-declarative-facts-tier.md
  - docs/issues/ISSUE-0180-topic-facts-reachable-only-by-their-exact-subject.md
  - docs/issues/ISSUE-0079-cross-session-supersede-not-scoped.md
---

# ISSUE-0181: Facts extracted together supersede each other

## Summary

When a fact is stored, older live rows with the same subject and predicate
are pointed at it as superseded
([`_facts_supersede.py`](../../agents/memory/_facts_supersede.py), lines
174–236). The key is the agent, subject, predicate, session, principal and
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
siblings restating the same claim (lines 46–68). Nothing covers siblings
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

## Notes

> 2026-10-02 — filed from EXP-001's first practice run. The maintainer
> ruled the same day that it is fixed before the scored run, with
> ISSUE-0159 and ISSUE-0180.
