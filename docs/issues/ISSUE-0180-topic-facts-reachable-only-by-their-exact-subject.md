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

> 2026-10-05 — [ISSUE-0181](ISSUE-0181-facts-extracted-together-supersede-each-other.md)'s
> fix covers only facts from one extraction. If option 1 here makes
> records reuse a stored subject, two records closed at the same instant
> will write the same key routinely, and the one whose extraction
> finishes later replaces the other's facts. Two such writes that
> overlap can leave neither live
> ([ISSUE-0183](ISSUE-0183-overlapping-fact-writes-leave-no-live-fact.md)).
> Decide them together.

> 2026-10-06 — the four options were measured on the first practice run,
> with no model calls. Each of the 25 turns where an arm D adviser spoke
> in the three later meetings was replayed through the shipped recall and
> prompt code, against the store that adviser held when the meeting
> began, written again with ISSUE-0181's fix
> ([#1034](https://github.com/mkhomutov/Persatrix/pull/1034)). Only the
> rule under test was swapped. As a check, the shipped rule on the run's
> own stores brings back the same two facts the run recalled, at the same
> times. The count is of prompts that hold a briefing fact: the hall
> booking, the $25 cap or the lighting crew. As shipped, none of the 25
> does.
>
> 1. **Option 1 changes nothing here.** An adviser's store gained a
>    topic subject 49 times. Sixteen were at the first close, when the
>    store was empty, and these hold every briefing fact. Twelve named
>    something that only a record closed with them had just named; those
>    extractions run side by side and cannot see each other's subjects.
>    Fifteen named a new topic. Six could have reused a stored subject,
>    all at the last close, and renaming those six leaves every count as
>    it was. Ideal naming does little for the shipped rule either: with
>    everything about the theatre under "harbour players", 3 of 25
>    prompts hold the briefing, because a message must still say
>    "Harbour Players". Merged subjects also cost facts under the
>    current write rule. Of the 121 stored facts, 31 to 44 stop being
>    live, nearly all because records closed together replace each
>    other's facts on a shared subject and predicate (the 2026-10-05
>    note above).
> 2. **Option 2 helps, mostly through one word.** Seeding a subject when
>    the message shares any one of its content words puts a briefing
>    fact in 17 of 25 prompts and all three in 8. But the advisers filed
>    the briefing under "event plan" or "event planning", and every
>    opening message says "plan". With that word ignored, the $25 cap
>    falls from 15 prompts to 3 and the lighting crew from 15 to 4. The
>    recall questions' own words match no subject, and the chair's memo
>    turns get nothing. Asking for every word, or most, reaches 3 or 4
>    prompts. A made-up subject such as "plan review" would be seeded on
>    18 of 25 turns, where today it needs the whole phrase.
> 3. **Option 3 as written gives one fact.** Only the subject "harbour
>    players" appears whole in the room's description, and it holds the
>    lighting crew for two advisers: 9 of 25 prompts, none of them the
>    chair's. Seeding a subject when all its words are in the
>    description adds the hall booking to all 25, the chair's memo turns
>    included, but only because the description says "hall". With half
>    its words, up to five "harbour players …" subjects compete for
>    three seeds once the first plan meeting has added its own, and the
>    newest ones win, so the $25 cap reaches 6 prompts. The description
>    already reaches the agent on every channel turn, with the roster,
>    but after fact recall has run. A planted subject that is a phrase
>    of the description would be seeded on every turn in the room.
> 4. **Option 4 adds routes, not facts.** Five briefing rows sit under a
>    person predicate, `avoids`. With ISSUE-0181's fix, each of those
>    facts is also live under a topic predicate in the same store. Alone
>    it changes nothing. With option 2 the hall booking goes from 10
>    prompts to 17, and with option 3 as written the $25 cap goes from 0
>    to 9. The chair gains nothing. Reading every row fails
>    `test_topic_seed_reads_only_topic_rows`, the test that pins the
>    bound, and so do two of the three narrower forms tried. Reading
>    only preference and commitment predicates passes it. In a check
>    with one planted topic tuple naming a colleague, that form still
>    showed the colleague's stated dislike on 3 of 20 turns from other
>    senders once option 2 was in.
>
> No option, alone or paired with another, puts all three facts into the
> chair's memo prompt in the recall meeting within the cap of three
> seeds. One combination does, in all 25 prompts: one subject per
> organisation from the first close, option 3 as written, and
> ISSUE-0181's fix. A list of stored subjects cannot produce that
> naming, since it is chosen while the store is empty. Whether a line in
> the extractor prompt can is untested and needs model calls.
>
> What this does not show: the transcripts are held fixed, so it counts
> which facts reach a prompt, not what the advisers would then say. It
> is one run and four stores, and the rules were tried on the practice
> series only. In the unit and integration suites no test fails under
> option 2, and option 4 fails only the test named above. No golden
> trace moves under options 2 or 4, and option 3 cannot move one, since
> no room in a golden has a description. None of them covers these
> cases: the goldens hold one-word subjects.
