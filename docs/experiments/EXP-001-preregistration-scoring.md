# EXP-001 — Pre-registration: scoring and the decision

> **Status**: 📋 **Planned** — pre-registered; no scored run has started.
> **Last updated**: 2026-09-17
> **Part of**: the [EXP-001 pre-registration](EXP-001-preregistration.md), which covers the materials, the arms and how the run happens. This part covers how answers are scored and how the scores decide which of [strategy §4](../project-strategy.md#4-the-decision-framework)'s decision rules fires.

## 1. What is scored

Each arm answers 20 plans, so there are 100 **decision memos** and 100 recall
answers (four per recall check). Before scoring, the harness cuts every memo
after its 400th word and records where it cut; the memo format asks for fewer
than 400.

## 2. Raters and blinding

Three raters score every memo and every recall answer: two people and one
[LLM judge](../methodology/process-glossary.md#llm-judge) (`claude-opus-5`,
default settings, one pass). Each works alone.

A rater's packet for a memo holds the organisation's one-line description,
every message the operator sent in that series up to and including the plan
the memo answers, the plan's answer key, and the memo. With the earlier
messages in hand, a rater can check a memo that cites an earlier fact, and can
catch one that invents a fact.

Blinding:

- The harness removes adviser names, gives every memo a random ID, and shuffles
  each rater's packets in a different order.
- The map from ID to arm stays sealed until every score is in.
- Memos and transcripts stay outside the repository until every score is in.
  The harness itself detects a missing memo, or a discussion that never closes,
  so nobody reads the text to find failures.
- Neither person reads a memo or transcript from the scored run before
  scoring, and at least one of them did not run the harness. The result says
  who read what, and when.

Before scoring, the raters score the practice memos together and agree how
the anchors apply. Those scores are thrown away.

## 3. The rubric

The [rubric](../../evaluators/experiments/EXP-001/rubric.yaml) scores each
memo on five criteria, each 0, 1 or 2, checking the anchors from 0 upwards:

1. finds the problems inside the plan that the key lists;
2. uses what the organisation said at earlier meetings: a fact counts only when
   the memo states the details the key lists for it and applies them, and
   recommending an option the key marks unsound scores 0;
3. recommends exactly one option, with its reason and what would change it;
4. compares every option against the organisation's situation;
5. stays accurate and invents nothing.

Criterion 2 is not scored on control plans, whose other four criteria are
scaled up to 10. A memo's **quality** is the average of the three raters'
totals, out of 10. It is what strategy §4 calls synthesis quality.

A recall answer is right when it gives every detail the key requires. It
counts as right when at least two raters mark it right.

## 4. Agreement

Every rule reads the difference between two arms on the same plan, so that is
where the raters have to agree. Ranking all 100 memos at once would hide a
disagreement there: two raters who only agree on which plans are hard, or on
which memos used the earlier facts, would still look like they agree.

For each pair of raters and each plan, the harness ranks that plan's five memos
by each rater's total and measures how closely the two rankings agree (a
Spearman correlation, with tied totals sharing their average rank). The pair's
figure is the average over the 20 plans. Memos the harness scored 0 because
none existed are left out, and a plan with fewer than three memos left is
skipped. The decision uses the average of three raters, and how reliable that
average is depends on how well they agree. So if the three pairs' figures
average below 0.4, the scores cannot carry a decision, and the result is
**inconclusive**. All three figures are reported, and so are the three
correlations over all 100 memos at once.

The result is also inconclusive if scoring is not finished within 21 days of
the last scored meeting, or if the judge's spend reaches its cap first.

## 5. How a rule fires

"Quality per dollar" cannot simply mean score divided by dollars. One call
costs a small fraction of a discussion, so that ratio would pick arm A before
anyone read a memo. The experiment puts a price on quality instead.

- **The price of a quality point, p, is $1.00** on one decision.
- A memo's **value** is p × its quality, minus its arm's dollars per plan.
- For arms X and Y, each series gets a **series difference**: the average over
  its four plans of X's value minus Y's.
- The **interval** is the average of the series differences, plus or minus
  t × their standard deviation ÷ √n. Here n is the number of series compared,
  normally 5, the standard deviation divides by n − 1, and t is the 97.5th
  percentile of Student's t with n − 1 degrees of freedom: 2.776 for five
  series, 3.182 for four.

Series, not plans, are the unit. All four plans in a series share one run of
each arm, one memory path and one dollar figure, so treating them as
independent would make the interval too narrow.

The verdicts:

- X **beats** Y when the whole interval is above zero.
- X **clearly beats** Y when X beats Y and the average series difference is at
  least p.

The checks run in order, and the first that applies fires its rule. The rules'
full text is in [§4](../project-strategy.md#4-the-decision-framework).

1. **C does not clearly beat A**: rule 1 fires. Stop adding society features
   for this job; reposition on the gate alone.
2. **D does not beat C**: rule 2 fires. The allocator is not the asset.
3. **D does not beat D′**: rule 3 fires. Long context replaces the allocator.
4. **Otherwise**, D beats D′: rule 4 fires. The full stack is justified.

**How this reads §4.** §4's rules are fixed; this section only reads them.
These readings are fixed when this document merges, before any run. The first
strategy review records them beside the rule that fired.

- **Quality per dollar.** §4's "quality per dollar" is value at price p.
- **Rules 1 and 2 overlap.** When C beats A, but not clearly, both rule 1 and
  rule 2's premise ("C beats A") hold. Rule 1 comes first, as it does in §4's
  list.
- **Rule 3.** It says "D′ matches D". Here that means D does not beat D′. The
  allocator carries the burden of proof, just as checks 1 and 2 put it on the
  discussion and on memory. So every complete result fires exactly one rule.

## 6. When no rule fires

An **incomplete** run fires no rule. So does an **inconclusive** score, and
neither can be read as firing one. A run is incomplete when a spend cap stops
it, when fewer than four series survive, or when the harness fails a third
time (see [part 1, §3](EXP-001-preregistration.md#3-running-it)).

The first strategy review may then do only one thing: order one repeat of the
whole run under these same documents, with a higher spend cap if a cap
stopped it. A second incomplete or inconclusive result is published as it is.

## 7. Reported, but never deciding

- B against A and C against B, since B is in no rule.
- For each arm:
  - quality;
  - dollars per plan;
  - quality divided by dollars, §4's literal ratio;
  - minutes per plan;
  - quality per minute.
- Each criterion on its own, the recall check for each arm, and the control
  plans on their own, which show whether memory gets in the way.
- The rule that would fire on the two people's scores alone.
- Every series difference, for every comparison.
- For each deciding verdict:
  - the range of prices p over which it holds;
  - the verdict with salience bids and memory summaries repriced at the list
    price of the shipped fast model, `claude-haiku-4-5` ($1.00 input, $5.00
    output per million tokens).
- For each meeting:
  - what closed the discussion (vote, depth cap or round limit);
  - each adviser's energy level at the memo turn;
  - whether the memo was cut at 400 words.

## Related documentation

- [EXP-001 pre-registration](EXP-001-preregistration.md) — materials, arms and
  the run.
- [Project strategy §4](../project-strategy.md#4-the-decision-framework) — the
  decision rules read here.
- [Scoring rubric](../../evaluators/experiments/EXP-001/rubric.yaml) — anchors,
  packets and the judge's prompts.
