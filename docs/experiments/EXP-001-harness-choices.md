# EXP-001 — The harness's frozen choices

> **Status**: 🚧 **In Progress** — the rows of PRs 1 to 4 froze when each merged; those of PRs 5a to 5c are in [holding the meetings](EXP-001-harness-choices-meetings.md), PRs 5d's and 5e's in [arm D′'s transcript prefix](EXP-001-harness-choices-d-prime.md), PR 6a's in [the judge](EXP-001-harness-choices-judge.md), PR 6b's in [the practice run](EXP-001-harness-choices-practice.md), and PR 6c's in [the scored run](EXP-001-harness-choices-scored.md).
> **Last updated**: 2026-10-07
> **Part of**: the [EXP-001 harness](EXP-001-harness.md), which lists the harness PRs and what each leaves the next

[Pre-registration §3](EXP-001-preregistration.md#3-running-it) says every
choice a harness PR makes, where the documents leave it open, is listed in the
PR and frozen when it merges. This table and its
[second part](EXP-001-harness-choices-meetings.md) collect them, so the
result can cite them. Until the first scored meeting, a row changes only
through a later harness PR that says what changed and why
([pre-registration §5](EXP-001-preregistration.md#5-changes)); from then on,
nothing changes.

## Frozen choices

| Choice | What the harness does | PR |
|---|---|---|
| Story date | Read from each message's first line, "Today is Monday 6 October 2036."; the weekday must match the date, every briefing falls on Monday 6 October 2036, and meetings must be exactly one week apart | 1 |
| Arm names | `A`, `B`, `C`, `D`, `D-prime`, in that order | 1 |
| Arm order | Python's `random.Random(2026)`; for each series in turn, series 1 first, one `sample` of all five arm names. The recorded orders are below; a test pins them | 1 |
| Call purposes | `reply` (an adviser's turn, or arm A's one call), `bid`, `memo`, `summary` (memory summaries and fact extraction), `judge` | 1 |
| Dollars | The table in pre-registration §3, per million tokens; a call on a model the table does not list, or with cache tokens on a model with no cache price, is refused rather than priced at zero | 1 |
| Dollars per plan | Counted calls of the series' briefing and plan meetings, in the attempt that finished, divided by four; judge calls never count. PR 5b holds a meeting a provider error cut short again, as its next try, so since then only each meeting's finished try counts ([holding the meetings](EXP-001-harness-choices-meetings.md)) | 1, 5b |
| Real spend | Every arm call in the scored series, in every attempt, counted or not; practice and judge calls are left out | 1 |
| Judging spend | Every `judge` call, for the $25 judging cap. PR 6a narrows it to the calls in one batch's own log, so the practice judging never counts toward the scored judging's cap ([the judge](EXP-001-harness-choices-judge.md)) | 1, 6a |
| The 400-word cut | A word is any run of characters without a space or line break, so a heading's `##` counts. The memo is kept up to the end of its 400th word, formatting and all, and its word count and whether it was cut are recorded | 2 |
| Control plans | A rater's total is (C1 + C3 + C4 + C5) × 10 ÷ 8. A score for C2 on a control plan, or a missing one on a plan, is refused | 2 |
| A plan a rater cannot rank | When one rater gives all of a plan's memos the same total, the correlation is undefined. That plan is skipped for that pair, like a plan with fewer than three memos, and the count of skipped plans is reported. A pair left with no plan makes agreement undefined, and the result inconclusive | 2 |
| The pooled correlations | Over every memo that exists, leaving out missing ones as the per-plan figure does; reported only | 2 |
| Rounding at a threshold | Agreement, "beats" and "clearly beats" are compared after rounding to nine decimals, so a figure of exactly 0.4, zero or p is not decided by floating point | 2 |
| t | 2.776 for five series and 3.182 for four, as part 2 states them | 2 |
| Price range | Prices p from zero up; the verdict can change only where the interval's low end, or its mean less p, crosses zero, and those points are solved exactly | 2 |
| Cheaper-model repricing | Every `bid` and `summary` call moves to `claude-haiku-4-5` at $1.00 input and $5.00 output; its cache tokens keep the fixed table's ratio to input, $1.25 to write and $0.10 to read. That model is never in the run's own price table | 2 |
| Blinding | Every adviser's ID and name from `panel.yaml`, in any case and joined by a space, hyphen, underscore or line break, becomes `[adviser]`; so does any one word of a name written capitalised, such as `Stoat`. A packet ID is 8 hex characters from the operating system's random source, so no seed can rebuild it. Each rater's order is a fresh shuffle from that source, drawn again if another rater already has it. A missing memo scores 0 and never reaches the raters | 2 |
| Story clock | An adviser's [agent time](../ai-glossary.md#agent-time) starts at 10:00 UTC on the meeting's story date and then runs with the real clock; it is not frozen. The advisers set no timezone, so they render UTC and their clock line reads 10:00. The harness also sets `PERSATRIX_CLOCK_ANCHOR` to the real moment the meeting began, so a restart inside a meeting resumes the clock rather than winding it back; at boot, messages sent before that moment are not replayed, because they belong to an earlier meeting's clock. Memory stamps, recall ages and incoming messages all read it, so in arm D a fact from the last meeting is a week old. Timers, deadlines and the orchestrator's records stay on real time | 3a; the zone in 3b |
| Cache marker | A caller hands the stable text to the model client as a cache prefix. The Anthropic adapter sends it as the first system block, before the system prompt, marked `ephemeral` with no TTL: the five-minute cache, whose write price of 1.25 times input is the one in pre-registration §3's table. A call without a prefix carries no marker anywhere. A provider that cannot cache reads the prefix joined onto the front of its system prompt | 3b |
| Call log | Every adviser appends to the file named by `PERSATRIX_CALL_LOG`, one line per call, failed calls included, with its tags in `PERSATRIX_CALL_TAGS`: arm, series, meeting, meeting kind, attempt and, since PR 5b holds a meeting again as its next try, try. Calls the harness makes itself, such as arm A's, carry the same tags through a scope around each meeting's calls; how the judge's calls are logged is PR 6a's choice, in [the judge](EXP-001-harness-choices-judge.md). A line's time is when the call began, in real time, never agent time. A line the reader cannot place (no purpose, a missing tag, an unknown arm) is refused, never guessed at | 3b, 5b |
| Call purposes, mapped | The runtime names each call `turn`, `bid`, `critic`, `revise`, `summary` or `compress`. A turn, critic or revise call is `reply`; `compress` is `summary`. Arm A's call has no adviser. Neither the reflexion loop (critic, revise) nor working-memory compression runs in EXP-001: reflexion needs `mode: plan` and `revise` of 1 or more, which the shipped `roundtable` channel leaves at `bid` and 0, and nothing calls compression. Those mappings only keep every line readable. PR 5e adds `keepalive`, arm D′'s chair keeping the prefix's cache entry alive, which stays a purpose of its own ([arm D′'s transcript prefix](EXP-001-harness-choices-d-prime.md)) | 3b, 5e |
| The memo's calls | A call is `memo` when it is the chair's turn, critic or revise, in that meeting, attempt and try (the try since PR 5b, so one try's memo request marks no other try's calls), begun at or after the moment the harness asked for the memo; both times are real time, and the harness asks only once the discussion has closed. A bid the chair makes before answering stays `bid`, and its memory summary after that stays `summary` | 3b, 5b |
| Memory writes in B, C and D′ | The runtime has no setting that stops memory writing: nothing turns off the summary written when an interaction closes. So those arms' `summary` calls are kept and priced in real spend but not counted in the arm's dollars per plan | 3b |
| Failed calls | A call that raised is logged with its error and no tokens, and read back apart from the records, so a failed call is never priced. A call cancelled after the provider began work, or retried inside the provider's library, may be billed for more than the log shows; the practice run compares the two | 3b |
| Arm A's prompt | The system prompt is the panel's arm A template, filled in, then a blank line and the meeting's instruction from `arm_a_by_meeting`; a control plan takes the plan's. The only user message is the operator's message, word for word. Nothing from an earlier meeting is sent | 4 |
| The advisers in arm A | Each adviser is its persona prompt's identity, background, behaviour and goals sections, rendered by the runtime's own section code from the agent config the channel arms deploy, a blank line apart as the runtime sets them; the advisers follow in `panel.yaml`'s order, a blank line apart, with one more after the last, so the template's closing line does not read as that adviser's last goal. The grounding section, which tells a persona to reply as itself, and the state section, which gives its mood, are left out. An adviser field no identity section renders, such as quirks, is refused, since the persona agents would read it and arm A would not; `knowledge` is kept, as no prompt renders it | 4 |
| Arm A's clock line | The now-anchor line an adviser's prompt shows as its meeting begins, when its [agent time](../ai-glossary.md#agent-time) reads 10:00:00 UTC on the story date. The runtime's own code renders it, and calls that hour late morning | 4 |
| Arm A's call | `claude-sonnet-4-6` with no alias; temperature 0.7, from `panel.yaml`; at most 4 096 output tokens, the persona agents' own limit, since the shipped personas set none; no tools and no cache prefix. The purpose is `turn`, read as `reply`, with no adviser. The reply's text is the memo at a plan meeting, unless it is empty or was cut off at the token limit: then the memo is missing, as it is when a persona agent's reply hits its limit and it posts nothing. Its stop reason and the real times the call began and ended are kept; the adapter reads a stop reason it does not know, a refusal included, as end_turn | 4 |

The choices of PRs 5a to 5c, which hold the meetings, are in
[their own document](EXP-001-harness-choices-meetings.md), those of PRs 5d
and 5e, arm D′'s transcript prefix, in [a third](EXP-001-harness-choices-d-prime.md),
PR 6a's, the judge, in [a fourth](EXP-001-harness-choices-judge.md), PR
6b's, the practice run, in [a fifth](EXP-001-harness-choices-practice.md),
and PR 6c's, the scored run, in [a sixth](EXP-001-harness-choices-scored.md).

## The arm order

The arm order each series runs in:

| Series | Order |
|---|---|
| 1 | A, C, D, D′, B |
| 2 | D′, D, C, B, A |
| 3 | B, A, C, D, D′ |
| 4 | C, A, B, D, D′ |
| 5 | C, B, D, D′, A |

## Related documentation

- [Holding the meetings](EXP-001-harness-choices-meetings.md) — the frozen
  choices of PRs 5a to 5c.
- [Arm D′'s transcript prefix](EXP-001-harness-choices-d-prime.md) — the
  frozen choices of PRs 5d and 5e.
- [The judge](EXP-001-harness-choices-judge.md) — the frozen choices of
  PR 6a.
- [The practice run](EXP-001-harness-choices-practice.md) — the frozen
  choices of PR 6b.
- [The scored run](EXP-001-harness-choices-scored.md) — the frozen choices
  of PR 6c.
- [EXP-001 harness](EXP-001-harness.md) — the harness PRs, and what each
  leaves the next.
- [EXP-001 pre-registration](EXP-001-preregistration.md) — materials, arms
  and the run.
