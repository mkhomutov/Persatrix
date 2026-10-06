---
id: ISSUE-0180
summary: "A topic fact reaches a later prompt only when the incoming message repeats its stored subject word for word. The extractor invents a subject at every close (\"harbour players hall\"), so paraphrase, a partial name or a subject that splintered across closes all miss; seeds are capped at three, newest first, and a short subject also matches inside a longer one. In EXP-001's first practice run, none of the 23 rows arm D's four advisers stored from the briefing reached a later meeting's prompt."
status: resolved
severity: medium
area: memory
created: 2026-10-02
closed: 2026-10-06
closed_pr: 1035
refs:
  - agents/persona_runtime/topic_seeds.py
  - agents/persona_runtime/facts_section.py
  - agents/persona_runtime/channel_roster.py
  - agents/persona_runtime/memory_context.py
  - agents/memory/facts.py
  - agents/memory/_facts_topics.py
  - prompts/runtime/safety/fact-extractor-suffix.md
  - docs/rfcs/0026-amendment-topic-subject-predicates.md
  - docs/rfcs/0026-declarative-facts-tier.md
  - docs/issues/ISSUE-0180-design-record.md
  - docs/issues/ISSUE-0159-episodic-score-inverts-bm25.md
  - docs/issues/ISSUE-0181-facts-extracted-together-supersede-each-other.md
  - docs/issues/ISSUE-0183-overlapping-fact-writes-leave-no-live-fact.md
  - docs/issues/ISSUE-0184-fact-recall-cap-runs-before-the-gate.md
  - docs/issues/ISSUE-0185-what-the-room-subject-seed-leaves-open.md
  - docs/experiments/EXP-001-harness.md
  - tests/unit/python/test_room_topic_seeds.py
  - tests/unit/python/test_memory_context_room_seeds.py
  - tests/unit/python/test_fact_store_recall_order.py
---

# ISSUE-0180: A topic fact comes back only when a message repeats its exact subject

## Summary

The facts tier stores what a persona learns about a topic under a subject
the extractor names at each close, such as "harbour players hall" or
"event planning". It brings such a fact back in exactly one way: the
incoming message must contain the whole subject as a run of words, after
case folding
([`match_topic_subjects`](../../agents/persona_runtime/topic_seeds.py)
and its regex). A message that says "the hall", or "our own
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

## Fix

Two halves, which the
[measurements of 2026-10-06](ISSUE-0180-design-record.md) showed are both
needed: the aim of option 1, reached through the extractor's prompt, and
option 3. The amendment's bounds hold: three topic seeds a turn, topic
predicates only, and the same eligibility rule. No schema change and no
migration.

**The extractor files an organisation's facts under the organisation.**
[The extractor prompt](../../prompts/runtime/safety/fact-extractor-suffix.md)
gains one sentence. When a conversation is about one organisation, team
or client, every `topic.*` tuple about it takes that organisation's own
name as its subject, and the part, event or document goes in the object.
Option 1 as written, a list of stored subjects, could not do this: the
briefing's subjects are chosen while the store is empty.

**The room's description seeds recall.**
[`topic_seeds.py`](../../agents/persona_runtime/topic_seeds.py) matches
the store's topic subjects against a second text, the acting room's
description, by the same whole-subject rule it applies to the message.
A subject the description names seeds a turn in that room whatever the
message says; the list below says when it does not. The description
comes with the roster
([`channel_roster.py`](../../agents/persona_runtime/channel_roster.py)),
which the turn now waits for before it recalls facts.

Four choices go beyond the letter of option 3:

- **The description's first-named subject seeds first.** The room's
  subjects are taken in the order its description names them, not the
  order the store last heard of them, so a fact filed later under a
  part the description mentions cannot take the seed from the
  organisation it names first. The message's subjects come next and the
  description's others last. The cap stays three seeds in all. Beside a
  subject the message names, the room takes at most two
  (`ROOM_SEED_LIMIT`); when the message names none, it may take all
  three.
- **Beside a subject the message names, the room's first subject is
  read six rows deep.** The facts section spends its tokens in seed
  order and gives no subject a share. Read to the full cap, a dozen
  facts under the room's subject would leave the message's subject no
  line. On such a turn the room's first subject gives its three newest
  and three oldest facts (`ROOM_SEED_ROWS_BESIDE_MESSAGE`).
