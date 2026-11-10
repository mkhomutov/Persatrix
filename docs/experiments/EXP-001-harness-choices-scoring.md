# EXP-001 — The harness's frozen choices: scoring

> **Status**: 🚧 **In Progress** — PR 6d's rows freeze when it merges ([#1091](https://github.com/mkhomutov/Persatrix/pull/1091)).
> **Last updated**: 2026-11-10
> **Part of**: the [EXP-001 harness](EXP-001-harness.md); the [first part](EXP-001-harness-choices.md) holds the choices of PRs 1 to 4 and the arm order, the [second part](EXP-001-harness-choices-meetings.md) those of PRs 5a to 5c, the [third part](EXP-001-harness-choices-d-prime.md) those of PRs 5d and 5e, the [fourth part](EXP-001-harness-choices-judge.md) PR 6a's, the [fifth part](EXP-001-harness-choices-practice.md) PR 6b's, and the [sixth part](EXP-001-harness-choices-scored.md) PR 6c's

These are the frozen choices of PR 6d, which scores a run once its packets
are drawn and judged: it reads the two people's scores and the judge's,
opens the seal once every score is in, and works out the figures
[part 2](EXP-001-preregistration-scoring.md) of the pre-registration names.
Part 2 fixes the arithmetic, which PR 2 wrote; the rows below are what it
leaves open about scoring a run's directory. PR 6d merges after the first
scored meeting but before the seal is opened, so its rows are chosen blind
to every score.

