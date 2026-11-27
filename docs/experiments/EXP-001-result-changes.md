# EXP-001 — Result, third part: what changed, and what is published

> **Status**: ✅ **Implemented** — written once, with the [result](EXP-001-result.md).
> **Last updated**: 2026-11-27
> **Part of**: the [EXP-001 result](EXP-001-result.md), which gives the rule and every figure; the [second part](EXP-001-result-scoring.md) says how the run was scored. This part lists every change made after the pre-registration merged, and every record published with the result.

The pre-registration asks the result to list every PR that changed its
documents, its materials or the harness, and to publish the transcripts
([part 1, §5 and §6](EXP-001-preregistration.md#5-changes)). The
[deviation](EXP-001-deviation-ai-raters.md) adds the AI raters' records.
Both lists are here.

## 1. What changed after the pre-registration merged

The pre-registration merged on 2026-09-17
([#969](https://github.com/mkhomutov/Persatrix/pull/969)). Dates in this
section are UTC; the repository's list of merged PRs gives local dates, a
day later for #969 and #1140.

- **The two documents.** One PR touched them:
  [#984](https://github.com/mkhomutov/Persatrix/pull/984) added a link to
  the harness document under part 1's related documentation. No rule, arm,
  price or figure changed.
- **The materials and the spend caps.** No PR changed them.
- **What a fired rule decides, and which review receives it.** The strategy
  review of 2026-10-18 and the amendment that ratifies it, by rulings (h)
  and (l) ([result §1](EXP-001-result.md#1-what-rule-1-says-and-what-it-now-decides)). This is the
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
- **The product the arms ran.** The arms ran Persatrix as it stood at
  `c6ef01d`, not as it stood when the pre-registration merged. In between,
  the product changed like any other code. Four fixes to memory recall are
  named here because the practice runs found them or waited for them:
  [#972](https://github.com/mkhomutov/Persatrix/pull/972),
  [#1030](https://github.com/mkhomutov/Persatrix/pull/1030),
  [#1034](https://github.com/mkhomutov/Persatrix/pull/1034) and
  [#1035](https://github.com/mkhomutov/Persatrix/pull/1035). Before the
  last two, a practice run showed that arm D's facts tier never brought
  back the briefing
  ([what each practice run showed](EXP-001-harness-run.md)).
  Arm D's figures are for memory as shipped at `c6ef01d`, with those fixes
  in.
- **The harness, after the first scored meeting.** Part 1 §5 allows no
  change from then on, so each of these is named here:
  - [#1091](https://github.com/mkhomutov/Persatrix/pull/1091), harness PR
    6d, merged on 2026-11-10 with the seal shut. It adds the scoring step
    PR 6c left to it: the score sheets, opening the seal, and the result's
    figures. It changes no mechanic the run was held with, and the
    arithmetic is PR 2's. Its choices were made blind to every score, but
    not to dollars, which the run's report already gave; none of them
    changes how dollars are counted
    ([where it stands](EXP-001-harness-choices-scoring.md)).
  - [#1095](https://github.com/mkhomutov/Persatrix/pull/1095), merged on
    2026-11-10, is the deviation. It adds the AI raters' commands:
    `rater-batches`, `rater-brief`, `rater-enter`, `rater-keep`,
    `rater-audit`, `rater-same-notes`, `rater-ready` and
    `rater-differences`. It also has the harness's own result name the
    deviation before any rule.
  - [#1140](https://github.com/mkhomutov/Persatrix/pull/1140), merged on
    2026-11-20, corrects the check on a rater run's record, after the
    practice refused two runs that had followed the rules
    ([second part](EXP-001-result-scoring.md#2-the-runs-and-the-ones-held-again)).
- **The raters.** Two AI raters in the two people's place, with eight
  smaller differences from the deviation's own rules
  ([second part](EXP-001-result-scoring.md#4-eight-differences-from-the-rater-rules)).
- **Discarded outputs.** None.

## 2. What is published with this result

Everything is under
[`evaluators/experiments/EXP-001/runs/`](../../evaluators/experiments/EXP-001/runs/README.md),
copied byte for byte from the run's own files:

- **The transcripts and memos**: for each series and arm, every meeting's
  record, with each message posted, the memo or recall reply, what closed
  the discussion and each adviser's energy.
- **The result as the harness wrote it**: `scores.json` and `scores.txt`,
  which the result's scores, comparisons and per-arm figures come from; the
  scored run's report; and `opened.json`, which holds the moment the seal opened and a SHA-256 of
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

**A cross-check.** The session that wrote the result copied the run
without the advisers' stores and logs, and had the harness at `ff411f9`
score the copy again. The harness accepted every hash `opened.json` holds
and wrote `scores.json` and `scores.txt` again, byte for byte the same.
Every score, comparison and per-arm figure in the result is from those two
files. The run's dates, spend and call counts are from its report, the arm
order is from the transcripts' times, and the counts in
[the result's §6](EXP-001-result.md#6-each-meeting) are tallies of the
harness's per-meeting list.

## Related documentation

- [EXP-001 result](EXP-001-result.md) — the rule and the figures.
- [How the run was scored](EXP-001-result-scoring.md) — the AI raters'
  runs and who read what, and when.
- [The records](../../evaluators/experiments/EXP-001/runs/README.md) — what
  each published file holds.
- [EXP-001 harness](EXP-001-harness.md) — every harness PR and the choices
  each one froze.
