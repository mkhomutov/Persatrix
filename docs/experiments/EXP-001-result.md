# EXP-001 — Result

> **Status**: ✅ **Implemented** — the scored run is held and scored, and this is its result. It is written once.
> **Last updated**: 2026-11-27
> **Reports on**: the [EXP-001 pre-registration](EXP-001-preregistration.md) and its [part 2](EXP-001-preregistration-scoring.md), under the [AI raters deviation](EXP-001-deviation-ai-raters.md)
> **Second part**: [how the run was scored](EXP-001-result-scoring.md) · **Third part**: [what changed, and what is published](EXP-001-result-changes.md) · **Records**: [`evaluators/experiments/EXP-001/runs/`](../../evaluators/experiments/EXP-001/runs/README.md)

Two [AI raters](../methodology/process-glossary.md#ai-rater) scored this run
in the place of the two people the pre-registration asks for. The maintainer decided that on 2026-11-10, and the
[deviation](EXP-001-deviation-ai-raters.md) records it. So **no rule fires as
registered**.

On the AI-scored figures, **rule 1 fires**. Arm C, the governed discussion,
does not clearly beat arm A, one model call that plays all four advisers.
It does not beat it at all: at $1.00 a quality point, C's memos were worth
less than A's, on average, in each of the five series.

**The limit.** The arms (`claude-sonnet-4-6`), the
[LLM judge](../methodology/process-glossary.md#llm-judge) (`claude-opus-5`)
and both AI raters (`claude-fable-5-2`) are all Claude models from one
provider, and both AI raters run the same model. Models from one family can
share blind spots and agree for the same wrong reason. So the raters'
agreement is not an independent check, and a high agreement figure says less
than two people's would ([deviation §4](EXP-001-deviation-ai-raters.md#4-the-limit)).

The run is complete, the raters' agreement is 0.775 against a floor of 0.4,
and scoring finished on time. So the result is neither incomplete nor
inconclusive.

## 1. What rule 1 says, and what it now decides

[Strategy §4](../project-strategy.md#4-the-decision-framework) fixed rule 1
before the run: if C does not clearly beat A on quality per dollar, stop
adding society features for this job and reposition on the gate alone.

Two later decisions bear on it, and the result names both beside the rule:

- **The [sequencing Amendment 2026-10-18](../v0.3.x-sequencing.md#amendment-2026-10-18--a-society-that-pursues-long-goals-exp-001-measures-v040-opens)**,
  written before the first scored meeting, made EXP-001 a measurement and
  not a gate. Its ruling (h) keeps what a rule says to build: under rule 1,
  no society features are added for the plan-critique job. What the rule
  says to pitch the project as no longer decides the pitch; the review
  records it as the pitch the rule would have chosen. No rule decides
  whether the society work goes on. Its ruling (l) sends this result to the
  first strategy review held after it is published. The amendment also
  records what the maintainer had seen by then: the judge's marks on the
  practice series, which put arm C below arm A.
- **The [deviation](EXP-001-deviation-ai-raters.md#3-what-the-result-may-claim)**
  fixed, before any rater read a scored packet, what a rule fired on
  AI-scored figures is worth. That review treats it as it would the
  registered rule, with the deviation and its limit stated beside it,
  whichever rule fired.

## 2. The run

- **When.** The run's first meeting started on 2026-11-05 at 19:19 UTC and
  its last ended on 2026-11-06 at 18:12 UTC, inside one seven-day window.
- **Code.** `main` at `c6ef01d`, the commit harness PR 6c merged
  ([#1036](https://github.com/mkhomutov/Persatrix/pull/1036)) and the
  practice run of 2026-10-16 passed on.
- **Meetings.** All five series are kept. Each of the five arms held six
  meetings with each organisation: 150 meetings, every one at its first
  try. No provider error was retried, no series started again, no harness
  fault stopped the run, and no output was discarded. Every answer was
  written: 100 memos and 25 recall replies.
- **Arm order**, drawn for each series from the stream seeded 2026:

  | Series | Order |
  |---|---|
  | 1 | A, C, D, D′, B |
  | 2 | D′, D, C, B, A |
  | 3 | B, A, C, D, D′ |
  | 4 | C, A, B, D, D′ |
  | 5 | C, B, D, D′, A |

- **Spend.** The arms made 3 527 calls and spent $44.09 of the $150 cap.
  The judge made 125 calls, none failed, and spent $4.44 of its $25 cap.
  The two AI raters cost no money beyond the maintainer's Claude Code plan
  ([second part](EXP-001-result-scoring.md#2-the-runs-and-the-ones-held-again)).

## 3. The raters, and how far they agree

Three raters scored every memo and recall reply: rater 1 and rater 2, the
two AI raters, and the judge. Each read the same **packets**. A packet is
one memo or recall reply, with the operator's messages and the answer key,
under a random ID that hides which arm wrote it. The **seal** is the map
from those IDs back to the arms, and it stays shut until every score is in.
A memo's quality is the average of the three raters' totals, out of 10.

| Pair | Agreement, plan by plan | Plans used | Plans skipped | Over all 100 memos at once |
|---|---|---|---|---|
| Rater 1 and rater 2 | 0.878 | 19 | 1 | 0.950 |
| Rater 1 and the judge | 0.656 | 18 | 2 | 0.847 |
| Rater 2 and the judge | 0.790 | 18 | 2 | 0.890 |

The three pairs average **0.775**, above the 0.4 floor, so the scores can
carry a decision. No memo was missing, so a skipped plan is one where a
rater gave all five memos the same total, which leaves nothing to rank.

The limit above applies here first: three Claude models agreeing is weaker
evidence than two people and a judge agreeing.

**On time.** Scoring was due by 2026-11-27 at 18:12:29 UTC, 21 days after the
last scored meeting. The seal was opened on 2026-11-26 at 19:48:29 UTC.

## 4. The checks

A memo's value is $1.00 times its quality, minus its arm's dollars per plan.
Each row compares two arms: the series differences in value, their average,
and the interval around it. The interval is
[part 2 §5](EXP-001-preregistration-scoring.md#5-how-a-rule-fires)'s: the
average, plus or minus 2.776 times the differences' standard deviation
divided by the square root of five. X beats Y only when the whole interval
is above zero.

| Comparison | Series 1 | Series 2 | Series 3 | Series 4 | Series 5 | Average | Interval | Verdict |
|---|---|---|---|---|---|---|---|---|
| C against A | −1.096 | −1.070 | −0.121 | −1.080 | −1.100 | −0.894 | −1.430 to −0.357 | C does not beat A |
| D against C | 0.179 | 1.285 | −1.359 | 1.367 | 1.435 | 0.581 | −0.910 to 2.072 | D does not beat C |
| D against D′ | −0.809 | −0.410 | −2.196 | 0.176 | −0.431 | −0.734 | −1.839 to 0.371 | D does not beat D′ |
| B against A | −1.459 | −0.832 | −1.317 | −1.266 | −0.255 | −1.026 | −1.635 to −0.417 | B does not beat A |
| C against B | 0.363 | −0.238 | 1.196 | 0.186 | −0.845 | 0.132 | −0.805 to 1.070 | C does not beat B |

**Check 1 decides.** "C clearly beats A" needs the whole interval above zero
and an average of at least $1.00. The interval lies wholly below zero. So C
does not clearly beat A, rule 1 fires, and the checks stop there.

The decision never read the other four rows. D against C and D against D′
would have been checks 2 and 3. B is in no rule.

For the deciding verdict, part 2 §7 asks for two more figures:

- **The price of a quality point.** "C clearly beats A" fails at every price
  from $0.00 up, not only at $1.00.
- **Repriced.** With salience bids and memory summaries priced at
  `claude-haiku-4-5`, the average is −0.855 and the interval −1.393 to
  −0.317. The verdict still fails, and rule 1 still fires.

**The two AI raters alone.** Part 2 §7's "two people's scores alone" is now
the two AI raters' sheets alone. On them rule 1 fires too, with an agreement
of 0.878.

## 5. Each arm

| Arm | Quality | On the 15 plans an earlier fact bears on | On the 5 control plans | Dollars a plan | Repriced | Quality a dollar | Minutes a plan | Quality a minute | Recall |
|---|---|---|---|---|---|---|---|---|---|
| A | 7.938 | 7.333 | 9.750 | $0.0136 | $0.0136 | 581.802 | 0.255 | 31.088 | 0 of 20 |
| B | 7.371 | 6.800 | 9.083 | $0.4729 | $0.4729 | 15.586 | 7.954 | 0.927 | 0 of 20 |
| C | 7.383 | 6.733 | 9.333 | $0.3530 | $0.3146 | 20.914 | 10.648 | 0.693 | 0 of 20 |
| D | 8.012 | 7.711 | 8.917 | $0.4009 | $0.3234 | 19.984 | 12.903 | 0.621 | 16 of 20 |
| D′ | 8.704 | 8.467 | 9.417 | $0.3585 | $0.3272 | 24.278 | 12.421 | 0.701 | 20 of 20 |

Arms A, B and C carry nothing from one meeting to the next, by design, so
they got no recall question right. The control plans are the ones no earlier
fact bears on; they show whether memory gets in the way.

**The cost ledger is not reported.**
[Part 1, §3](EXP-001-preregistration.md#3-running-it) says the
orchestrator's own cost ledger is reported beside these dollars. The
harness's result and the run's report do not hold it, so no ledger figure
is given here. The dollars above are the fixed table's, priced from every
recorded call.

Each criterion on its own, 0 to 2, averaged over every rater and memo.
Criterion 2 is averaged only over the plans that score it.

| Arm | 1: finds the plan's problems | 2: uses earlier facts | 3: one recommendation | 4: compares every option | 5: accurate, invents nothing |
|---|---|---|---|---|---|
| A | 1.967 | 0.178 | 2.000 | 2.000 | 1.350 |
| B | 2.000 | 0.178 | 2.000 | 1.933 | 0.850 |
| C | 2.000 | 0.200 | 2.000 | 1.950 | 0.817 |
| D | 1.867 | 1.267 | 1.900 | 1.817 | 1.033 |
| D′ | 1.950 | 1.667 | 1.833 | 1.867 | 1.333 |

Between A and C, the criterion that differs most is the fifth: 1.350
against 0.817.

## 6. Each meeting

Part 2 §7 asks, for each meeting, what closed the discussion, each adviser's
energy at the memo turn, and whether the memo was cut at 400 words. The
harness's own result lists all 150 meetings, one line each, under "Each
meeting" in
[`scores.txt`](../../evaluators/experiments/EXP-001/runs/scored/scores.txt).
Counted by arm:

| Arm | Closed by the idle window, of 30 meetings | Closed by the depth cap, of 30 meetings | Memos cut at 400 words, of 20 | Lowest energy at a memo turn |
|---|---|---|---|---|
| B | 11 | 19 | 6 | 0.55 |
| C | 19 | 11 | 6 | 0.60 |
| D | 29 | 1 | 10 | 0.70 |
| D′ | 26 | 4 | 14 | 0.70 |

- Arm A is one call a meeting. It has no discussion to close, and none of
  its memos was cut.
- **No discussion closed by an end vote**, by the round limit or by the cost
  bound, in any arm. Each closed at the depth cap, or at the idle window:
  the first 600 seconds with no message, which the harness counts as a
  close ([frozen choice](EXP-001-harness-choices-meetings.md)).
- Minutes run from the operator's message to the finished memo, so a
  discussion the idle window closed counts those ten quiet minutes.
- The longest memo was 494 words, in arm D′. Raters read its first 400.
- The highest energy was 0.90. One adviser, `crimson-crow`, has no energy
  recorded at series 3's recall check in arms C, D and D′.

## 7. What this result cannot show

The nine limits the pre-registration named before the run still hold
([part 1, §4](EXP-001-preregistration.md#4-what-it-cannot-show)). Among
them: one job, one model, one language, invented organisations, and one run
of each arm for each series.

One of the nine bears on the rule that fired. Every meeting had one
audience, so the audience gate was never tested. Rule 1 falls back on "the
gate alone", and that fallback is assumed to be worth something, not
measured.

The deviation adds its own limit
([§4](EXP-001-deviation-ai-raters.md#4-the-limit)), stated at the top of this
document.

The sequencing Amendment 2026-10-18 adds a third. EXP-001 measures a short
job: six meetings, each ending in one memo. It cannot show whether a society
of agents keeps making progress on a long task.

## 8. What changed, and what is published

The [third part](EXP-001-result-changes.md) lists both in full. In short:

- **Changes.** One PR touched the two pre-registration documents, to add a
  link. No PR changed the materials or the spend caps. The strategy review
  of 2026-10-18 and its amendment changed what a fired rule decides and
  which review receives it ([§1](#1-what-rule-1-says-and-what-it-now-decides)).
  Three harness PRs merged after the first scored meeting, from which part
  1 §5 allows no change, so each is named there: the scoring step
  ([#1091](https://github.com/mkhomutov/Persatrix/pull/1091)), the AI
  raters' commands
  ([#1095](https://github.com/mkhomutov/Persatrix/pull/1095)) and a
  correction to them
  ([#1140](https://github.com/mkhomutov/Persatrix/pull/1140)). No output
  was discarded.
- **Published.** The transcripts and memos, the result as the harness wrote
  it, and the AI raters' prompts, answers, public records and logs, all
  under
  [`evaluators/experiments/EXP-001/runs/`](../../evaluators/experiments/EXP-001/runs/README.md).
  The maintainer read the public records of the two refused practice runs
  on 2026-11-27, before they were published, as rule 6 of the deviation
  asks.
- **A cross-check.** Scored again from a copy, the run gave the same
  result, byte for byte
  ([how](EXP-001-result-changes.md#2-what-is-published-with-this-result)).

## 9. What happens next

The first strategy review held after this result logs the rule, with part
2's readings and the two decisions of
[§1](#1-what-rule-1-says-and-what-it-now-decides) beside it. Any decision it
changes is ratified by a sequencing amendment.

## Related documentation

- [How the run was scored](EXP-001-result-scoring.md) — the AI raters'
  runs, the eight differences from their rules, and who read what, and
  when.
- [What changed, and what is published](EXP-001-result-changes.md) — every
  PR that changed the documents, the materials or the harness, and every
  record published.
- [EXP-001 pre-registration](EXP-001-preregistration.md) and
  [part 2](EXP-001-preregistration-scoring.md) — the materials, the arms,
  the scoring and how a rule fires.
- [Deviation: two AI raters](EXP-001-deviation-ai-raters.md) — what
  changed, what the result may claim, and the rater rules.
- [The harness, third part](EXP-001-harness-scoring.md) — scoring a run and
  what its result holds.
- [Project strategy §4](../project-strategy.md#4-the-decision-framework) —
  the four decision rules.
- [Sequencing Amendment 2026-10-18](../v0.3.x-sequencing.md#amendment-2026-10-18--a-society-that-pursues-long-goals-exp-001-measures-v040-opens)
  — what a fired rule decides, and which review records it.
