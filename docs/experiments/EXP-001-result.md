# EXP-001 — Result

> **Status**: ✅ **Implemented** — the scored run is held and scored, and this is its result. It is written once.
> **Last updated**: 2026-11-27
> **Reports on**: the [EXP-001 pre-registration](EXP-001-preregistration.md) and its [part 2](EXP-001-preregistration-scoring.md), under the [AI raters deviation](EXP-001-deviation-ai-raters.md)
> **Second part**: [how the run was scored](EXP-001-result-scoring.md) · **Records**: [`evaluators/experiments/EXP-001/runs/`](../../evaluators/experiments/EXP-001/runs/README.md)

Two AI raters scored this run in the place of the two people the
pre-registration asks for. The maintainer decided that on 2026-11-10, and the
[deviation](EXP-001-deviation-ai-raters.md) records it. So **no rule fires as
registered**.

On the AI-scored figures, **rule 1 fires**. Arm C, the governed discussion,
does not clearly beat arm A, one model call that plays all four advisers.
It does not beat it at all: at $1.00 a quality point, C's memos were worth
less than A's, on average, in each of the five series.

**The limit.** The arms (`claude-sonnet-4-6`), the judge (`claude-opus-5`)
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
two AI raters, and the judge. A memo's quality is the average of their three
totals, out of 10.

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
and the interval around it. X beats Y only when the whole interval is above
zero.

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
they answer no recall question. The control plans are the ones no earlier
fact bears on; they show whether memory gets in the way.

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

| Arm | Closed by the idle window | Closed by the depth cap | Memos cut at 400 words | Lowest energy at a memo turn |
|---|---|---|---|---|
| B | 11 | 19 | 6 of 20 | 0.55 |
| C | 19 | 11 | 6 of 20 | 0.60 |
| D | 29 | 1 | 10 of 20 | 0.70 |
| D′ | 26 | 4 | 14 of 20 | 0.70 |

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

