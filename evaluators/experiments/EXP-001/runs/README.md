# EXP-001 — The scored run's records

These are the records published with the
[EXP-001 result](../../../../docs/experiments/EXP-001-result.md), whose
[third part](../../../../docs/experiments/EXP-001-result-changes.md) lists
them. Each file is
a byte-for-byte copy of what the harness or a rater's session wrote during
the run. One was put together by the coordinating session: `notes.md`,
word for word from the two raters' kept answers. Nothing here is edited by
hand, and nothing here is changed later.

The run's own files stayed outside the repository until every score was in,
as the pre-registration asks. They were copied here on 2026-11-27, after
the seal opened on 2026-11-26.

## `scored/` — the run and its result

| File | What it holds |
|---|---|
| `scores.json`, `scores.txt` | The result as the harness worked it out when the seal opened: the rule, the raters' agreement, every comparison, each arm, each memo and each meeting. `scores.txt` says the same in plain words |
| `report.json`, `report.txt` | The scored run's report, written when its meetings ended: every try, dollars per plan, real spend and the judge's calls |
| `run.json` | The run's own state: its one window, the series kept and when each ended |
| `window-1/opened.json` | The moment the seal opened, and a SHA-256 of each score sheet, the judge's answers and calls, the packets, the seal and the result |
| `window-1/pairs/<series>/<arm>/tries.jsonl` | The transcripts. One line per meeting: every message posted in the meeting's channel, the memo or recall reply, what closed the discussion and each adviser's energy. Arm A's lines hold its one call's reply |
| `window-1/pairs/<series>/<arm>/pair.json` | Which attempt of that series the arm finished on. All finished on their first |

The arm the harness calls `D-prime` is arm D′.

## `ai-raters/` — the two AI raters' records

Two AI raters scored the run in the two people's place, by the
[deviation](../../../../docs/experiments/EXP-001-deviation-ai-raters.md).
Its rule 6 lists what is published. `person-1/` is rater 1 and `person-2/`
rater 2. Each has a `practice/`, a `notes/` and a `scored/` folder.

| File | What it holds |
|---|---|
| `notes.md` | The 11 notes on how the anchors apply that both raters accepted in practice. Every scored prompt holds them |
| `batch-NN.md`, `notes-N.md` | A prompt: what one fresh run was given to read |
| `<prompt>.run-N.answer.md` | That run's answer, when it was entered or kept |
| `<prompt>.run-N.public.jsonl` | That run's record, without what Claude Code adds around the messages |
| `runs.jsonl` | The session's log: one line per run, entered, kept or refused, with how the run was set up and a SHA-256 of its prompt, its full record and its public record |

These files name the maintainer's home folder, in each brief's path and in
`runs.jsonl`. The deviation says so in advance.

Two files are not here yet: `person-1/practice/batch-01.run-1.public.jsonl`
and `person-2/practice/batch-01.run-1.public.jsonl`, the public records of
the two refused practice runs. Rule 6 has the maintainer read a refused
run's copy before it is published.

## What is not here

The maintainer keeps the rest of the run unpublished: each rater run's full
record, the advisers' memory stores and logs, the per-call logs, the packets
file, the seal, the judge's answers and the two score sheets.
`opened.json` and each `runs.jsonl` hold the hashes those files can be
checked against.
