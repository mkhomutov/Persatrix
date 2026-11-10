# EXP-001 — Deviation: two AI raters in the two people's place

> **Status**: 🚧 **In Progress** — fixed when it merges, before any rater reads a packet from the scored run.
> **Last updated**: 2026-11-10
> **Departs from**: [pre-registration part 2, §2](EXP-001-preregistration-scoring.md#2-raters-and-blinding) · **Decided by**: the maintainer, on 2026-11-10

Part 2 of EXP-001's pre-registration has three raters score every memo and
every recall answer: two people and the
[LLM judge](../methodology/process-glossary.md#llm-judge). On 2026-11-10 the
maintainer decided that two
[AI raters](../methodology/process-glossary.md#ai-rater) score in the two
people's place, and that the result says so in its first lines. This
document records that change before any rater reads a packet from the
scored run. It says what the result may claim because of it, where its
limit lies, and the rules the two AI raters follow.

## 1. What changed, and who decided

The maintainer decided it on 2026-11-10, in their own planning notes, and
confirmed it the same day in the session that scores the run. Their reason:
they trust AI raters more.

**What is replaced.** Part 2 §2's two people. Rater 1 fills person-1's score
sheet from person-1's packets, and rater 2 fills person-2's from
person-2's, so the harness reads both sheets as it always would. Each
rater still works alone, in its own packet order.

**What stays.** Everything else in part 2:

- the rubric and its anchors;
- three raters, averaged: the two AI raters and the judge, which already
  scored every packet as registered;
- the agreement measure and its 0.4 floor;
- the 21 days for scoring, which end on 2026-11-27 at 18:12 UTC;
- the checks and the rules of §5.

Blinding stays too. The packets carry random IDs and no adviser names, and
the map from ID to arm stays sealed until every score is in. Part 1, the
materials and the caps are untouched.

## 2. Why this is a deviation, not a change to part 2

[Part 1 §5](EXP-001-preregistration.md#5-changes) lets part 2 change only
through a strategy review and the amendment that ratifies it. It also says
that from the first scored meeting on, nothing changes: a problem found then
is reported in the result, not fixed. The scored run's first meeting was
held on 2026-11-05.

So part 2 stays as it was merged. This document names the departure from
it, before any rater reads a scored packet, and the result reports it
first.

## 3. What the result may claim

- **No rule fires as registered.** Part 2 asks for two people, so the rule
  the scores pick is not the pre-registered decision. The result reports
  the rule the AI-scored figures fire, as what those figures show.
- **Its first lines say so.** They say that two AI raters replaced the two
  people, that the maintainer decided it on 2026-11-10, and they link here.
- **§7's people-alone rule changes.** "The rule that would fire on the two
  people's scores alone" becomes the rule on the two AI raters' scores
  alone.
- **Incomplete and inconclusive mean what they meant.** An agreement below
  0.4, scoring that finishes late, or a judge stopped by its cap still makes
  the result inconclusive.
- **The strategy review decides what it is worth.** The first strategy
  review after the result receives it like any result. It records the rule
  the AI-scored figures fire, and decides how much weight that rule
  carries. Any decision it changes is ratified by a sequencing amendment,
  as usual.
- **Who read what, and when.** Part 2 §2 asks the result to say this, and it
  now covers both AI raters' runs and the sessions that brief them (§6).

## 4. The limit

The arms (`claude-sonnet-4-6`), the judge (`claude-opus-5`) and both AI
raters (Fable 5.2) are all Claude models from one provider, and both AI
raters run the same model. Models from one family can share blind spots and
agree for the same wrong reason. So the three raters' agreement is not an
independent check, and a high agreement figure says less than two people's
would.

All five arms run one model, so a rater's leaning towards its own family
cannot favour one arm over another. It can still favour, or mark down,
every arm alike.

Part 2 §2 asks that at least one person did not run the harness. No AI
rater runs it, but the sessions that brief them run two of its commands.

## 5. The rater rules

These rules are fixed when this document merges, before the seal opens. The
harness's [`ai_raters`](../../evaluators/exp001/ai_raters.py) commands hold
each rater's session to them.

1. **The model.** Both raters run on Fable 5.2, the maintainer's choice. It
   is neither the judge's model nor the arms'. Each run gives its exact
   model ID on its answer's first line. `rater-enter` refuses an answer
   from any other model, and the result records the ID.
2. **One session per rater.** Each rater has its own Claude Code session on
   that model, which briefs that rater and no other:
   - `rater-batches` writes the rater's packets as prompts;
   - the session gives each prompt to a fresh run;
   - `rater-enter` enters each answer in the rater's sheet.

   The session never opens a packet file to read it; the commands do. The
   session that built the harness briefs neither rater, and never reads a
   packet from the scored run.
3. **A run.** A fresh run of the rater's model, a subagent of the rater's
   session, gets one prompt and nothing else. A prompt holds:
   - the rubric, as the people would have had it;
   - the notes agreed in practice (rule 7);
   - eight packets in the rater's own order, or fewer in the last batch,
     each followed by the cells to score.

   The run reads its prompt file and calls no other tool. A run whose
   record shows any other tool call is discarded and held again whole.
4. **Off limits.** Neither a run nor a rater's session reads:
   - the seal, `sealed/`;
   - the judge's answers and calls, `judging/`;
   - the meetings' transcripts, `pairs/`;
   - `packets.json`;
   - the run's report;
   - the other rater's packet file or sheet.

   A run reads nothing but its prompt.
5. **Scores as given.** `rater-enter` refuses an answer that does not give
   exactly the cells asked for, with 0, 1 or 2 for a criterion and right or
   wrong for a recall question. It never changes a cell already scored, and
   enters nothing once the seal is open. A run that fails is held again
   whole, from the same prompt: refused, cut off, or calling another tool.
   The result lists every run held again, and why.
6. **Everything is kept.** These stay outside the repository until every
   score is in, then are published with the result:
   - each run's prompt, answer and record;
   - each session's log of what it entered, `entered.jsonl`.
7. **Practice first.** As part 2 §2 has the people do, both raters first
   score the practice run's packets, the same way but with no notes yet.
   `rater-differences` lists the cells they scored differently. Each
   rater's session then gives a fresh run its practice prompts, both
   raters' practice scores and those differences, and asks for short notes
   on how the anchors apply. The two lists are put side by side, and each
   session gives a fresh run the combined list to accept or reject note by
   note. The notes both accept go into every scored prompt and are
   published with the result. The practice scores are thrown away.
8. **Opening the seal.** The session that built the harness runs `score`,
   which writes the blank sheets and reports progress. Only once every
   score is in, and after telling the maintainer, does it open the seal
   with `score --open`, by 2026-11-27 18:12 UTC.

## 6. Who read what, and when, so far

The result completes this record. What is known when this document merges:

- 2026-11-05 and 06: the scored run was held, its packets drawn and the
  judge's scores taken, by the harness without anyone reading a packet.
- To 2026-11-10: the session that built the harness opened nothing in the
  scored run's directory.
- 2026-11-10: a planning session listed the names, sizes and dates of the
  files in `~/exp001`, `~/exp001/scored` and the scored window's five
  folders. It opened none of them.

## Related documentation

- [EXP-001 pre-registration, part 2](EXP-001-preregistration-scoring.md) —
  scoring as registered, which this document departs from.
- [The harness, third part](EXP-001-harness-scoring.md) — scoring a run,
  and the AI raters' commands.
- [Process glossary](../methodology/process-glossary.md#ai-rater) — AI rater
  and LLM judge.
