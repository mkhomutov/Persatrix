# EXP-001 — Result, second part: how the run was scored

> **Status**: ✅ **Implemented** — scoring is finished and the seal is open. This part is written once, with the [result](EXP-001-result.md).
> **Last updated**: 2026-11-27
> **Part of**: the [EXP-001 result](EXP-001-result.md), which gives the rule and every figure. This part gives the record the [AI raters deviation](EXP-001-deviation-ai-raters.md) asks for: the raters' runs, each difference from their rules, and who read what, and when.

Two AI raters scored EXP-001's scored run in the place of two people. The
[deviation](EXP-001-deviation-ai-raters.md) set eight rules for them before
any rater read a scored packet, and asked the result to report how the
scoring went. This is that report. Times are UTC.

## 1. Who did what

- **Rater 1 and rater 2.** Each is a Claude Code session of its own, on
  Fable 5.2, `claude-fable-5-2`, in an empty folder outside any repository.
  A rater's session never reads a packet. It hands each prompt to a fresh
  run of the same model, which reads that one prompt, calls no other tool
  and hands back its scores. Rater 1 filled person-1's sheet and rater 2
  person-2's.
- **The judge.** `claude-opus-5`, one pass per packet, on 2026-11-06, as
  registered. No rater saw its scores.
- **The coordinating session.** A third session, on Opus 5.5,
  `claude-opus-5-5`, started the two raters' sessions, put their practice
  notes side by side, ran the checks and opened the seal. The rules give
  those steps to the session that built the harness
  ([difference 1](#4-eight-differences-from-the-rater-rules)).
- **The maintainer** gave the word to start the raters, to resume them and
  to open the seal.

Every run's record shows replies from `claude-fable-5-2` alone, and every
answer names that model on its first line. Both raters' sessions ran at one
reasoning effort, high. The app started both sessions on `claude-opus-5-5`;
they were switched to `claude-fable-5-2` before their first command.

## 2. The runs, and the ones held again

Each rater held 23 runs, 46 in all.

| | Practice | Notes | Scored |
|---|---|---|---|
| Rater 1 | 3 held: 2 entered, 1 refused | 2 kept | 18 held, 18 entered: 125 packets |
| Rater 2 | 3 held: 2 entered, 1 refused | 2 kept | 18 held, 18 entered: 125 packets |

**Two runs were held again, both in practice.** Each rater's first practice
run, of its first batch, was refused on 2026-11-20 at 22:17. Both runs had
followed the rules. Claude Code had given each its own commit and
pull-request attribution lines, a kind of added text the harness's check on
a run's record did not know, so the check refused the record. As rule 7
provides, the harness was corrected by a pull request the maintainer
reviewed, [#1140](https://github.com/mkhomutov/Persatrix/pull/1140), merged
on 2026-11-20 at 23:26. Each batch was then held again whole by a fresh
run, and that run was entered.

**No scored run was refused, failed or held again.** Each of the 18 scored
batches took one run per rater. Scoring ran on 2026-11-26 from 11:46 to
12:17.

**Set-up.** Every notes run and every scored run was on Claude Code 2.1.328
and the harness at `ff411f9`, the commit that merged #1140. Before the seal
opened, `rater-ready` on the scored window listed no difference between the
36 scored runs' set-ups. On the practice run it listed one: three practice
runs were on Claude Code 2.1.325 and one on 2.1.328.

**The checks before the seal.** `rater-same-notes` passed on both folders of
scored prompts before any scored run was briefed. Once every score was in,
`rater-audit` listed nothing for either rater, and `rater-ready` said the
seal may be opened.

**Cost.** No money beyond the maintainer's Claude Code plan: no API key was
used. The 46 runs wrote 1 734 202 tokens to the cache, read 4 037 442 from
it, and used 184 other input tokens and 444 881 output tokens. That counts
the runs' own records, not the three sessions around them. The deviation
sets no cap on the raters.

## 3. The notes agreed in practice

Both raters first scored the practice run's 15 packets, with no notes. They
differed in 3 cells, in 3 packets.

Each rater's first notes run then wrote ten short notes on how the anchors
apply. They are about criterion 5, on made-up facts, estimates and errors,
and criterion 4, on comparing every option. The coordinating session put the
two lists together, 20 notes. Each rater's second notes run accepted or
rejected each one.

Both accepted the same 11 and rejected the same nine: seven as repeats of
an earlier note, and two for changing what the rubric asks. The
[notes file](../../evaluators/experiments/EXP-001/runs/ai-raters/notes.md)
holds those 11, word for word, and every scored prompt holds it. The
practice scores are thrown away.

## 4. Eight differences from the rater rules

The [rater rules](EXP-001-deviation-ai-raters.md#5-the-rater-rules) were
fixed on 2026-11-10. The scoring differed from them, or from what they
expected, in eight ways. None changed a score after it was given.

1. **Who coordinated.** Rules 7 and 8 give the coordinating steps to the
   session that built the harness; a session on Opus 5.5 did them, which
   wrote the harness correction (#1140) and not the harness itself.
2. **The harness moved during practice.** The raters started on `27745087`,
   the commit rule 2 names, and moved to `ff411f9` after the refusals,
   before any rater read a scored packet.
3. **Claude Code's version in practice.** Three practice runs were on
   2.1.325 and one, rater 2's second batch, on 2.1.328; every notes run and
   scored run was on 2.1.328.
4. **Rater 2's late entry.** Rater 2's second run of its first practice
   batch finished on 2026-11-20 and was entered on 2026-11-26, because two
   sessions each waited for a notice that never came; the record's hash was
   unchanged.
5. **A copy of the practice run.** The practice was scored on a copy of the
   practice run of 2026-10-16, so that run stays as it was.
6. **Helper scripts outside the harness.** The harness has no command that
   builds a notes prompt, the accept-or-reject prompt or the notes file, so
   three short scripts did; each notes prompt held only the 3 packets the
   raters scored differently, where rule 7 leaves out alike packets only as
   far as needed to fit.
7. **Labels.** The combined list numbered its notes C1 to C20, which reuses
   the rubric's names for criteria 1 to 5; the notes file carries no
   labels, so no scored prompt has the clash.
8. **The number of batches.** The raters' sessions were told to expect
   about 16 scored batches each, and `rater-batches` wrote 18.

## 5. Who read what, and when

Part 2 §2 asks the result to say this. The coordinating session kept the
record as it went; the entries before 2026-11-20 are as the deviation and
the sessions themselves gave them.

**Before the seal opened, no session opened a scored packet, a scored
prompt, a scored answer, a score sheet, the seal, the judge's answers, a
transcript, the packets file or the run's report.** Only each rater's fresh
runs read scored packets, one prompt each. Where a command's progress lines
named packet IDs, the coordinating session hid them before they were shown.

| When | Who | What it read or did |
|---|---|---|
| 2026-11-05 and 06 | The harness | Held the scored run, drew its packets and took the judge's scores, without anyone reading a packet |
| To 2026-11-10 | The session that built the harness | Opened nothing in the scored run's folder |
| 2026-11-10 | A planning session | Listed the names, sizes and dates of the files in the experiment's folder, the scored run's folder and the scored window's five folders; opened none |
| 2026-11-20, 21:35 to 21:50 | The coordinating session | Listed names, sizes and dates in the experiment's folders, the scored window's `raters` folder included: two packet files, no sheet. Opened none. Made the raters' empty folders and the copy of the practice run. Tallied, over other sessions' run records on the same machine, the kinds of text Claude Code adds; printed no message |
| 2026-11-20, 22:06 | The coordinating session | On the maintainer's word, started both raters' sessions |
| 2026-11-20, 22:15 to 22:55 | The coordinating session | Checked the two refused practice records, printing only pass or refusal, the set-up and tool names; opened #1140 |
| 2026-11-20, 22:30 to 22:45 | The session that reviewed #1140, on `claude-fable-5-2` | Tallied 217 run records by kind, version and tool name; checked the two refused practice records the same way; listed names and sizes in the two raters' folders. Nothing in the scored run |
| 2026-11-20, 23:26 to 23:29 | The maintainer, then rater 1 | #1140 merged. Rater 1 entered both practice batches. Then nothing happened until 2026-11-26 |
| 2026-11-21, Sydney time | A planning session | Listed branch and worktree names and searched other sessions' transcripts for two phrases, reading the matching snippets only. One quoted a line saying the scored run was complete. Opened no file in the experiment's folders |
| 2026-11-26, 11:31 | The maintainer's "next steps" session | Listed names and dates in the experiment's folder, read the last four lines of the scored run's log and the coordinating session's last 12 messages, and passed on the maintainer's word to resume |
| 2026-11-26, 11:34 to 11:35 | The coordinating session | Listed names, sizes and dates again: no sheet, seal shut. Ran `score` without `--open`, which wrote both blank sheets; its packet IDs were hidden |
| 2026-11-26, 11:35 to 11:43 | Rater 2, both raters, the coordinating session | The rest of the practice was entered and both notes runs kept. The coordinating session read the four kept notes answers, which are practice material, and wrote the combined list and the notes file |
| 2026-11-26, 11:43 to 11:44 | Both raters, the coordinating session | Each rater wrote its 18 scored prompts with `rater-batches` and opened none. `rater-same-notes` passed |
| 2026-11-26, 11:46 to 12:17 | Both raters' runs | Scored every packet: 18 runs each, each reading one prompt |
| 2026-11-26, 12:16 to 12:22 | The coordinating session | Read both scored `runs.jsonl` files for counts, batch names, times and set-up, not packet IDs. Ran `rater-audit`, `score` without `--open` and `rater-ready`, with ID and cell lists hidden. Wrote its report and asked the maintainer for the word |
| 2026-11-26, 19:41 and 19:45 | The "next steps" session | Listed file names and dates in the scored run's and the raters' folders, and read the coordinating session's report and last 14 messages. Opened no packet, prompt, answer, sheet, record, judge file or seal. Passed on the maintainer's word to open |
| 2026-11-26, 19:45 to 19:48 | The coordinating session, the maintainer | The session held the seal until the maintainer typed the word in that session itself. At 19:48:29 it ran `score --open`. It read the printed result and gave it to the maintainer. It told the "next steps" session only that the seal was open and on time |
| 2026-11-27 | The session that wrote this result, on `claude-fable-5-2` | With the seal open: read the result files, the run's report, the raters' logs and notes and the coordinating session's record; searched the transcripts and the raters' published records for failures and for private details; scored a copy of the run again as a cross-check |

This record is the sessions' own. It holds no entry of the maintainer
opening a file of the scored run before the seal opened. From 2026-11-26 at
19:48:29, nothing in the run is closed.

One time differs between two records. The coordinating session's notes give
22:25 for the two practice refusals; each rater's log gives 22:16:58 and
22:17:03. This document follows the logs.

## 6. What is published, and what is held back

The raters' prompts, answers, public records and logs are in
[`runs/ai-raters/`](../../evaluators/experiments/EXP-001/runs/README.md),
as rule 6 lists them. As that rule says, the published files still name the
home folder: in each brief's path, and in `runs.jsonl` each record's path
and the path and hash of the maintainer's own instructions file. A search of
them found no other private detail.

Each prompt and each public record was checked against the SHA-256 its
`runs.jsonl` line holds, for all 46 runs. All match.

The public records of the two refused practice runs are held back until the
maintainer has read them, as rule 6 asks. The maintainer keeps every run's
full record, unpublished; `runs.jsonl` holds each one's SHA-256.

## Related documentation

- [EXP-001 result](EXP-001-result.md) — the rule, the figures and what is
  published.
- [Deviation: two AI raters](EXP-001-deviation-ai-raters.md) — the rater
  rules this part reports against.
- [The harness, third part](EXP-001-harness-scoring.md) — the AI raters'
  commands.
- [EXP-001 pre-registration, part 2](EXP-001-preregistration-scoring.md) —
  raters and blinding as registered.
