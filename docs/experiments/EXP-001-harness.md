# EXP-001 — The harness

> **Status**: 🚧 **In Progress** — PRs 1, 2, 3a, 3b, 4, 5a, 5b, 5c, 5d, 6a and 6b merged ([#984](https://github.com/mkhomutov/Persatrix/pull/984), [#986](https://github.com/mkhomutov/Persatrix/pull/986), [#987](https://github.com/mkhomutov/Persatrix/pull/987), [#988](https://github.com/mkhomutov/Persatrix/pull/988), [#992](https://github.com/mkhomutov/Persatrix/pull/992), [#993](https://github.com/mkhomutov/Persatrix/pull/993), [#1013](https://github.com/mkhomutov/Persatrix/pull/1013), [#1015](https://github.com/mkhomutov/Persatrix/pull/1015), [#1021](https://github.com/mkhomutov/Persatrix/pull/1021), [#1023](https://github.com/mkhomutov/Persatrix/pull/1023), [#1024](https://github.com/mkhomutov/Persatrix/pull/1024)); no practice run yet.
> **Last updated**: 2026-10-01
> **Carries out**: the [EXP-001 pre-registration](EXP-001-preregistration.md) and its [part 2](EXP-001-preregistration-scoring.md)
> **Code**: [`evaluators/exp001/`](../../evaluators/exp001/)

The pre-registration fixes what EXP-001 runs and how its scores decide. The
harness is the code that does it: it holds the five arms' meetings, records
and prices every model call, collects the memos, and does the arithmetic that
picks a rule. It lands in the reviewed PRs below, as
[pre-registration §3](EXP-001-preregistration.md#3-running-it) asks.

That section also says every choice a harness PR makes, where the documents
leave it open, is listed in the PR and frozen when it merges. The
[frozen choices](EXP-001-harness-choices.md) document, its second part,
[holding the meetings](EXP-001-harness-choices-meetings.md), its third,
[arm D′'s transcript prefix](EXP-001-harness-choices-d-prime.md), its
fourth, [the judge](EXP-001-harness-choices-judge.md), and its fifth,
[the practice run](EXP-001-harness-choices-practice.md), collect them, so
the result can cite them.

## The PRs

| PR | What it adds | Pre-registration checks it proves |
|---|---|---|
| 1 ([#984](https://github.com/mkhomutov/Persatrix/pull/984)) | Reads and checks the materials; records each model call; prices it with the fixed table; draws each series' arm order | 4 (the record's fields) |
| 2 ([#986](https://github.com/mkhomutov/Persatrix/pull/986)) | Scoring and the decision: the 400-word cut, memo quality, recall majority, per-plan agreement, the interval, the verdicts and the rule that fires, the price range and the cheaper-model repricing; blinded rater packets | — (part 2) |
| 3a ([#987](https://github.com/mkhomutov/Persatrix/pull/987)) | Agent time: memory stamps, recall ages and the orchestrator's timestamps all read one clock that `PERSATRIX_CLOCK_START` can shift, so the adviser's clock and arm D's memories agree on the story date | 2 and 7, in part |
| 3b ([#988](https://github.com/mkhomutov/Persatrix/pull/988)) | Runtime: the Anthropic adapter's prompt-cache marker and cache token counts; each meeting's clock settings; a [call log](../ai-glossary.md#call-log) that tags each call with its arm, meeting, adviser and purpose, and the reader that turns it into call records | 3, 4 and 7, in part |
| 4 ([#992](https://github.com/mkhomutov/Persatrix/pull/992)) | Arm A: one call per meeting, with the advisers' identities rendered by the persona runtime's own prompt code; the reader that checks the panel and builds each adviser's agent config | 3, 4, 7 and 8, for arm A |
| 5a ([#993](https://github.com/mkhomutov/Persatrix/pull/993)) | Arms B and C: a deployment of the advisers for every meeting, its channel from `panel.yaml`, the memo turn, and the failures a meeting can show | 1, 5 and 6, for B and C; 7 and 8 for the deployed advisers |
| 5b ([#1013](https://github.com/mkhomutov/Persatrix/pull/1013)) | Retries and failure detection for every arm, A included: provider errors retried, a series restarted from its briefing and then dropped, and the harness faults that stop the run | — (§3, attempts and failures) |
| 5c ([#1015](https://github.com/mkhomutov/Persatrix/pull/1015)) | Arm D: one deployment per series, with a channel for every meeting, and restarts between meetings that are checked to leave memory as it was; a meeting held again starts from the memory it began with | 2, for restarts; 5 and 6 for D |
| 5d ([#1021](https://github.com/mkhomutov/Persatrix/pull/1021)) | Arm D′: the earlier meetings' transcripts in a cached prompt prefix | 3, its reader; 1, 5 and 6 for D′ |
| 6a ([#1023](https://github.com/mkhomutov/Persatrix/pull/1023)) | The judge: its prompts from the rubric, one pass per packet, a call purpose and call log of its own that never name an arm, and the $25 cap | — (part 2 §2) |
| 6b ([#1024](https://github.com/mkhomutov/Persatrix/pull/1024)) | The practice run: every arm's practice series, each try kept, so a run started again goes on where it stopped; the practice memos judged in a batch of their own; and a report of what the checks show, the scored judging's projected spend and the call log's totals for the provider's usage report | all eight, on the practice series, once it is held |
| 6c | The scored run: every arm's series in its drawn order, harness faults counted and outputs set aside, the $150 cap and the seven-day window; dollars per plan, and the series every arm's comparisons keep; and, from PR 6b's review, the scored judging's projected spend at the scored packets' own sizes | — (§3, the run) |

Each PR is test-first, like all unit-level code here. The practice run PR
6b holds is the evidence that the eight checks pass before any scored
meeting. PR 5 is split in four, PR 6 in three and PR 3 in two, so each part
stays reviewable.

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

PR 5b holds every arm's series by the rules of attempts and failures, and
leaves two things. Arm D's memory carries from one meeting to the next, so
PR 5c's own way of holding a D meeting must give a second try the memory
its advisers had before the first: it keeps a copy of each adviser's store
from before the meeting and puts it back before trying again. PR 6b builds
arm B's and C's hold with `deployed_meeting.run_meeting` and the binary
bound to it, arm D's with `arm_d_hold` and the binary, arm D′'s with
`arm_d_prime_hold` and the binary, and arm A's with a new log per series;
`run_meeting` refuses D′ without the prefix only its hold gives. And PR
6c's run does what a harness fault asks beyond stopping: it counts the
faults, so a third ends the run, and it sets aside the scored outputs so
far, including the stopped series' tries, which the fault carries. It also
passes each series' finished tries to dollars per plan, and asks which
series every arm's comparisons keep.

PR 5c holds arm D and meets PR 3b's second constraint: a close summary cut
off at its 30-second limit is a provider error, and in D, whose summaries
count, the meeting is then held again from the memory it began with. It
rests on the fix for
[ISSUE-0172](../issues/ISSUE-0172-stopping-agent-drops-open-conversations.md):
a discussion that ends by its idle window tells no adviser, so each still
holds it open when the harness stops the advisers, and only since that fix
does the stop write it to memory. A stopping adviser writes it once the turn
it is running, and any queued behind that turn, has finished. The harness
kills an adviser still running 90 seconds after it was asked to stop, which
loses everything the adviser still held open, so in D a kill at the stop is
a harness fault, and the harness's wait of up to five minutes for the turns
in flight also guards D's memory. PR 5c leaves one thing. Check 2 has two halves, and PR 5c's tests show only the
second, that a restart leaves memory exactly as it was. The first, that a
briefing fact can reach a later meeting's prompt through the shipped memory
path, needs a model that writes real summaries; the offline mock provider
the tests run on writes placeholders. So PR 6b's practice run shows it,
through arm D's recall check: each meeting is a new channel, so the chair
can answer the briefing's questions only from memory.

PR 5d holds arm D′. Its meetings are arm C's, each on a new deployment with
empty stores and no memory, so nothing from an earlier meeting reaches a
prompt but the prefix (check 1). What carries from one meeting to the next
is text. From the series' second meeting on, the harness writes the
transcripts of the meetings before it, every message of each, oldest first,
into a file beside the try, and every adviser's turn carries that text at
the front of its prompt, marked for the cache, as its
[prompt prefix](../ai-glossary.md#prompt-prefix). In D only the turn receives
recalled memory, so only the turn carries it: no bid or memory summary
does. The call log names the prefix each call carried by its SHA-256, so
every turn can be matched to the file the harness wrote. PR 5d meets PR
3b's first constraint only in part. Every turn that carries the prefix has
the same tools ahead of it, so the four advisers share one cache entry. And
floor control gives one adviser the floor at a time, but moves on after 45
seconds whether or not that adviser's call has begun: a meeting's first turn
is under way before the next begins only if it starts within those 45
seconds, and the check names two writes when it does not. A try that
carries a prefix waits six minutes after the last try with the same prefix
stopped, so its first turn writes the prefix again rather than reading what
an earlier try paid for. A meeting whose orchestrator exited is carried as
far as its own store holds it.

PR 5d leaves three things to settle before any scored meeting. The first
is to show check 3 on the real provider: PR 6b's practice run does, with the
harness's check, which reads each try's calls, failed ones included, and
names every call that broke the check. The
second is a gap the practice run should confirm rather than discover. A
cache entry lives five minutes after the last call that wrote or read it
began, and a governed discussion can go quiet by design: the chair gets one
forced turn per discussion, and none on a stall after its own message. A
discussion that then closes by its 600-second idle window has made no call
for ten minutes, so the memo turn after it writes the prefix again, and
check 3 fails for that meeting. A longer-lived cache would change the fixed
price table, which only an amendment does; a harness that kept the entry
alive would be a harness PR. One of them is needed before the scored run.
The third is the discussion's token budget. Every turn charges its whole
prefix against it, and the cost close comes at 1 776 000 tokens for a room
of five, so a late-series D′ meeting, whose prefix holds five transcripts,
may reach it, which the pre-registration's 2 000 000 is meant to rule out.
The practice run adds up each D′ discussion's tokens against that mark
before any scored meeting. A prefix shorter than the model's minimum of
1 024 tokens is not cached at all, and the check names that too.

PR 6a adds the judge, the third rater, which scores the blinded packets PR 2
builds. It reads each packet in the same words as the two people, and
answers with the JSON the rubric's prompts ask for. Each packet gets one
answer. A provider error is tried again, since no answer came, but an answer
that came is kept and never asked for again, and one the harness cannot read
stops judging as a harness fault. The judge's calls go to a call log of
their own, with a purpose of their own, tagged with the packet's ID and
never its arm, and the $25 cap is read from that log before each call.

PR 6a leaves three things for PR 6b. Its run judges the practice memos in a
batch of their own, so their calls never count toward the scored judging's
cap, and gives the judge the order `rater_orders` draws for it. The practice
run shows whether the judge answers every packet readably within 16 000
output tokens, its default thinking included. An answer cut off, a refusal,
or a control plan scored on C2 would stop judging, and a fix to the reader
or to the call is a harness PR before any scored meeting. And before any
scored meeting, the run projects the scored judging's spend from the
practice batch's calls: 100 times a memo packet's mean cost plus 25 times a
recall packet's. At the token limit a call costs about $0.41, so the 125
scored calls fit the $25 cap only if they average under about 7 600 output
tokens, thinking included. A projection over the cap needs a change to the
call before the first scored meeting, since the caps freeze then.

PR 6b holds the practice run. Each arm named holds the practice series in
turn, and each try is kept as it ends, so a run started again goes on where
it stopped. The harness then draws the packets, once: each person's packets
go to a file of their own, and the seal is kept apart. The judge scores
them as batch `practice`, and the harness writes a report of what the run
shows:

- check 3, naming any memo turn that wrote the prefix again after a quiet
  spell outlasted the cache entry;
- each D′ discussion's tokens against the cost close, and projected to
  the five transcripts a scored series' recall check carries;
- what each meeting recorded, and the judge's marks on each arm's recall
  check, which D's chair can answer only from memory (check 2);
- the judge's largest answer against its token limit, and the scored
  judging's projected spend;
- every call's tokens by model, to compare with the provider's usage
  report.

To hold it, with the orchestrator built (`make build-orchestrator`), a key
used by nothing else in `ANTHROPIC_API_KEY`, and a directory outside the
repository, since memos and transcripts stay out of it:

```bash
python -m evaluators.exp001 practice ~/exp001/practice-1 --provider anthropic
```

`--provider offline` holds the meetings on the mock provider at no cost,
and judges nothing. PR 6b leaves the scored run to PR 6c: every arm's series
in its drawn order, the faults and dollars PR 5b names, the $150 cap, which
every scored attempt counts toward, discarded or not, and the seven-day
window.

PR 6b's review adds one thing, its finding F-8. The report projects the
scored judging's spend from the practice packets, but a scored memo packet's
input is about 28% longer. That adds only about $0.24, yet longer packets
may also take more thinking, which practice packets cannot show. PR 6c
settles the projection at the scored packets' own sizes, before the first
scored meeting, when the caps freeze.

## Frozen choices

The choices each harness PR froze, and the arm order each series runs in, are
in [their own document](EXP-001-harness-choices.md), so the result can cite
them. Those of PRs 5a to 5c, which hold the meetings, are in its second part,
[holding the meetings](EXP-001-harness-choices-meetings.md), PR 5d's in
its third, [arm D′'s transcript prefix](EXP-001-harness-choices-d-prime.md),
PR 6a's in its fourth, [the judge](EXP-001-harness-choices-judge.md), and
PR 6b's in its fifth, [the practice run](EXP-001-harness-choices-practice.md).

## Related documentation

- [EXP-001 pre-registration](EXP-001-preregistration.md) — materials, arms
  and the run.
- [Part 2: scoring and the decision](EXP-001-preregistration-scoring.md).
- [Frozen choices](EXP-001-harness-choices.md) — the choices of PRs 1 to 4,
  and the arm order.
- [Frozen choices: holding the meetings](EXP-001-harness-choices-meetings.md)
  — the choices of PRs 5a to 5c.
- [Frozen choices: arm D′'s transcript prefix](EXP-001-harness-choices-d-prime.md)
  — the choices of PR 5d.
- [Frozen choices: the judge](EXP-001-harness-choices-judge.md) — the choices
  of PR 6a.
- [Frozen choices: the practice run](EXP-001-harness-choices-practice.md) —
  the choices of PR 6b.
- [Evaluators guide](../evaluators-guide.md) — the golden-trace harness this
  one sits beside.
