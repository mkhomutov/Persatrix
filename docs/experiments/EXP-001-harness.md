# EXP-001 — The harness

> **Status**: 🚧 **In Progress** — PRs 1, 2, 3a, 3b and 4 merged ([#984](https://github.com/mkhomutov/Persatrix/pull/984), [#986](https://github.com/mkhomutov/Persatrix/pull/986), [#987](https://github.com/mkhomutov/Persatrix/pull/987), [#988](https://github.com/mkhomutov/Persatrix/pull/988), [#992](https://github.com/mkhomutov/Persatrix/pull/992)); PR 5a open ([#993](https://github.com/mkhomutov/Persatrix/pull/993)); no practice run yet.
> **Last updated**: 2026-09-24
> **Carries out**: the [EXP-001 pre-registration](EXP-001-preregistration.md) and its [part 2](EXP-001-preregistration-scoring.md)
> **Code**: [`evaluators/exp001/`](../../evaluators/exp001/)

The pre-registration fixes what EXP-001 runs and how its scores decide. The
harness is the code that does it: it holds the five arms' meetings, records
and prices every model call, collects the memos, and does the arithmetic that
picks a rule. It lands in the reviewed PRs below, as
[pre-registration §3](EXP-001-preregistration.md#3-running-it) asks.

That section also says every choice a harness PR makes, where the documents
leave it open, is listed in the PR and frozen when it merges. The
[frozen choices](EXP-001-harness-choices.md) document collects them in one
place, so the result can cite them.

## The PRs

| PR | What it adds | Pre-registration checks it proves |
|---|---|---|
| 1 ([#984](https://github.com/mkhomutov/Persatrix/pull/984)) | Reads and checks the materials; records each model call; prices it with the fixed table; draws each series' arm order | 4 (the record's fields) |
| 2 ([#986](https://github.com/mkhomutov/Persatrix/pull/986)) | Scoring and the decision: the 400-word cut, memo quality, recall majority, per-plan agreement, the interval, the verdicts and the rule that fires, the price range and the cheaper-model repricing; blinded rater packets | — (part 2) |
| 3a ([#987](https://github.com/mkhomutov/Persatrix/pull/987)) | Agent time: memory stamps, recall ages and the orchestrator's timestamps all read one clock that `PERSATRIX_CLOCK_START` can shift, so the adviser's clock and arm D's memories agree on the story date | 2 and 7, in part |
| 3b ([#988](https://github.com/mkhomutov/Persatrix/pull/988)) | Runtime: the Anthropic adapter's prompt-cache marker and cache token counts; each meeting's clock settings; a [call log](../ai-glossary.md#call-log) that tags each call with its arm, meeting, adviser and purpose, and the reader that turns it into call records | 3, 4 and 7, in part |
| 4 ([#992](https://github.com/mkhomutov/Persatrix/pull/992)) | Arm A: one call per meeting, with the advisers' identities rendered by the persona runtime's own prompt code; the reader that checks the panel and builds each adviser's agent config | 3, 4, 7 and 8, for arm A |
| 5a ([#993](https://github.com/mkhomutov/Persatrix/pull/993)) | Arms B and C: a deployment of the advisers for every meeting, its channel from `panel.yaml`, the memo turn, and the failures a meeting can show | 1, 5 and 6, for B and C; 7 and 8 for the deployed advisers |
| 5b | Retries and failure detection for every arm, A included: provider errors retried, a series restarted from its briefing and then dropped, and the harness faults that stop the run | — (§3, attempts and failures) |
| 5c | Arm D: one deployment per series, and restarts between meetings that leave memory as it was | 2; 5 and 6 for D |
| 5d | Arm D′: the earlier meetings' transcripts in a cached prompt prefix | 3; 1, 5 and 6 for D′ |
| 6 | The judge, with a call purpose of its own, and the practice run, which also checks the call log's total against the provider's usage report | all eight, on the practice series |

Each PR is test-first, like all unit-level code here. PR 6's practice run is
the evidence that the eight checks pass before any scored meeting. PR 5 is
split in four, as PR 3 was in two, so each part stays reviewable.

PR 3b leaves two constraints for PRs 5c and 5d. Anthropic can read a cache
entry only once the response that wrote it has begun, and it keys the entry
on the tool list as well as the prefix. So every call that carries D′'s
prefix must share one tool list, and a meeting's first such call must be
under way before the next one starts; otherwise each writes its own entry and
check 3 fails. And a close summary that runs past its 30-second limit is
cancelled: in D that adviser loses its summary and facts of the meeting, and
the call may still be billed, so D's failure detection must catch it.

PR 4 leaves PR 5 two more. Arm A renders each adviser from the agent config
the panel reader builds, and shows the clock line of an adviser on UTC. So the
channel arms must deploy the advisers from that same config, adding nothing
under `persona` and changing none of its fields, with the panel's
`temperature` and no `max_tokens`; otherwise checks 7 and 8 no longer hold. A
`persona.timezone` would move the advisers' clock line, and `persona.quirks`
would add a section arm A never shows. PR 5a deploys them that way, and a
test builds each adviser from the `agents.yaml` a deployment writes and finds
arm A's sections in its prompt. Its clock line is arm A's line moved on by the
real time since the meeting began, since a deployed adviser's clock keeps
running; the test allows for that time. And arm A logs a failed call
and raises it: the retries the pre-registration asks for come with PR 5b, for
every arm at once.

PR 5a leaves three things for the parts after it. Each meeting of B and C
writes its own call log, so PR 5b can read a meeting's provider errors from
it: a failed line names the exception's class. A deployment that will not
start, a rate limiter left on, a wallet not served and a lease a spending
limit refused each raise `DeploymentError`; PR 5b decides which of them are
harness faults. And arm D
declares its series' six channels in one deployment, each named for its
meeting's place as B and C name theirs, well inside the shipped cap of 50
channels.

## Frozen choices

The choices each harness PR froze, and the arm order each series runs in, are
in [their own document](EXP-001-harness-choices.md), so the result can cite
them in one place.

## Related documentation

- [EXP-001 pre-registration](EXP-001-preregistration.md) — materials, arms
  and the run.
- [Part 2: scoring and the decision](EXP-001-preregistration-scoring.md).
- [Frozen choices](EXP-001-harness-choices.md) — every choice a harness PR
  froze, and the arm order.
- [Evaluators guide](../evaluators-guide.md) — the golden-trace harness this
  one sits beside.