- **A subject the room names is read from both ends.**
  [`FactStore.recall`](../../agents/memory/facts.py) gains
  `order="both_ends"`: the newest fact, the oldest, the second newest,
  the second oldest, and so on. A read stops at 20 rows and the facts
  section at 200 tokens. Such a subject is recalled turn after turn and
  every later room adds to it, so read newest first it would lose what
  the first room said first. A subject only the message names still
  reads newest first, since it answers what was just said. One both
  texts name is read from both ends in that room, so there a read that
  existed before this change returns its facts in a new order.
- **Only the description is matched,** not the channel's name or its
  autonomous topic.

Measured on arm D of the practice run, through this change's own recall
code and with no further model calls: on the stores that each of the two
re-extractions with the new sentence gives, all three briefing facts
are in all 25 prompts, the chair's three memo turns included. They stay
there when four more meetings like the first plan meeting are added to
the store, six plan meetings in all. Read newest first, the same stores
lose the briefing for two or three of the four advisers once three such
meetings are there. The result rests on the briefing being the first
thing filed under the organisation: in all 28 extractions of the
operator's briefing record, its three facts are the subject's three
oldest rows. One of the 25 turns names a stored subject of its own
beside the room's.

What the fix leaves
([ISSUE-0185](ISSUE-0185-what-the-room-subject-seed-leaves-open.md)
tracks the open ones):

- **Only a record that names the organisation files under it.** In the
  practice briefing that is the operator's record alone; the advisers'
  replies still go under "event planning" or "event". Nothing merges
  those subjects, and a subject no description names is still reached
  only by its exact wording.
- **The channel's name can be taken for an organisation.** When a
  record's text names none, the new sentence sometimes makes the
  extractor use the name in the prompt's own header: the chair's
  memo-request record filed three tuples under `advice-2` or `advice-3`
  in each pass. Such a subject seeds only in a channel whose
  description contains that name as a word. No practice channel's does.
  The shipped `planning` channel's does, so a subject `planning` would
  seed on every turn there; whether the extractor files one for that
  channel is unmeasured, since it needs a model call.
- **The seed does not always fire, and nothing above DEBUG says so.**
  The description and the stored subject must match word for word: an
  article, "&" for "and", an abbreviation or an extra word misses. The
  subject must be among the 200 topic subjects asserted most recently,
  and at least three characters long. A turn whose roster fetch fails
  has no description to match. And a subject taught in another channel
  seeds only while `memory.facts.cross_room` is `live`, so setting it
  to `shadow` or `off` switches this off for such subjects.
- **Records closed in one meeting still replace each other's facts** on
  a shared subject and predicate
  ([ISSUE-0181](ISSUE-0181-facts-extracted-together-supersede-each-other.md)'s
  limits, [ISSUE-0183](ISSUE-0183-overlapping-fact-writes-leave-no-live-fact.md)),
  and more facts now share a subject. So the read from both ends keeps
  what a room was first told safe from later rooms, not from a later
  record in the same room. In the two re-extractions 8 and 7 rows were
  replaced by another record's, none with the shipped wording, and in
  2 of the 15 groups of records closed at one instant two records wrote
  one subject and predicate, none before (both under "spring gala"). In
  the practice briefing no reply named the theatre, so nothing else
  wrote its subject there. In the later meetings 7 of 25 replies did. A
  reply that names the organisation and files one `topic.has_status`
  under it replaces the operator's three if it arrives later. Filed as
  one standing channel, not a channel per meeting, the same extractions
  leave none of the operator's three briefing facts live for three of
  the four advisers after the first plan meeting.
- **The read from both ends keeps the oldest facts, right or wrong.**
  It drops the middle of a long list. When another room later corrects
  what the first room said, both facts stay live, since a fact replaces
  another only inside one room. Once the list outgrows the caps, the
  first value is printed without its correction, and a fact line carries
  no date. And the oldest rows are the briefing only where the briefing
  came first: with eight older facts under the subject and eight newer,
  a three-fact briefing sits in the dropped middle.
- **The facts section is still spent in seed order.** The persona's own
  facts come first, then the sender's, the room's first subject, the
  message's subjects and the room's others. Only the room's first
  subject is capped, and only beside a subject the message names. Eight
  facts about the persona and twenty about the sender still leave no
  line for any topic, as before this change.
- **The audience check admits what it cannot check.** A fact from
  another channel is withheld when this channel holds someone that
  channel does not, but admitted when that channel's roster cannot be
  fetched (the shipped policy, `ENFORCED_VERDICTS` in
  [`audience.py`](../../agents/persona_runtime/audience.py)). Before,
  such a fact reached the check only on a turn whose message named its
  subject. Under a subject the description names it reaches it on every
  turn. So while a source channel's roster fetch fails, or once that
  channel is deleted, a fact told in a DM can be in a group channel's
  prompt turn after turn. Whether a standing seed should fail closed is
  the maintainer's call.
