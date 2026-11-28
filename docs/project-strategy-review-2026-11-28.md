# Project Strategy — The Review of 2026-11-28

> Companion to the [standing strategy review](project-strategy.md): the record
> of the review held on 2026-11-28 at `9ec80213`, the first after
> [EXP-001's result](experiments/EXP-001-result.md) was published on
> 2026-11-28, the day its pull request merged; the result document itself is
> dated 2026-11-27. Its row in the standing review's
> [log](project-strategy.md#10-review-cadence-and-log) is the summary. The
> [sequencing Amendment 2026-11-28](v0.4.x-sequencing-2026-11-28.md#rulings)
> ratifies its six rulings, as rulings (y) to (ad); nothing here binds before
> that amendment merges. The figures the review measured again are in
> the [evidence companion](project-strategy-evidence.md), and the findings it
> re-verified in the
> [four-reviews companion](project-strategy-four-reviews.md#1-what-the-four-reviews-established).

## 1. The rule it logs

No rule fires as registered: two
[AI raters](methodology/process-glossary.md#ai-rater) scored in the two
people's place, by the maintainer's decision of 2026-11-10. On the AI-scored
figures rule 1 fires: arm C, the governed discussion, does not clearly beat
arm A, one model call. At $1.00 a quality point, C's memos were worth $0.894
less than A's on average, and the whole interval lies below zero (−1.430 to
−0.357).

As the
[deviation](experiments/EXP-001-deviation-ai-raters.md#3-what-the-result-may-claim)
fixed in advance, this review treats that rule as it would the registered one,
with its limits beside it:

- The arms, the [LLM judge](methodology/process-glossary.md#llm-judge) and both
  AI raters are Claude models from one provider, and both raters run the same
  model. So their agreement is not an independent check
  ([deviation §4](experiments/EXP-001-deviation-ai-raters.md#4-the-limit)).
- Every meeting had one audience, so the gate that rule 1 falls back on was
  never tested. Its worth is assumed, not measured
  ([result §7](experiments/EXP-001-result.md#7-what-this-result-cannot-show)).
- EXP-001 is one short job on one model. It cannot show whether a society
  keeps making progress on a long task.

[Part 2 §5](experiments/EXP-001-preregistration-scoring.md#5-how-a-rule-fires)
fixed three readings of the rules before any run, and has the review that
logs the rule record them beside it:

- "Quality per dollar" is a memo's value at a price of $1.00 a quality point:
  the price times its quality, minus its arm's dollars per plan.
- Where rules 1 and 2 overlap, rule 1 comes first.
- Rule 3's "D′ matches D" means D does not beat D′.

Only the first bore on this result. C did not beat A at all, so the overlap
did not arise, and the checks stopped before rule 3.

Rulings (h) and (l) of the
[Amendment 2026-10-18](v0.3.x-sequencing.md#amendment-2026-10-18--a-society-that-pursues-long-goals-exp-001-measures-v040-opens)
say what the rule decides:

- What it says to build binds: no society features are added for the
  plan-critique job.
- What it says to pitch, the gate alone, reachable over MCP, is logged as the
  pitch the rule would have chosen. It decides nothing.
- No rule decides whether the society work goes on.

This review orders no repeat.
[Part 2 §6](experiments/EXP-001-preregistration-scoring.md#6-when-no-rule-fires)
allows one only after an incomplete or inconclusive result, and this result is
neither.

## 2. What has boarded by ruling (u)

Nothing. No sequencing amendment has been written since 2026-10-21 and EXP-002
has no pre-registration, so no addition has taken that route, and there is no
result to set beside one.

## 3. The maintainer's rulings

Each was put to the maintainer as a recommendation with its reason, and the
maintainer accepted all six as put on 2026-11-28. None binds until the next
sequencing amendment ratifies it: the
[Amendment 2026-11-28](v0.4.x-sequencing-2026-11-28.md#rulings) does, with
ruling (y) for the first and so on to ruling (ad) for the sixth.

1. **The society work goes on, in a changed order.** EXP-002's
   pre-registration merges before any [v0.4.0 row](v0.4.0-plan.md#progress)
   that has not started. The four rows stopped part-way, A2, A4, B1 and C1,
   may resume. Rule 1 is about a short job, so it is no reason to stop. It is
   also the only measurement so far, and it went against the governed
   discussion.
2. **The pause ends when that amendment merges**, for those four rows, the
   strategy document's rewrite, EXP-002's pre-registration and the licence
   PR. The README follows the rewrite (ruling v).
3. **Ruling (t)'s three items.**
   - No governance-core extraction is ordered. It reopens, and
     [RFC 0046](rfcs/0046-budget-lease-extraction.md) with it, only when an
     amendment's External evidence section records an outside ask. It is no
     longer passed from review to review.
   - No component is retired. The orchestrator, the channel engine, memory
     and the web console are what the society runs on, and the rewrite says
     how each serves the goal.
   - The Rust CLI and the workflow engine are not removed and stay frozen.
     The first review after EXP-002's pre-registration merges decides, since
     that document will say whether its society arm uses either.
4. **Four small rulings for the amendment to carry.**
   - ISSUE-0234 to ISSUE-0290 keep the severities their filings proposed.
   - Ruling (e)'s warning is 700 lines, as decided on 2026-11-21
     ([ISSUE-0288](issues/ISSUE-0288-plans-and-the-sequencing-record-still-say-500-lines.md)).
   - The decision of 2026-11-16, that replies under an old message from a
     loose id stay as they are, gets a Known Gaps line
     ([ISSUE-0275](issues/ISSUE-0275-old-messages-from-a-loose-id-still-reach-agents.md)).
   - `Python 3.12 (unit tests)` becomes a required check.
5. **The trust score stays, still unevidenced.**
   [Strategy §8](project-strategy.md#8-implementation-changes) left it to
   EXP-001, and no EXP-001 arm varies trust. EXP-002's pre-registration names
   a comparison that isolates it, or the review after that retires the schema
   and decay logic.
6. **The next review** is the first after EXP-002's pre-registration merges,
   or 2027-01-23, eight weeks on, whichever comes first.

## 4. What else it records

- Of the four reviews' nine findings, F6 no longer holds as written and F5
  half holds, as before. The other seven stand.
- [Strategy §6](project-strategy.md#6-plan-for-the-next-ten-weeks) item 3 is
  done. Items 5–7 have not started, and the rewrite says which of them serve
  the goal.
- The market claims in the
  [evidence companion](project-strategy-evidence.md#8-market-claims--perishable)
  were not re-checked, since no ruling relies on one.

## Related documentation

- [Project strategy — standing review](project-strategy.md) — the decision
  rules, and the log this review adds a row to.
- [EXP-001's result](experiments/EXP-001-result.md) and the
  [AI raters deviation](experiments/EXP-001-deviation-ai-raters.md) — the
  rule, and what a rule fired on AI-scored figures is worth.
- [v0.3.x sequencing](v0.3.x-sequencing.md#amendment-2026-10-18--a-society-that-pursues-long-goals-exp-001-measures-v040-opens)
  — rulings (h) and (l).
- [v0.4.x sequencing, 2026-10-21](v0.4.x-sequencing-2026-10-21.md#rulings) —
  rulings (t) and (u).
- [v0.4.x sequencing, 2026-11-28](v0.4.x-sequencing-2026-11-28.md#rulings) — the
  amendment that ratifies this review, with the pause's record.
- [v0.4.0 plan](v0.4.0-plan.md#resumed-in-a-changed-order) — the rows that
  resume, and the rows that wait.
