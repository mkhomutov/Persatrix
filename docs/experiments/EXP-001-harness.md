# EXP-001 — The harness

> **Status**: 🚧 **In Progress** — PR 1 of 6 merged ([#984](https://github.com/mkhomutov/Persatrix/pull/984)), PR 2 open ([#986](https://github.com/mkhomutov/Persatrix/pull/986)); no practice run yet.
> **Last updated**: 2026-09-23
> **Carries out**: the [EXP-001 pre-registration](EXP-001-preregistration.md) and its [part 2](EXP-001-preregistration-scoring.md)
> **Code**: [`evaluators/exp001/`](../../evaluators/exp001/)

The pre-registration fixes what EXP-001 runs and how its scores decide. The
harness is the code that does it: it holds the five arms' meetings, records
and prices every model call, collects the memos, and does the arithmetic that
picks a rule. It lands in the reviewed PRs below, as
[pre-registration §3](EXP-001-preregistration.md#3-running-it) asks.

That section also says every choice a harness PR makes, where the documents
leave it open, is listed in the PR and frozen when it merges. The
[frozen choices](#frozen-choices) table collects them in one place, so the
result can cite them.

## The PRs

| PR | What it adds | Pre-registration checks it proves |
|---|---|---|
| 1 ([#984](https://github.com/mkhomutov/Persatrix/pull/984)) | Reads and checks the materials; records each model call; prices it with the fixed table; draws each series' arm order | 4 (the record's fields) |
| 2 ([#986](https://github.com/mkhomutov/Persatrix/pull/986)) | Scoring and the decision: the 400-word cut, memo quality, recall majority, per-plan agreement, the interval, the verdicts and the rule that fires, the price range and the cheaper-model repricing; blinded rater packets | — (part 2) |
| 3 | Runtime: the Anthropic adapter's prompt-cache markers and cache token counts; a pinned clock; a hook that tags each call with its arm, meeting, adviser and purpose | 3, 4, 7 |
| 4 | Arm A: the advisers' identities rendered by the persona runtime's own prompt code, one call per meeting | 7, 8 |
| 5 | Arms B, C, D and D′: deployments, channels, the memo turn, restarts in D, D′'s transcript prefix, retries and failure detection | 1, 2, 3, 5, 6 |
| 6 | The judge and the practice run | all eight, on the practice series |

Each PR is test-first, like all unit-level code here. PR 6's practice run is
the evidence that the eight checks pass before any scored meeting.

## Frozen choices

| Choice | What the harness does | PR |
|---|---|---|
| Story date | Read from each message's first line, "Today is Monday 6 October 2036."; the weekday must match the date, every briefing falls on Monday 6 October 2036, and meetings must be exactly one week apart | 1 |
| Arm names | `A`, `B`, `C`, `D`, `D-prime`, in that order | 1 |
| Arm order | Python's `random.Random(2026)`; for each series in turn, series 1 first, one `sample` of all five arm names. The recorded orders are below; a test pins them | 1 |
| Call purposes | `reply` (an adviser's turn, or arm A's one call), `bid`, `memo`, `summary` (memory summaries and fact extraction), `judge` | 1 |
| Dollars | The table in pre-registration §3, per million tokens; a call on a model the table does not list, or with cache tokens on a model with no cache price, is refused rather than priced at zero | 1 |
| Dollars per plan | Counted calls of the series' briefing and plan meetings, in the attempt that finished, divided by four; judge calls never count | 1 |
| Real spend | Every arm call in the scored series, in every attempt, counted or not; practice and judge calls are left out | 1 |
| Judging spend | Every `judge` call, for the $25 judging cap | 1 |
| The 400-word cut | A word is any run of characters without a space or line break, so a heading's `##` counts. The memo is kept up to the end of its 400th word, formatting and all, and its word count and whether it was cut are recorded | 2 |
| Control plans | A rater's total is (C1 + C3 + C4 + C5) × 10 ÷ 8. A score for C2 on a control plan, or a missing one on a plan, is refused | 2 |
| A plan a rater cannot rank | When one rater gives all of a plan's memos the same total, the correlation is undefined. That plan is skipped for that pair, like a plan with fewer than three memos, and the count of skipped plans is reported. A pair left with no plan makes agreement undefined, and the result inconclusive | 2 |
| The pooled correlations | Over every memo that exists, leaving out missing ones as the per-plan figure does; reported only | 2 |
| Rounding at a threshold | Agreement, "beats" and "clearly beats" are compared after rounding to nine decimals, so a figure of exactly 0.4, zero or p is not decided by floating point | 2 |
| t | 2.776 for five series and 3.182 for four, as part 2 states them | 2 |
| Price range | Prices p from zero up; the verdict can change only where the interval's low end, or its mean less p, crosses zero, and those points are solved exactly | 2 |
| Cheaper-model repricing | Every `bid` and `summary` call moves to `claude-haiku-4-5` at $1.00 input and $5.00 output; its cache tokens keep the fixed table's ratio to input, $1.25 to write and $0.10 to read. That model is never in the run's own price table | 2 |
| Blinding | Every adviser's ID and name from `panel.yaml`, in any case and joined by a space, hyphen, underscore or line break, becomes `[adviser]`; so does any one word of a name written capitalised, such as `Stoat`. A packet ID is 8 hex characters from the operating system's random source, so no seed can rebuild it. Each rater's order is a fresh shuffle from that source, drawn again if another rater already has it. A missing memo scores 0 and never reaches the raters | 2 |

The arm order each series runs in:

| Series | Order |
|---|---|
| 1 | A, C, D, D′, B |
| 2 | D′, D, C, B, A |
| 3 | B, A, C, D, D′ |
| 4 | C, A, B, D, D′ |
| 5 | C, B, D, D′, A |

## Related documentation

- [EXP-001 pre-registration](EXP-001-preregistration.md) — materials, arms
  and the run.
- [Part 2: scoring and the decision](EXP-001-preregistration-scoring.md).
- [Evaluators guide](../evaluators-guide.md) — the golden-trace harness this
  one sits beside.
