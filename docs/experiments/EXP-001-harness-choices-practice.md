# EXP-001 — The harness's frozen choices: the practice run

> **Status**: 🚧 **In Progress** — PR 6b's rows freeze when it merges ([#1024](https://github.com/mkhomutov/Persatrix/pull/1024)).
> **Last updated**: 2026-09-30
> **Part of**: the [EXP-001 harness](EXP-001-harness.md); the [first part](EXP-001-harness-choices.md) holds the choices of PRs 1 to 4 and the arm order, the [second part](EXP-001-harness-choices-meetings.md) those of PRs 5a to 5c, the [third part](EXP-001-harness-choices-d-prime.md) PR 5d's, and the [fourth part](EXP-001-harness-choices-judge.md) PR 6a's

These are the frozen choices of PR 6b, which holds the practice run: the
practice series for every arm, the practice memos judged, and a report of
what the run shows. [Pre-registration §3](EXP-001-preregistration.md#3-running-it)
says the practice series runs first, as often as needed, until the eight
checks pass, and is never scored; the rows below are what it leaves open.
The first part's rule holds for them too: until the first scored meeting a
row changes only through a later harness PR that says what changed and why,
and from then on nothing changes.

## Frozen choices

| Choice | What the harness does | PR |
|---|---|---|
| The practice run's arms | Each arm named holds the practice series, one arm at a time, in the order named; by default all five, in the arms' own order, A, B, C, D and D′. No order is drawn, since no rule reads the practice scores | 6b |
| Each arm's hold | Arm A makes its one call a meeting through the harness's own model client and logs every try of the series to one new file in the arm's directory. Arms B and C hold each try on a new deployment, and arms D and D′ by their own holds, all running the orchestrator binary this checkout builds. Every model alias points at `claude-sonnet-4-6`; offline, at the offline mock provider, which arm A's call then uses too | 6b |
| Each try kept | Every try, cut short or not, is kept as it ends, as one line of the arm's `tries.jsonl`: the meeting, attempt and try, whether it started and why not, the classes of the failed calls that bear on it, the arm's whole record of the meeting, and its answer. The answer is the chair's memo, or its answers at the recall check, or arm A's reply. There is none at a briefing, and none when it is missing: a memo never written, or arm A's reply empty or cut off at its token limit | 6b |
| A run started again | A practice run's directory keeps the arms it was started with, in `run.json`, and a start with other arms is refused: they get a practice run of their own. An arm's series held to the end is marked in `pair.json` with the attempt that finished, or none when the series was dropped, and a run started again in the same directory reads it back rather than holding it again. One stopped partway, by Ctrl-C, a hangup or a crash, has no mark: its directory moves whole into `interrupted`, numbered, and the series is held again from its briefing. Its calls stay on disk and count in the report's usage totals. The packets are drawn once, and the judge is never asked again about a packet it answered | 6b |
| A harness fault | A harness fault while the meetings are held stops the run and closes its directory, recorded in `run.json`, so a practice run after the fix starts in a new directory and none mixes meetings held before and after a fix. A fault while judging leaves the directory open, since a reviewed fix to the judge's reader reads the kept answers again without a second pass. No practice fault counts toward the scored run's three | 6b |
| The practice packets | Once every arm has held the series, each answer asked for is gathered from its meeting's finished try: the memo of each plan meeting and the answers of the recall check. A missing one goes to no rater and is listed, and a dropped series gives none. They are blinded as PR 2 blinds them, and each rater's order is drawn: the two people, `person-1` and `person-2`, and the judge. Each person's packets go to a file of their own, in their order, each headed by its ID and shown as every rater reads it. The seal goes to a folder of its own, with the missing answers and each memo's word count and whether it was cut | 6b |
| The practice judging | The judge scores every practice packet once, in the order drawn for it, as batch `practice` in a directory of its own, so none of its calls counts toward the scored judging's cap. Offline nothing is judged, since the mock provider cannot answer as the judge | 6b |
| The practice report | `report.json`, and the same in plain words in `report.txt`. For each try: what closed the discussion, the failures its record names, and whether its answer is missing. Check 3, read over every arm's calls, with each D′ try's prefix as the harness wrote it. Each D′ discussion's tokens against 1 776 000, the cost close for a room of five: the turns and bids begun before the close, each counting every input token it carried, cached ones included, and its output, as the wallet counts them. The judge's marks on each arm's recall check, read back through the seal, since D's chair can answer them only from memory (check 2). The judge's output tokens against its 16 000 limit, and the scored judging's spend projected from the practice batch: 100 times a memo packet's mean cost plus 25 times a recall packet's, against $25, a packet's cost being every call it made | 6b |
| The usage comparison | The report totals every call the run made, set-aside series included, by model: calls, input, output, cache write and cache read tokens, and dollars at the fixed table, with the failed calls by class and the window from the first call's start to the last. The comparison with the provider's own usage report is made by hand, for the run's own API key over that window. A difference is a call the provider billed that the call log missed, such as a request the provider's library retried inside one call | 6b |

## Related documentation

- [EXP-001 harness](EXP-001-harness.md) — the harness PRs, what each
  leaves the next, and how to hold the practice run.
- [Frozen choices, first part](EXP-001-harness-choices.md) — PRs 1 to 4,
  and the arm order.
- [Frozen choices, second part](EXP-001-harness-choices-meetings.md) — PRs 5a
  to 5c, holding the meetings.
- [Frozen choices, third part](EXP-001-harness-choices-d-prime.md) — PR 5d,
  arm D′'s transcript prefix.
- [Frozen choices, fourth part](EXP-001-harness-choices-judge.md) — PR 6a,
  the judge.
- [EXP-001 pre-registration](EXP-001-preregistration.md) — the practice
  series, and the eight checks it must pass before any scored meeting.
