---
id: ISSUE-0180
summary: "A topic fact reaches a later prompt only when the incoming message repeats its stored subject word for word. The extractor invents a subject at every close (\"harbour players hall\"), so paraphrase, a partial name or a subject that splintered across closes all miss; seeds are capped at three, newest first, and a short subject also matches inside a longer one. In EXP-001's first practice run, none of the 23 rows arm D's four advisers stored from the briefing reached a later meeting's prompt."
status: open
severity: medium
area: memory
created: 2026-10-02
refs:
  - agents/persona_runtime/topic_seeds.py
  - agents/persona_runtime/facts_section.py
  - agents/memory/_facts_topics.py
  - prompts/runtime/safety/fact-extractor-suffix.md
  - docs/rfcs/0026-amendment-topic-subject-predicates.md
  - docs/rfcs/0026-declarative-facts-tier.md
  - docs/issues/ISSUE-0159-episodic-score-inverts-bm25.md
  - docs/issues/ISSUE-0181-facts-extracted-together-supersede-each-other.md
  - docs/experiments/EXP-001-harness.md
---

# ISSUE-0180: A topic fact comes back only when a message repeats its exact subject

## Summary

The facts tier stores what a persona learns about a topic under a subject
the extractor names at each close, such as "harbour players hall" or
"event planning". It brings such a fact back in exactly one way: the
incoming message must contain the whole subject as a run of words, after
case folding
([`match_topic_subjects`](../../agents/persona_runtime/topic_seeds.py),
the regex at line 129). A message that says "the hall", or "our own
stage", or names the theatre without the word "hall", reaches nothing.

Three things narrow the route further:

- **Subjects splinter.** The extractor never sees the subjects already
  stored ([the extractor prompt](../../prompts/runtime/safety/fact-extractor-suffix.md)
  asks for "a canonical short name" and gives no list), so one topic is
  filed under a new string at each close. One adviser's briefing alone
  produced four subjects for one organisation's three facts.
- **Seeds crowd each other out.** A message seeds at most three subjects
  (`TOPIC_SEED_LIMIT`), newest first, and a short subject also matches
  inside a longer one: "harbour players" uses a slot whenever "harbour
  players hall" is named.
- **Some rows can never be read.** A topic seed reads only topic
  predicates, so a row that pairs a topic subject with a person predicate
  ("harbour players | avoids | ticket prices above $25") is unreachable by
  any message.

The person route does not help. Facts keyed to the sender come back without
any word overlap ([`facts_section.py`](../../agents/persona_runtime/facts_section.py),
lines 21–24), but what the operator says about an organisation is filed
under the organisation's topic subjects, not under the operator.

## Context

Found in EXP-001's first paid practice run, on 2026-10-01. Arm D holds a
series on one deployment, with a new channel for every meeting. The
briefing gave three facts: the hall is booked every Saturday in May, the
board caps tickets at $25, and the lighting crew is down to two. All four
advisers extracted them, 23 rows in all. None reached a later prompt: the
recall time stayed empty on every briefing row, and no later meeting's
opening message named any of the six briefing subjects. Counting every
message an adviser received as a possible stimulus, an upper bound, one
briefing fact was reachable in two of the four stores, and only because
other advisers wrote "Harbour Players". In two stores no message at all
could bring back the $25 cap: its rows were superseded by their siblings
([ISSUE-0181](ISSUE-0181-facts-extracted-together-supersede-each-other.md))
or filed under a person predicate. The episodic tier, the other route, was
closed by [ISSUE-0159](ISSUE-0159-episodic-score-inverts-bm25.md).

The whole-subject rule is deliberate. RFC 0026's
[topic amendment](../rfcs/0026-amendment-topic-subject-predicates.md)
chose a word-boundary match on the canonical fold so the read path stays
deterministic, with no model call in it (lines 65–74), and bounds what a
message can reach for security (lines 108–120 and 159–171). What the rule
costs in recall is not discussed there, and the subject-alias question,
RFC 0026's open question 3, was deferred with no issue filed.

## Impact

A persona remembers a topic fact only when someone later says its stored
label exactly. In EXP-001, arm D's facts tier carries nothing from one
meeting to the next. By the maintainer's call of 2026-10-02, this is fixed
before the scored run, next to ISSUE-0159 and ISSUE-0181.

## Proposed fix / investigation path

Each option must keep the amendment's bounds: topic seeds read topic
predicates only, the seed cap, and seed eligibility.

1. Anchor subjects when facts are written: give the extractor the store's
   live topic subjects so it reuses one instead of inventing another.
2. Match a subject by its content words rather than the whole phrase,
   still deterministic and capped.
3. Seed from the room's configured topic or description.
4. Let a topic seed read a person-predicate row filed under a topic
   subject. This weakens a bound the amendment kept on purpose.

Whichever lands, check which golden traces move and re-record them.

## Notes

> 2026-10-02 — filed from EXP-001's first practice run. The maintainer
> ruled the same day that the facts tier is fixed before the scored run,
> with ISSUE-0159 and ISSUE-0181; the choice among the options above comes
> back to the maintainer.
