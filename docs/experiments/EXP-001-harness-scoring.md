# EXP-001 — The harness, third part: scoring

> **Status**: 🚧 **In Progress** — PR 6d ([#1091](https://github.com/mkhomutov/Persatrix/pull/1091)), the scoring, is the last harness PR; its choices freeze when it merges.
> **Last updated**: 2026-11-10
> **Part of**: the [EXP-001 harness](EXP-001-harness.md), whose first part lists every harness PR; the [second part](EXP-001-harness-run.md) holds the judge, the practice run and the scored run

The [second part](EXP-001-harness-run.md) reached its size limit, so PR
6d's account starts here. PR 6c holds the scored run, draws its packets and
has the judge score them. What is left is step 4 of
[pre-registration §6](EXP-001-preregistration.md#6-from-here-to-the-result):
two people score every packet within 21 days, and only then is the seal
opened, the map from each packet's ID back to the arm that wrote it. PR 6d
does the harness's part of that step. It reads the people's scores and the
judge's, opens the seal once every score is in, and works out every figure
[part 2](EXP-001-preregistration-scoring.md) names. Its choices are in
[their own document](EXP-001-harness-choices-scoring.md).

## Scoring a run

Scoring makes no model call, so it takes no provider. Once the scored run
has drawn its packets and the judge has scored them:

```bash
python -m evaluators.exp001 score ~/exp001/scored
```

The first time, this writes each person a blank score sheet beside the
packets they read. Every time, it says how many packets each rater has
left, and the date scoring is due. It reads no seal and no answer's arm, so the people, and whoever
runs it, can run it as often as they like while scoring goes on. Once every
score is in, it says so, and the seal opens only when asked:

```bash
python -m evaluators.exp001 score ~/exp001/scored --open
```

Asked before every score is in, it opens nothing and fails. Opened, the
seal stays open: the same command, with or without `--open`, gives the
result again. A practice run's directory is scored the same way, so the
people can try their sheets on the practice packets first, which part 2 §2
has them score together and throw away.

## The score sheets

Each person writes their scores in `raters/person-1-scores.csv` or
`raters/person-2-scores.csv`, beside the file of packets they read. A
spreadsheet opens it. It has one row per packet, in the person's own
order, and these columns:

- `number` and `packet`: where the packet comes in the person's file, and
  its ID, as the packet's heading gives them;
- `kind`: `memo` or `recall`;
- `C1` to `C5`: a memo's five scores, each 0, 1 or 2;
- `R1` and on: a recall reply's mark for each of its key's questions,
  `right` or `wrong`.

A cell that does not apply holds a dash: C2 on a memo whose key lists no
earlier facts, which is a control plan, every criterion on a recall reply,
and every question on a memo. A packet with a cell still empty is not yet
scored. Anything else that is not a score, a mark on a cell with a dash, a
packet the person was not given, one listed twice or one left out is
refused with its row, never guessed at. So is a row whose `number` or
`kind` is no longer its packet's. Sorting one column apart from the rest
moves IDs to other rows, and only those two cells can tell.

A spreadsheet can turn a packet ID such as `12e45678` into a number, or
drop the zeros an ID begins with. About one ID in a hundred is at risk, so
a scored run's sheet is more likely than not to hold one. That ID is then
refused, and the refusal names the ID the row's number points to. The
person puts it back, and keeps the packet column as text whenever a
spreadsheet opens the sheet.

A sheet is read however a spreadsheet saves it, as long as every cell is
kept: with a byte order mark, with any line ends, and with semicolons or
tabs between the cells where a spreadsheet writes those instead of commas.
It must be UTF-8. One saved in another encoding is refused, and the refusal
says how to save it.

## Opening the seal

Every score is in when each person's sheet scores every packet they were
given and the judge's batch is over: every packet answered, or the judge's
$25 cap reached. A judge its cap stopped is shown as stopped, with the
number of packets it never judged, not as every score in. The judge's
answers are read as they were kept, asking nothing. An answer asked with
another prompt, or one the harness cannot read, is a harness fault, as it
was when the batch was judged.

When the seal opens and the result is worked out, the harness keeps
`opened.json` beside the packets. It holds the moment, and a SHA-256 of:

- each person's scores, so a sheet saved again with the same scores still
  reads;
- the judge's answers and calls, the packets and the seal;
- the result itself.

That moment is when scoring finished, so scoring is on time when it falls
within 21 days of the last scored meeting's end, the date the scored run's
report gives and scoring repeats each time: the seal is opened by then.

No score may change once the arms are known. So every later look works the
result out again and compares it with the one kept:

- A changed score, or a change to one of the files named above, is refused
  by name.
- Anything else that changes the result is refused too. That covers each
  arm's calls and records, which its dollars and minutes come from, and the
  run's own state, which holds the due date.
- A run that holds `scores.json` but no `opened.json` is refused as well.
  Its seal was opened before, and it is not opened a second time.

A scored run is scored only when its report would call it complete: its
last window is closed by no fault and holds the packets of four series or
more. One run at a time holds the directory, so scoring is refused while
the scored run still judges, and is run again once judging is over.

## What the result holds

Through the seal, every score reaches the memo or recall reply it scores.
The result goes to `scores.json` and, in plain words, to `scores.txt`, in
the run's directory:

- **The rule that fires**, or why none does: an incomplete run, the raters'
  agreement below 0.4, scoring finished late, or the judge's cap reached
  (part 2 §4 to §6). Every cause that holds is named, not only the first.
- **Agreement**: each pair of raters' per-plan rank correlation, averaged
  over the plans, with the plans used and skipped, the same correlation
  over every memo at once, and the three pairs' average (§4).
- **The comparisons**: C against A, D against C and D against D′, which are
  the checks, and B against A and C against B, which never decide. Each has
  every series difference, the interval and the verdicts. For each check
  the decision read, the result also gives the range of prices for a quality
  point over which its verdict holds, and the verdict with bids and memory
  summaries repriced at `claude-haiku-4-5` (§5, §7).
- **Reported, never deciding**: the rule with that repricing, and the rule
  on the two people's sheets alone (§7), which two AI raters fill in the
  people's place.
- **Each arm**: quality, on plans and on control plans apart; dollars per
  plan, at the fixed table and repriced; quality per dollar; minutes per
  plan and quality per minute; each criterion's average score; and its
  recall answers right (§7).
- **Each memo**: its packet, each rater's total and its quality, a memo
  never written scoring 0; and each recall answer's marks.
- **Each meeting**: what closed the discussion, each adviser's energy at the
  memo turn, whether the memo was cut at 400 words, and a plan's minutes
  from the operator's message to the finished memo (§7). A discussion that
  never closed, a try that left no record and an answer never written each
  say so.

If the judge's cap stops it before every packet, the judge's scores cannot
be averaged in, so the result is inconclusive and every figure is the two
people's alone.

## The practice run, scored

Before review, the scoring was run end to end on a copy of the practice run
held on 2026-10-16, with made-up scores. The made-up scores were drawn at
random, without the seal. The first look wrote both blank sheets, 15 rows
each, with the dashes where they belong, and gave each person 15 packets
left and the judge none. Asked to open then, it opened nothing and failed.
With the sheets filled in, it said every score was in, and only `--open`
opened the seal. Once it was open, a sheet saved again as a spreadsheet
saves it, with a byte order mark and Windows line ends but the same scores,
gave the same result, and a sheet with one score changed was refused.

The result read the practice run's records as the scored run's will be
read. It fired no rule, since a practice run holds one series, not four.
Random scores agree with nothing, so the raters' agreement came to 0.07.
Two memos were cut at 400 words, one of 403 words and one of 410. The
minutes per plan ran from 0.3 for arm A's one call to about 15 for D′'s
discussions, and every meeting showed what closed it: the idle window,
or the depth cap in three of arm B's.

## AI raters in the people's place

On 2026-11-10 the maintainer decided that two AI raters score the scored
run in the two people's place. That is a
[deviation](EXP-001-deviation-ai-raters.md) from part 2, and it sets the
rules they follow. Each rater's own session briefs it with three commands,
and never reads a packet itself. For rater 1, on the scored run:

```bash
python -m evaluators.exp001 rater-batches ~/exp001/scored/window-1 person-1 OUT --notes NOTES
python -m evaluators.exp001 rater-enter ~/exp001/scored/window-1 person-1 OUT/batch-01.md RECORD
```

`rater-batches` splits the rater's packet file into prompts of up to eight
packets each, in the rater's order, fewer when eight would not fit one
Read. Each prompt holds the rubric, the notes agreed in practice, and the
cells to score for each packet, taken from the rater's blank sheet. It
takes no notes on the practice run and needs them on a scored one.
`rater-brief OUT/batch-01.md` prints the one message a fresh run of that
prompt is given. `rater-enter` copies the run's own record beside the
prompt, reads the answer from it and enters it in the sheet exactly as
given. It refuses a record that shows another model, any message but the
brief, project instructions, memory or a git status, or a tool call other
than one Read of the whole prompt. It refuses an answer that does not give
exactly the cells asked for, or could be read two ways. It never scores a
cell twice, and logs every run, entered or refused, in `OUT/runs.jsonl`.

Four more commands check the rest:

- `rater-keep PROMPT RECORD` checks and keeps a run that writes the
  practice notes, as `rater-enter` checks a scoring run;
- `rater-audit OUT RECORDS` lists any run in a session's folder of run
  records that was never entered, kept or refused;
- `rater-same-notes OUT1 OUT2` checks both raters' scored prompts hold one
  set of notes;
- on a practice run, `rater-differences ROOT` lists the cells the two
  raters scored differently, so they can agree on how the anchors apply.

## What PR 6d leaves

Step 5 of pre-registration §6 writes `docs/experiments/EXP-001-result.md`
from `scores.json` and commits the transcripts under
`evaluators/experiments/EXP-001/runs/`. Part 2 §2 also asks the result to
say who read what, and when. The harness cannot know that, so the people
record it. Nothing from the scored run reaches the repository before every
score is in.

## Related documentation

- [EXP-001 harness](EXP-001-harness.md) — the first part: every harness PR.
- [The harness, second part](EXP-001-harness-run.md) — the judge, the
  practice run and the scored run.
- [Frozen choices: scoring](EXP-001-harness-choices-scoring.md) — the
  choices of PR 6d.
- [EXP-001 pre-registration, part 2](EXP-001-preregistration-scoring.md) —
  scoring, agreement and how a rule fires.