- **One owner per organisation.** `topic.owned_by` holds one value per
  subject. With the part moved into the object, two owners of two parts
  of one organisation, stated in one record, would leave one. In the
  re-extractions 5 of 13 and 4 of 12 owner tuples sit under the
  organisation, none of 18 with the shipped wording. No practice record
  names two owners, so the loss itself was not seen.
- **The 20-row cap still runs before the confidentiality gate**
  ([ISSUE-0184](ISSUE-0184-fact-recall-cap-runs-before-the-gate.md));
  for a subject the room names it now keeps ten rows from each end,
  three beside a subject the message names.
- **Option 4 was not taken.** A person predicate filed under a topic
  subject is still out of a topic seed's reach. The new sentence leaves
  few: none under the organisation's name in its two passes, where the
  shipped wording left seven in one.

Cost: the sentence adds about 65 input tokens to every close-path call.
The roster request used to overlap every memory read of a turn; it now
overlaps only the two ahead of the facts tier, on a DM turn too, whose
roster carries no description. A subject the description names is
recalled on every turn, so every turn there also pays for that recall:
one roster request for each other channel the recalled facts came from
(the audience check fetches each again every turn, and each counts
against the REST rate limit), one write that marks the printed facts as
recalled, and the gate's warning and tripwire watch for a row it
withholds.

Tests:
[`test_room_topic_seeds.py`](../../tests/unit/python/test_room_topic_seeds.py)
pins which subjects seed, in what order, how deep and under which caps,
follows a briefing from the store to the prompt past both caps and
beside a subject the message names, and holds the shadow pass to the
live read.
[`test_memory_context_room_seeds.py`](../../tests/unit/python/test_memory_context_room_seeds.py)
pins the wiring on a whole turn, and
[`test_fact_store_recall_order.py`](../../tests/unit/python/test_fact_store_recall_order.py)
the new order. Of the 43 new tests, 35 are in the two new files, which
do not import on the unfixed tree, and the other eight fail there. Each
of 45 mutants of the change turns a test red. In the older
`test_topic_seeds.py`, two doubles could not take the call the code
makes, so the tests around them could not fail; they now take it. Its
three store-backed tests call the new seed function, which replaced
the one they called. No other existing test changed. Five of the six
golden traces moved, in their close-path
request alone (its hash and its token count), and were re-recorded
offline with the same replies. No golden exercises the new seeding
itself: the eval harness's roster carries no description, and a recipe
cannot declare one.

## Slot: merges before EXP-001, by the maintainer's calls of 2026-10-02 and 2026-10-06

- **No plan is needed**, as for
  [ISSUE-0181](ISSUE-0181-facts-extracted-together-supersede-each-other.md):
  ruling (a) of the
  [sequencing Amendment 2026-09-12](../v0.3.x-sequencing.md#amendment-2026-09-12--close-v0316-small-then-measure-before-any-train-opens)
  opens no plan before EXP-001 reports, and this is a standalone fix.
  Ruling (f) holds: no store migration.
- **The choice among the options** came back to the maintainer, who made
  it on 2026-10-06 with
  [those measurements](ISSUE-0180-design-record.md): the added sentence
  with option 3, and the facts budget covered.
- **EXP-001.** Arm D runs the memory code as shipped when the scored run
  is held, so this changes what arm D's facts tier carries between
  meetings. Every arm that closes a conversation pays the longer
  close-path prompt, and every adviser turn waits for the roster a
  little earlier. A practice run on the merged code, before the scored
  run, would show the eight checks still hold.

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

> 2026-10-06 — two sets of measurements decided the fix: how the four
> options above measured on the first practice run, with no model calls,
> and a paid probe of the extractor's prompt. **Split out to
> [the measurements record](ISSUE-0180-design-record.md)** when the Fix
> section took this file past its word limit.

> 2026-10-06 — the review of the fix changed three things in it. The
> room's subjects are ranked by the order its description names them,
> where they were ranked by which the store heard of last. Beside a
> subject the message names, the room's first subject is read six rows
> deep, where a long list under it left the message's subject no line.
> And a subject the description names is read from both ends in
> whichever slot it has. The practice replay gives the same counts after
> them. What the review found and the fix does not change is in "What
> the fix leaves" above and tracked in
> [ISSUE-0185](ISSUE-0185-what-the-room-subject-seed-leaves-open.md).
