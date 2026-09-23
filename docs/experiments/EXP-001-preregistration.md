# EXP-001 — Pre-registration

> **Status**: 📋 **Planned** — pre-registered; no scored run has started.
> **Last updated**: 2026-09-17
> **Applies**: the arms and decision rules of [strategy §4](../project-strategy.md#4-the-decision-framework), under ruling (a) of the [sequencing Amendment 2026-09-12](../v0.3.x-sequencing.md#amendment-2026-09-12--close-v0316-small-then-measure-before-any-train-opens)
> **Materials**: [`evaluators/experiments/EXP-001/`](../../evaluators/experiments/EXP-001/) · **Scoring and the decision**: [part 2](EXP-001-preregistration-scoring.md)

Persatrix claims that a governed discussion between persona agents, with
memory, gives better advice for the money than asking one model once. No test
has ever compared the two. EXP-001 does, on one job: critique a plan and
recommend a decision.

[Strategy §4](../project-strategy.md#4-the-decision-framework) already fixed
the job, the five setups (the **arms**) and the four
[decision rules](../methodology/process-glossary.md#decision-rule). This
document fixes the materials, what each arm means in the code, and how the run
happens. [Part 2](EXP-001-preregistration-scoring.md) fixes how answers are
scored and which rule fires. Both are fixed before any scored run. The result
is published as `docs/experiments/EXP-001-result.md`, with transcripts,
whichever way it falls.

## 1. The materials

Every arm meets the same five fictional organisations. Each **series** is six
meetings with one organisation, one week apart in a story calendar that starts
on Monday 6 October 2036:

1. **Briefing.** The operator states six facts, such as "the lease on our
   Quay Lane shop ends next June, and the landlord has told us it will not be
   renewed". Not scored.
2. **Four plan meetings.** The operator brings a plan with three lettered
   options and asks the panel to critique it and recommend one. Scored.
3. **Recall check.** The operator asks four questions about the briefing
   facts. Reported; no rule reads it.

That gives 20 scored plans. In 15 of them, one or two briefing facts make at
least one option unsound. In ten of those 15, the unsound option is one a
careful reader of the plan alone could well choose. Examples: a refit whose
corrected payback looks fine, but whose lease ends in eight months; a
cautious trial that moves the one customer whose contract forbids it. The
unsound letter varies from plan to plan. The other 5 are **control plans**
that no earlier fact bears on; they show whether memory gets in the way when
there is nothing to remember.

The panel never sees a plan's **answer key**. It lists three problems inside
the plan, the earlier facts that bear on the plan, and any option those facts
make unsound. For each fact it gives the details a memo must state to show it
used that fact, so a memo can't earn credit by guessing a generic concern.

The recall questions avoid the facts' own words: "How much extra debt are we
allowed to take on right now?" has no word in common with the fact it tests.
That is the rule of the [dementia test](../manual-tests/MT-MEMORY-005-dementia-test.md):
a question that repeats a fact's words tests word matching, not memory.

A practice series (a community theatre, two plans) is never scored. It is for
testing the harness and training the scorers.

| File | What it holds |
|---|---|
| `series-1.yaml` to `series-5.yaml` | The scored series: messages, facts, answer keys |
| `practice.yaml` | The practice series |
| `panel.yaml` | The advisers, each arm's deployment and settings, every instruction the harness adds |
| `rubric.yaml` | The scoring rubric, the packets and the LLM judge's prompts |

## 2. The arms

Every arm uses the same panel of four advisers
([`panel.yaml`](../../evaluators/experiments/EXP-001/panel.yaml)): a chair, a
finance adviser, an operations adviser and a customer adviser. Every arm gets
the organisation's one-line description and the operator's messages word for
word. Every scored answer is a **decision memo** in one format of at most 400
words: the recommendation, the problems with the plan, the options weighed,
and what would change the recommendation.

| Arm | How a meeting runs | What carries to the next meeting |
|---|---|---|
| **A** | One model call plays all four advisers and writes the memo. | Nothing |
| **B** | The advisers are persona agents in a channel with governance off: no salience bids, and no end vote that can close the discussion. | Nothing |
| **C** | As B, with governance as shipped: salience bids, chair, end vote, round limit. | Nothing |
| **D** | As C. | Memory as shipped, through the 1 500-token allocator |
| **D′** | As C. | No allocator; the full transcripts of earlier meetings, in a cached prompt prefix |

In B, C, D and D′, every plan meeting and recall check ends with a **memo
turn**. Once the discussion closes, however it closes, the harness turns the
channel's autonomous mode off and sets the other advisers to observers. The
operator then asks the chair alone for the memo, or for the answers. That
starts no new discussion, and the chair sees the whole meeting.

What those words mean in the code:

- **One model and one runtime.** Every call in every arm uses
  `claude-sonnet-4-6`, salience bids and memory summaries included: the
  `quality`, `fast` and `summarizer` aliases all point at it. Every live arc
  so far has run it as the quality lane, though their bids and summaries ran
  on the cheaper shipped lane, and the shipped Anthropic adapter sends a
  temperature, which newer models reject. Every arm runs the one build that
  teaches the adapter prompt caching; only D′ uses it.
- **A new channel for every meeting.** Its members are the four advisers and
  the operator. The operator posts each message as an ordinary channel
  message; convene is never used. The topic and goal are fixed and the agenda
  is empty, so no adviser gets agenda-driven turns.
- **Governance as shipped** (C, D, D′) is the shipped `roundtable` channel in
  [`config/channels.yaml`](../../config/channels.yaml), with its end vote sized
  for four advisers the way the multivendor blueprint does it: all four must
  vote within 8 messages. Round limit 8, depth cap 5. The per-discussion token
  budget is raised to 2 000 000, so the cost bound never closes a discussion
  and D′'s prefix cannot cut its discussions short.
- **Governance off** (B): the advisers reply without a bid, and the end vote
  can never reach its threshold. The code will not switch off the rest. The
  advisers keep the shipped instructions that allow silence and voting. A
  discussion ends at the depth cap or the round limit, each with a chair
  synthesis, and the chair still gets a forced turn when a round stalls.
- **Memory off** (B, C, D′): the memory budget is 0, and every meeting starts
  on a new deployment with empty stores. Memory writing is switched off if the
  runtime allows; otherwise those calls are recorded but not counted in the
  arm's dollars, since memory is not part of these arms' design.
- **Memory as shipped** (D): every series starts on a new deployment with empty
  stores, and it keeps that deployment for its six meetings. The audience gate
  and cross-channel recall run as shipped; every meeting has the same members,
  so the gate admits the series' own facts. Between meetings the harness resets
  each adviser's energy and restarts the agents, and a restart must leave memory
  exactly as it was.
- **Raw transcript** (D′): wherever D's prompts receive recalled memory, D′'s
  receive the full transcripts of the series' earlier meetings, oldest first,
  placed before anything that changes between calls and marked for the cache.
- **The advisers themselves.** They have no configured tools: the built-in
  note tools remain, and verbatim channel recall is denied. Each adviser's
  message window holds 32 000 tokens, so the operator's plan never scrolls out
  of view. Its clock reads 10:00 on the meeting's story date.
- **Arm A** reads the advisers' identities in the same words the persona agents
  read, rendered by the runtime's own prompt code, with the same clock line.

## 3. Running it

**Before the scored run.** The harness lands under `evaluators/` in its own
reviewed PRs. Every choice they make that this document leaves open is listed
in the PR and frozen when it merges. Before any scored meeting, tests or a
practice run must show that:

1. in B, C and D′, nothing from an earlier meeting reaches any prompt, apart
   from D′'s transcript prefix;
2. in D, a briefing fact can reach a later meeting's prompt through the shipped
   memory path, and a restart leaves the memory stores exactly as they were;
3. in D′, the first call of a meeting that carries the prefix writes it to the
   cache and every later call of that meeting reads it, and no other arm sets
   a cache breakpoint;
4. every model call is recorded with its arm, meeting, adviser, purpose, time
   and token counts, cache reads and writes included;
5. the memo turn's prompt contains every message of its meeting, the plan
   included, and the memo turn starts no new discussion;
6. no call is refused by the shipped spending limits (global, per workflow,
   per agent), which the deployment turns off;
7. every adviser's clock, and arm A's clock line, shows the meeting's date;
8. arm A's prompt contains each adviser's identity exactly as the persona
   agents' prompts do.

**Order.** The practice series runs first, as often as needed. The scored
series then run in order, 1 to 5. Within a series the five arms run one at a
time, in an order drawn for that series from one random stream seeded 2026, so
the series do not share an order, and recorded. Every scored meeting happens
within seven days of the first.

**Attempts and failures.**

- **Provider errors.** An error from the model provider (a rate limit, a
  server error, a timeout) is retried up to three times. The harness reads
  them from the runtime's own records: a persona turn that ends in a provider
  error publishes nothing to the channel, so it looks like silence. If the
  meeting still fails, that arm's series starts again from its briefing, once.
  If it fails again, that series is dropped from every arm's comparisons. With
  fewer than four series left, the run is incomplete. A failed attempt's spend
  is reported, but not counted in dollars per plan.
- **Failures the system causes.** Examples are a discussion that never
  closes, an adviser that goes silent, or a missing memo. These are not
  retried. The memo is scored as written, or 0 on every criterion if there is
  none, and its cost counts. The harness detects these failures itself.
- **Harness faults.** If the harness itself proves wrong, the run stops, the
  fix goes through a reviewed PR, and every scored output so far is discarded.
  The discarded outputs are published later with the result, and the
  seven-day window starts again. A third harness fault ends the run as
  incomplete.

**Dollars.** The harness prices every model call an arm makes with this table,
per million tokens, even if list prices change:

| Model | Input | Output | Cache write | Cache read |
|---|---|---|---|---|
| `claude-sonnet-4-6`, the arms | $3.00 | $15.00 | $3.75 | $0.30 |
| `claude-opus-5`, the judge | $5.00 | $25.00 | — | — |

The orchestrator's cost ledger is reported beside these figures but cannot
stand alone: it has no price for cached tokens, and it does not meter every
memory summary. An arm's **dollars per plan** are everything it spent on a
series (the briefing and the four plan meetings, not the recall check) divided
by four. **Minutes per plan** run from the operator's message to the finished
memo.

**Spend caps.** The scored run stops at $150 of real spend: every scored
attempt counts, discarded or not, and whether or not a call counts in an arm's
dollars. Judging stops at $25. A run stopped by its cap is incomplete, and no
rule fires ([part 2, §6](EXP-001-preregistration-scoring.md#6-when-no-rule-fires)).

## 4. What it cannot show

- One job, one model, one language and invented organisations: the result says
  nothing about other jobs or models.
- Each arm runs each series once, so run-to-run variation is not in the
  intervals.
- Every meeting has one audience, so the audience gate is never tested. Rule
  1's fallback, "the gate alone", is assumed to be worth something, not
  measured.
- Salience bids and memory summaries run on the arms' model rather than the
  shipped cheaper one, which overstates C, D and D′'s dollars. Part 2 reports
  every verdict with those calls repriced.
- An adviser's energy still drains within a meeting, and a busy adviser's
  prompt can tell it to conserve effort; B's advisers speak most.
- A memo that uses earlier facts hints that it came from D or D′. The key
  rewards those facts either way.
- The judge comes from the same provider as the arms.
- Plans name what their facts are about ("Quay Lane"), as real follow-up plans
  do. Only the recall check avoids shared words.
- The price of $1.00 a quality point is a judgement. Part 2 reports the range
  of prices over which each verdict holds.

## 5. Changes

From the moment these documents merge, some things change only the way the
decision rules do, through a strategy review and the amendment that ratifies
it (see [strategy §10](../project-strategy.md#10-review-cadence-and-log)):

- the arms and their settings;
- the model and the price table;
- the rubric;
- everything in part 2.

Until the first scored meeting starts, the materials, the harness mechanics
and the spend caps can change through a PR that says what changed and why. The
result lists every such PR. From the first scored meeting on, nothing changes:
a problem found then is reported in the result, not fixed.

## 6. From here to the result

1. These documents and the materials merge. The harness PRs follow under
   `evaluators/`.
2. Practice runs, until the eight checks in §3 pass.
3. The scored run. Memos and transcripts stay outside the repository.
4. Scoring, within 21 days of the last scored meeting.
5. `docs/experiments/EXP-001-result.md` is written, and transcripts are
   committed under `evaluators/experiments/EXP-001/runs/`. The result
   includes every figure part 2 names, the rule that fired, every PR that
   changed these documents, and any discarded outputs.
6. The first strategy review, by 2026-11-30 at the latest, logs the rule. Its
   amendment decides what opens next.

## Related documentation

- [EXP-001 pre-registration, part 2](EXP-001-preregistration-scoring.md) —
  scoring, agreement and how a rule fires.
- [EXP-001 harness](EXP-001-harness.md) — the harness PRs and the choices
  each one freezes.
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