[Part 1 §5](EXP-001-preregistration.md#5-changes) lets the harness
mechanics change only until the first scored meeting, and has the result
list every PR that changed them. PR 6d is a harness PR that merges after
that meeting, so here is where it stands:

- It changes no mechanic the scored run was held with. The materials, the
  caps and part 2 stay as they are.
- It adds the scoring step, which
  [PR 6c left to a PR of its own](EXP-001-harness-run.md#pr-6c-the-scored-run)
  before the run was held.
- The scored run's report, which PR 6c writes as the run ends, already
  gives each arm's dollars per plan. So these rows were not chosen blind to
  dollars, and none of them changes how dollars are counted.
- The result lists PR 6d as a harness PR that merged after the first scored
  meeting.

## Frozen choices

| Choice | What the harness does | PR |
|---|---|---|
| The score sheets | Each person writes their scores in a CSV file of their own, `raters/<person>-scores.csv`, beside the file of packets they read. The harness writes it blank whenever a run is scored while the seal is shut and the sheet is not there, one row per packet in the person's own order, and never writes over one that is. Its columns are `number`, `packet` and `kind`, then `C1` to `C5`, then each question the recall keys ask, `R1` and on. A cell that does not apply holds a dash: C2 on a memo whose key lists no earlier facts, every criterion on a recall reply, every question on a memo | 6d |
| Reading a sheet | A score is 0, 1 or 2; a mark is `right` or `wrong`, in any case; spaces around either are ignored. A packet with a cell that applies still empty is left, not yet scored. A cell that holds anything else, a mark where a dash belongs, a packet the person was not given, one listed twice, one left out or columns that differ are refused, with the row, so a sheet a spreadsheet changed is never read as scores its person did not give. A packet ID is matched exactly, so one a spreadsheet turned into a number is refused, and the refusal names the ID the row's number points to, for the person to put back. A row's `number` and `kind` must still be its packet's when they are not blank, so an ID that moved to another row, as sorting one column apart from the rest moves it, is refused. A sheet is read however a spreadsheet saves it while every cell is kept: a byte order mark, any line ends, semicolons or tabs between the cells, and blank rows or columns at the end. It must be UTF-8; another encoding is refused, saying how to save it | 6d |
| The judge's scores | Read back from the batch's kept answers as PR 6a's judging reads them, asking nothing. A batch is over when every packet is answered, or when the judge's cap stopped it, the packets after that left. One with a packet still to ask is not over. An answer asked with another prompt, one the harness cannot read, a call answered with no answer kept or an answer for a packet not in the batch is a harness fault, as when the batch was judged | 6d |
| Every score is in | Each person's sheet scores every packet they were given, and the judge's batch is over. Until then scoring reports how many packets each rater has left, and when scoring is due, and reads neither the seal nor any meeting's record. A judge its cap stopped is reported as stopped, with the number of packets it never judged | 6d |
| Opening the seal | Only once every score is in, and only when `--open` asks: every score in still waits to be asked, so a person can still change a score until then. Once the result is worked out, opening keeps `opened.json` beside the packets: the moment; a SHA-256 of each person's scores as their sheet gave them, so a sheet saved again with the same scores still reads; one of the judge's kept answers and calls, the packets and the seal; and one of the result itself. A seal the result cannot be worked out from opens nothing and keeps nothing. Opened, the seal stays open, and any later look works the result out again. No score may change once the arms are known, so that look is refused when a score or one of those files has changed, and when the result differs from the one kept for any other reason: an arm's calls or records, or the run's own state. A run that holds a result but no `opened.json` is refused too, and its seal is not opened a second time | 6d |
| When scoring finished | The moment the seal is opened. Part 2 §4's scoring is on time when that moment is no later than 21 days after the last scored meeting ended, the date PR 6c's report gives; later, the result is inconclusive. Scoring's report gives that date each time, so the seal is opened by then. A practice run has no due date | 6d |
| Which run is scored | A scored run's last window, once its report would call the run complete: no fault closed the window, and its meetings ended with four series or more kept and the cap not reached. Anything else, or a window with no scored meeting ended, is refused, since nothing it drew can be scored. A practice run is scored in its own directory, unless a harness fault closed it; its one series fires no rule. One run at a time holds the directory, so scoring is refused while the scored run judges, and is run again once judging is over | 6d |
| The seal | The seal must name every answer the kept series asked of every arm, a plan's memo or a recall check's reply, once each, either as a packet drawn or as an answer missing; anything else is refused. A memo missing scores 0 on every criterion from every rater, and a recall reply missing is wrong on every question | 6d |
| The judge's cap | A judge its cap stopped before every packet makes the result inconclusive (part 2 §4). The judge's scores then cannot be averaged in, so every figure is the two people's alone, and a recall answer is right when both people say so | 6d |
| The comparisons | All five, C against A, D against C, D against D′, B against A and C against B, are given whenever four or five series are kept, each with every series difference, whether or not a rule fires. Those the decision read are marked as the checks. B's two never decide | 6d |
| Each check's price range and repricing | For each check the decision read, the range of prices p over which its verdict, as it came out at $1, still holds: "clearly beats" for C against A and "beats" for the others, found as PR 2's row finds it. And the same verdict with bids and memory summaries repriced at `claude-haiku-4-5`. The result also gives the rule that would fire with that repricing, which never decides | 6d |
| The rule on the people alone | Each memo's quality the two people's average, their own per-plan agreement against the 0.4 floor, and the same checks, run complete and on time as the result is. The judge's cap does not bear on it | 6d |
| Dollars per plan | Each arm's, in each kept series, from its pair's calls, as PR 6c's report counts them: the finished attempt, each meeting's finished try, the briefing and the plans; and the same repriced. An arm's figure is the average over the kept series. A practice series divides by its own two plans, not four | 6d |
| Minutes per plan | Each plan's finished try, from the operator's message to the finished memo: a channel arm's opening message to its chair's memo, and arm A's one call from asked to answered. A plan whose memo was never written has no minutes: it is left out of the arm's average, and the result says how many were. Quality per minute is the arm's quality over its minutes per plan | 6d |
| Each arm's figures | Quality is the average over every plan the arm answered in the kept series, a missing memo counted as 0, with plans and control plans also apart. Each criterion's figure averages every rater's score on every such memo, a missing one 0, and C2 only over the plans that score it. Quality per dollar is quality over dollars per plan | 6d |
| Each meeting | Every meeting of every kept series in every arm, from its finished try: what closed the discussion, as its record names it, or nothing for arm A's one call; whether the try left a record, and whether the answer the meeting asked for was written, so a discussion that never closed, a try with no record and a missing memo or reply each say so; each adviser's energy at the memo turn, as the record gives it; and whether the memo was cut at 400 words, with its length, from the seal | 6d |
| The result | `scores.json`, and the same in plain words in `scores.txt`, in the run's directory, written whenever the seal is open. When no rule fires, it names every cause that holds, not only the first | 6d |

## Related documentation

- [EXP-001 harness](EXP-001-harness.md) — the harness PRs, and what each
  leaves the next.
- [The harness, third part](EXP-001-harness-scoring.md) — scoring a run,
  and how to do it.
- [Frozen choices: the scored run](EXP-001-harness-choices-scored.md) — the
  choices of PR 6c.
- [EXP-001 pre-registration, part 2](EXP-001-preregistration-scoring.md) —
  scoring, agreement and how a rule fires.
