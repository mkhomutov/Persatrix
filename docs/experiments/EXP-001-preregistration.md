# EXP-001 — Pre-registration

> **Status**: 📋 **Planned** — pre-registered; no scored run has started.
> **Last updated**: 2026-09-17
> **Applies**: the arms and decision rules of [strategy §4](../project-strategy.md#4-the-decision-framework), under ruling (a) of the [sequencing Amendment 2026-09-12](../v0.3.x-sequencing.md#amendment-2026-09-12--close-v0316-small-then-measure-before-any-train-opens)
> **Materials**: [`evaluators/experiments/EXP-001/`](../../evaluators/experiments/EXP-001/)

Persatrix claims that a governed discussion between persona agents, with
memory, gives better advice for the money than asking one model once. No
test has ever compared the two. EXP-001 does, on one job: critique a plan and
recommend a decision.

[Strategy §4](../project-strategy.md#4-the-decision-framework) already fixed
the job, the five setups (the **arms**) and the four
[decision rules](../methodology/process-glossary.md#decision-rule) that say
what the project does for each outcome. This document fixes everything else,
before any scored run: the test material, how each arm runs, how answers are
scored, and the arithmetic that decides which rule fires. The result is
published as `docs/experiments/EXP-001-result.md`, with transcripts, whichever
way it falls.

## 1. The materials

Every arm meets the same five fictional organisations. Each **series** is six
meetings with one organisation, in a fixed order:

1. **Briefing.** The operator states six facts, such as "the lease on our
   Quay Lane shop ends next June, and the landlord has told us it will not be
   renewed". Not scored.
2. **Four plan meetings.** The operator brings a plan with three lettered
   options and asks the panel to critique it and recommend one. Scored.
3. **Recall check.** The operator asks four questions about the briefing
   facts. Reported, but no rule reads it.

That gives 20 scored plans. In 15 of them, one or two briefing facts decide
which options are sound: a refit of the Quay Lane shop cannot pay for itself
before the lease ends. The other 5 are **control plans** that no earlier fact
bears on; they show whether memory gets in the way when there is nothing to
remember. The panel never sees a plan's **answer key**, which lists three
problems inside the plan itself, the earlier facts that bear on it, and any
option those facts make unsound.

The recall questions avoid the facts' own words: "How much debt are we allowed
to take on this year?" asks about a $150 000 borrowing cap. That is the rule
of the [dementia test](../manual-tests/MT-MEMORY-005-dementia-test.md): a
question that repeats a fact's words tests word matching, not memory.

A practice series (a community theatre, two plans) is never scored. It is for
testing the harness and training the scorers.

| File | What it holds |
|---|---|
| `series-1.yaml` to `series-5.yaml` | The scored series: messages, facts, answer keys |
| `practice.yaml` | The practice series |
| `panel.yaml` | The advisers, each arm's settings, every instruction the harness adds |
| `rubric.yaml` | The scoring rubric and the LLM judge's prompts |

## 2. The arms

Every arm uses the same panel of four advisers
([`panel.yaml`](../../evaluators/experiments/EXP-001/panel.yaml)): a chair, a
finance adviser, an operations adviser and a customer adviser. Every arm gets
the operator's messages word for word. Every scored answer is a **decision
memo** in one fixed format of at most 400 words: the recommendation, the
problems with the plan, the options weighed, and what would change the
recommendation.

| Arm | How a meeting runs | What carries to the next meeting |
|---|---|---|
| **A** | One model call plays all four advisers and writes the memo. | Nothing |
| **B** | The advisers are persona agents in a channel with governance off: no salience bids and no end vote. | Nothing |
| **C** | As B, with governance as shipped: salience bids, chair, end vote, round limit. | Nothing |
| **D** | As C. | Memory as shipped, through the 1 500-token allocator |
| **D′** | As C. | No allocator; the full transcripts of earlier meetings, in a cached prompt prefix |

In B, C, D and D′ every plan meeting ends with a **memo turn**: once the
discussion closes, however it closes, the chair writes the memo and sees that
meeting's whole discussion. At the recall check the chair gives the answers
instead.

What those words mean in the code:

- **One model for every call** in every arm: `claude-sonnet-4-6`, salience
  bids and memory summaries included, so the `quality`, `fast` and
  `summarizer` aliases all point at it. Every live arc so far has run on it,
  and the Anthropic adapter as shipped sends a temperature, which newer
  models reject.
- **A new channel for every meeting**, whose members are the four advisers and
  the operator. A persona's window of recent messages reads its whole channel,
  so a reused channel would hand the previous meeting's raw text to every arm.
- **Governance as shipped** (C, D, D′) is the shipped `roundtable` channel in
  [`config/channels.yaml`](../../config/channels.yaml), sized for four
  advisers the way the multivendor blueprint sizes it: an autonomous channel,
  round limit 8, a budget of 200 000 tokens per discussion, and an end vote
  once all four advisers vote within 8 messages. The depth cap of 5 stays.
- **Governance off** (B): every adviser replies each round without a bid, and
  the end vote can never reach its threshold. The code will not switch off
  the rest, so B keeps two things: the chair gets one forced turn when a round
  stalls, and the round limit asks the chair for a closing synthesis.
- **Memory off** (B, C, D′): the memory budget is 0 and every meeting starts
  with empty memory stores. The harness switches memory writing off if the
  runtime allows it. If not, those calls are recorded but not counted in the
  arm's dollars, because nothing reads what they write.
- **Memory as shipped** (D): the stores start empty for each series and keep
  what the series adds for all six meetings. The audience gate and
  cross-channel recall run as shipped. Every meeting of a series has the same
  members, so the gate admits the series' own facts.
- **Raw transcript** (D′): wherever D's prompts receive recalled memory, D′'s
  receive the full transcripts of the series' earlier meetings, oldest first,
  placed before anything that changes between calls and marked for the
  provider's cache.
- **No tools, no leftover state.** The advisers have no tools, and before
  every meeting each adviser's saved state is reset, except its memory stores.

## 3. Running it

**Before the scored run.** The harness lands under `evaluators/` in its own
reviewed PRs. Every choice it makes that this document leaves open is listed
in its PR and frozen when that PR merges. Before any scored meeting, its tests
or a practice run must show that:

1. in B, C and D′, nothing from an earlier meeting reaches any prompt, apart
   from D′'s transcript prefix;
2. in D, a briefing fact can reach a later meeting's prompt through the
   shipped memory path;
3. in D′, the second model call of a meeting reads the prefix from the cache;
4. every model call is recorded with its arm, meeting, adviser, purpose, time
   and token counts, cache reads and writes included;
5. the memo turn sees its meeting's whole discussion.

**Order.** The practice series runs first, as often as needed. The scored
series then run in order, 1 to 5. Within a series the five arms run one at a
time, in an order drawn with seed 2026 and recorded. Every scored meeting
happens within seven days of the first.

**Attempts.** Each scored meeting runs once. A provider error (a rate limit,
a server error, a timeout) is retried up to three times; if the meeting still
fails, that arm's series starts again from its briefing, once. A failure the
system itself causes, such as a discussion that never closes, an adviser that
goes silent or a missing memo, is not retried: the memo is scored as written,
or 0 on every criterion if there is none, and its cost counts. If the harness
itself proves wrong during the scored run, the run stops, the fix goes through
a reviewed PR, and every scored output so far is discarded and later published
with the result. That can happen at most twice.

**Dollars.** The harness prices every model call an arm makes with this table,
per million tokens, even if list prices change:

| Model | Input | Output | Cache write | Cache read |
|---|---|---|---|---|
| `claude-sonnet-4-6`, the arms | $3.00 | $15.00 | $3.75 | $0.30 |
| `claude-opus-5`, the judge | $5.00 | $25.00 | — | — |

The orchestrator's cost ledger is reported beside these figures but cannot
stand alone: it has no price for cached tokens, and it does not meter every
memory summary.

An arm's **dollars per plan** are everything it spent on a series (the
briefing and the four plan meetings, not the recall check) divided by four.
**Minutes per plan** run from the operator's message to the finished memo.

**Spend cap.** The scored run stops at $150 of real spend, counted in the
arms' dollars or not. Scoring stops at $25. A run stopped by a cap is published
as incomplete, and no rule fires.

## 4. Scoring

Three raters score every memo: two people and one
[LLM judge](../methodology/process-glossary.md#llm-judge) (`claude-opus-5`,
default settings, one pass). Each works alone and blind. The harness removes
adviser names, gives each memo a random ID, and keeps the map from ID to arm
sealed until every score is in. A rater's packet holds each plan's message,
its answer key and the memo, shuffled in a different order for each rater. At
least one of the two people must not have run the harness. Before scoring,
the raters score the practice memos together to agree how the anchors apply;
those scores are thrown away.

The [rubric](../../evaluators/experiments/EXP-001/rubric.yaml) has five
criteria, each scored 0, 1 or 2:

1. finds the problems inside the plan that the key lists;
2. uses what the organisation said at earlier meetings (not scored on control
   plans, whose other four criteria are scaled up to 10);
3. recommends exactly one option, with its reason and what would change it;
4. weighs the options against the organisation's situation;
5. stays accurate and invents nothing.

A memo's **quality** is the average of the three raters' totals, out of 10.
It is what strategy §4 calls synthesis quality.

**Agreement.** For each pair of raters, the harness ranks the 100 memos by each
rater's score and measures how closely the two rankings agree (a Spearman
correlation). If the average of the three pairs is below 0.4, the raters
disagree too much to carry a decision: the result is published as
inconclusive and no rule fires. The same holds if the two people cannot
finish scoring within 21 days of the run.

**Recall answers** are right or wrong against the key's required items. An
answer is right when at least two raters mark it right.

## 5. How a rule fires

"Quality per dollar" cannot simply mean score divided by dollars. One call
costs a small fraction of a discussion, so that ratio would pick arm A before
anyone read a memo. The experiment puts a price on quality instead:

- **One quality point is worth $1.00** on one decision.
- A memo's **value** is $1.00 × its quality, minus its arm's dollars per plan.
- Comparing arms X and Y, the **difference** on each of the 20 plans is X's
  value minus Y's.
- The **interval** is a 95% bootstrap interval for the average difference:
  draw four plans with replacement within each series, 10 000 times, with
  seed 2026, and take the 2.5th and 97.5th percentiles of the averages.

The verdicts:

- X **beats** Y when the whole interval is above zero.
- X **clearly beats** Y when X beats Y and the average difference is at least
  $1.00, one quality point.
- X **matches** Y when the interval includes zero and lies between −$1.00 and
  +$1.00.

The checks run in order, and the first that applies fires its rule. The rules'
full text is in [§4](../project-strategy.md#4-the-decision-framework).

1. **C does not clearly beat A**: rule 1 fires. Stop adding society features
   for this job; reposition on the gate alone.
2. **D does not beat C**: rule 2 fires. The allocator is not the asset.
3. **D′ matches D, or beats it**: rule 3 fires. Long context replaces the
   allocator.
4. **D beats D′**: rule 4 fires. The full stack is justified.
5. Otherwise no rule fires: the allocator and long context could not be told
   apart.

**Reported, but never deciding:** B against A and C against B, since B is in no
rule; quality per minute; each criterion on its own; the recall check for each
arm; the control plans on their own, to show whether memory gets in the way;
the rule that would fire on the two people's scores alone; and, for each
verdict, the price of a quality point at which it would flip.

## 6. What it cannot show

- One job, one model, one language and invented organisations: the result says
  nothing about other jobs or models.
- Every meeting has one audience, so the audience gate is never tested. Rule
  1's fallback, "the gate alone", is assumed to be worth something, not
  measured.
- A memo that uses earlier facts hints that it came from D or D′. The key
  rewards those facts either way.
- The judge comes from the same provider as the arms.
- Plans name what their facts are about ("Quay Lane"), as real follow-up plans
  do. Only the recall check avoids shared words.
- The price of $1.00 a point is a judgement; the flip prices let a reader apply
  another.

## 7. Changes

Until the first scored meeting starts, this document and the materials change
only through a PR that says what changed and why; the result lists every such
PR. From the first scored meeting on, nothing changes: a problem found then is
reported in the result, not fixed. The decision rules themselves change only
the way [strategy §10](../project-strategy.md#10-review-cadence-and-log)
describes: through a review and the amendment that ratifies it.

## 8. From here to the result

1. This document and the materials merge. The harness PRs follow under
   `evaluators/`.
2. Practice runs, until the five checks in §3 pass.
3. The scored run. Transcripts are committed under
   `evaluators/experiments/EXP-001/runs/`.
4. Scoring, within 21 days of the run.
5. `docs/experiments/EXP-001-result.md`: every figure in §5, the rule that
   fired, every PR that changed this document, and any discarded outputs.
6. The first strategy review, by 2026-11-30 at the latest, logs the rule. Its
   amendment decides what opens next.

## Related documentation

- [Project strategy §4](../project-strategy.md#4-the-decision-framework) — the
  arms and decision rules this document applies.
- [Sequencing Amendment 2026-09-12](../v0.3.x-sequencing.md#amendment-2026-09-12--close-v0316-small-then-measure-before-any-train-opens)
  — why no release plan opens before EXP-001 reports.
- [MT-MEMORY-005, the dementia test](../manual-tests/MT-MEMORY-005-dementia-test.md)
  — the rule the recall questions follow.
- [Autonomous channels guide](../guides/autonomous-channels.md) — the
  governance settings arms B to D′ use.
- [Evaluators guide](../evaluators-guide.md) — the golden-trace harness that
  EXP-001's harness sits beside.