The limits the pre-registration named before the run still hold
([part 1, §4](EXP-001-preregistration.md#4-what-it-cannot-show)): one job,
one model, one language, invented organisations, and one run of each arm for
each series. The deviation adds its own
([§4](EXP-001-deviation-ai-raters.md#4-the-limit)), stated at the top of this
document.

The sequencing Amendment 2026-10-18 adds a third. EXP-001 measures a short
job: six meetings, each ending in one memo. It cannot show whether a society
of agents keeps making progress on a long task.

## 8. What changed after the pre-registration merged

The pre-registration merged on 2026-09-18
([#969](https://github.com/mkhomutov/Persatrix/pull/969)).

- **The two documents.** One PR touched them:
  [#984](https://github.com/mkhomutov/Persatrix/pull/984) added a link to
  the harness document under part 1's related documentation. No rule, arm,
  price or figure changed.
- **The materials and the spend caps.** No PR changed them.
- **What a fired rule decides, and which review receives it.** The strategy
  review of 2026-10-18 and the amendment that ratifies it, by rulings (h)
  and (l) ([§1](#1-what-rule-1-says-and-what-it-now-decides)). This is the
  route [part 1, §5](EXP-001-preregistration.md#5-changes) leaves for a
  change to part 2.
- **The harness, before the first scored meeting.** PRs
  [#984](https://github.com/mkhomutov/Persatrix/pull/984),
  [#986](https://github.com/mkhomutov/Persatrix/pull/986),
  [#987](https://github.com/mkhomutov/Persatrix/pull/987),
  [#988](https://github.com/mkhomutov/Persatrix/pull/988),
  [#992](https://github.com/mkhomutov/Persatrix/pull/992),
  [#993](https://github.com/mkhomutov/Persatrix/pull/993),
  [#1013](https://github.com/mkhomutov/Persatrix/pull/1013),
  [#1015](https://github.com/mkhomutov/Persatrix/pull/1015),
  [#1018](https://github.com/mkhomutov/Persatrix/pull/1018),
  [#1021](https://github.com/mkhomutov/Persatrix/pull/1021),
  [#1023](https://github.com/mkhomutov/Persatrix/pull/1023),
  [#1024](https://github.com/mkhomutov/Persatrix/pull/1024),
  [#1032](https://github.com/mkhomutov/Persatrix/pull/1032) and
  [#1036](https://github.com/mkhomutov/Persatrix/pull/1036). The
  [harness document](EXP-001-harness.md) says what each one adds, and the
  [frozen choices](EXP-001-harness-choices.md) list what each one chose.
- **The harness, after the first scored meeting.** Part 1 §5 allows no
  change from then on, so each of these is named here:
  - [#1091](https://github.com/mkhomutov/Persatrix/pull/1091), harness PR
    6d, merged on 2026-11-10 with the seal shut. It adds the scoring step
    PR 6c left to it: the score sheets, opening the seal, and the result's
    figures. It changes no mechanic the run was held with, and the
    arithmetic is PR 2's
    ([where it stands](EXP-001-harness-choices-scoring.md)).
  - [#1095](https://github.com/mkhomutov/Persatrix/pull/1095), merged on
    2026-11-10, is the deviation. It adds the AI raters' commands:
    `rater-batches`, `rater-brief`, `rater-enter`, `rater-keep`,
    `rater-audit`, `rater-same-notes`, `rater-ready` and
    `rater-differences`.
  - [#1140](https://github.com/mkhomutov/Persatrix/pull/1140), merged on
    2026-11-20, corrects the check on a rater run's record, after the
    practice refused two runs that had followed the rules
    ([second part](EXP-001-result-scoring.md#2-the-runs-and-the-ones-held-again)).
- **The raters.** Two AI raters in the two people's place, with eight
  smaller differences from the deviation's own rules
  ([second part](EXP-001-result-scoring.md#4-eight-differences-from-the-rater-rules)).
- **Discarded outputs.** None.

## 9. What is published with this result

Everything is under
[`evaluators/experiments/EXP-001/runs/`](../../evaluators/experiments/EXP-001/runs/README.md),
copied byte for byte from the run's own files:

- **The transcripts and memos**: for each series and arm, every meeting's
  record, with each message posted, the memo or recall reply, what closed
  the discussion and each adviser's energy.
- **The result as the harness wrote it**: `scores.json` and `scores.txt`,
  which every figure here comes from; the scored run's report; and
  `opened.json`, which holds the moment the seal opened and a SHA-256 of
  each sheet, the judge's answers and calls, the packets, the seal and the
  result.
- **The AI raters' records**, as
  [rule 6](EXP-001-deviation-ai-raters.md#5-the-rater-rules) lists them:
  each prompt, each entered or kept answer, each run's public record, and
  each session's `runs.jsonl`. With them, the
  [notes](../../evaluators/experiments/EXP-001/runs/ai-raters/notes.md)
  both raters accepted in practice.

Two files are held back: the public records of the two refused practice
runs. Rule 6 has the maintainer read a refused run's copy before it is
published.

The maintainer keeps the rest unpublished: each run's full record, the
advisers' stores and logs, the per-call logs, the packets file, the seal,
the judge's answers and the two sheets.

**A cross-check.** The session that wrote this document copied the run
without the advisers' stores and logs, and had the harness at `ff411f9`
score the copy again. The harness accepted every hash `opened.json` holds
and wrote `scores.json` and `scores.txt` again, byte for byte the same.
Every figure here is from those two files; nothing was worked out apart
from them.

## 10. What happens next

The first strategy review held after this result logs the rule, with part
2's readings and the two decisions of
[§1](#1-what-rule-1-says-and-what-it-now-decides) beside it. Any decision it
changes is ratified by a sequencing amendment.

## Related documentation

- [How the run was scored](EXP-001-result-scoring.md) — the AI raters'
  runs, the eight differences from their rules, and who read what, and
  when.
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
