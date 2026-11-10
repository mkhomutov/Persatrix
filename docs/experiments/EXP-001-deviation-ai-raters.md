# EXP-001 — Deviation: two AI raters in the two people's place

> **Status**: 🚧 **In Progress** — fixed when it merges ([#1095](https://github.com/mkhomutov/Persatrix/pull/1095)), before any rater reads a packet from the scored run.
> **Last updated**: 2026-11-10
> **Departs from**: [pre-registration part 2, §2](EXP-001-preregistration-scoring.md#2-raters-and-blinding) · **Decided by**: the maintainer, on 2026-11-10

Part 2 of EXP-001's pre-registration has three raters score every memo and
every recall answer: two people and the
[LLM judge](../methodology/process-glossary.md#llm-judge). On 2026-11-10 the
maintainer decided that two
[AI raters](../methodology/process-glossary.md#ai-rater) score in the two
people's place, and that the result says so in its first lines. This
document records that change before any rater reads a packet from the
scored run. It says what the result may claim because of it, where its
limit lies, and the rules the two AI raters follow.

## 1. What changed, and who decided

The maintainer decided it on 2026-11-10, in their own planning notes, and
confirmed it the same day in the session that scores the run. Their reason:
they trust AI raters more.

**What is replaced.** Part 2 §2's two people. Rater 1 fills person-1's score
sheet from person-1's packets, and rater 2 fills person-2's from
person-2's, so the harness reads both sheets as it always would. Each
rater still works alone, in its own packet order.

**What stays.** Everything else in part 2:

- the rubric and its anchors;
- three raters, averaged: the two AI raters and the judge, which already
  scored every packet as registered;
- the agreement measure and its 0.4 floor;
- the 21 days for scoring, which end on 2026-11-27 at 18:12 UTC;
- the checks and the rules of §5.

Blinding stays too. The packets carry random IDs and no adviser names, and
the map from ID to arm stays sealed until every score is in. Part 1, the
materials and the caps are untouched.

**What the harness gains.** The harness gains the AI raters' commands
after the first scored meeting, which part 1 §5 would not allow for a
change. They add steps for the raters and change nothing the harness did
before them: the run, its packets, the seal and the judge's scores stay as
they were. The result lists them with this deviation. The harness's own
result, in plain words and as figures, names this deviation before any
rule. Its plain words also name the people-alone rule of §7 by the two
sheets it reads; the harness's own field for it keeps its name,
`people_alone`.

## 2. Why this is a deviation, not a change to part 2

[Part 1 §5](EXP-001-preregistration.md#5-changes) lets part 2 change only
through a strategy review and the amendment that ratifies it. It also says
that from the first scored meeting on, nothing changes: a problem found then
is reported in the result, not fixed. The scored run's first meeting was
held on 2026-11-05.

So part 2 stays as it was merged. This document names the departure from
it, before any rater reads a scored packet, and the result reports it
first.

## 3. What the result may claim

- **No rule fires as registered.** Part 2 asks for two people, so the rule
  the scores pick is not the pre-registered decision. The result reports
  the rule the AI-scored figures fire, as what those figures show.
- **Its first lines say so.** They say that two AI raters replaced the two
  people, that the maintainer decided it on 2026-11-10, and they link here.
- **§7's people-alone rule changes.** "The rule that would fire on the two
  people's scores alone" becomes the rule on the two AI raters' scores
  alone.
- **Incomplete and inconclusive mean what they meant, and §6 still binds
  them.** An agreement below 0.4, scoring that finishes late, or a judge
  stopped by its cap still makes the result inconclusive. Then part 2 §6
  applies as written: the first strategy review may only order one repeat
  of the whole run, under the same documents and this deviation.
- **What the rule is worth is fixed now, not after.** If a rule fires on
  the AI-scored figures, the first strategy review after the result treats
  it as it would the registered rule, with this deviation and its limit
  (§4) stated beside it. The weight it gives does not depend on which rule
  fired. The maintainer decided this on 2026-11-10, before any rater read
  a scored packet. Any decision it changes is ratified by a sequencing
  amendment, as usual.
- **Who read what, and when.** Part 2 §2 asks the result to say this, and it
  now covers both AI raters' runs and the sessions that brief them (§6).

## 4. The limit

The arms (`claude-sonnet-4-6`), the judge (`claude-opus-5`) and both AI
raters (Fable 5.2) are all Claude models from one provider, and both AI
raters run the same model. Models from one family can share blind spots and
agree for the same wrong reason. So the three raters' agreement is not an
independent check, and a high agreement figure says less than two people's
would.

All five arms run one model, so a rater's leaning towards its own family
is unlikely to favour one arm over another. It can still favour, or mark
down, every arm alike.

Part 2 §2 asks that at least one person did not run the harness. No AI
rater runs it, but the sessions that brief them run its `rater-` commands.

## 5. The rater rules

These rules are fixed when this document merges, before the seal opens. The
harness's [`ai_raters`](../../evaluators/exp001/ai_raters.py) commands hold
each rater's session to them,
[`ai_rater_runs`](../../evaluators/exp001/ai_rater_runs.py) checks each
run's record, and
[`ai_rater_checks`](../../evaluators/exp001/ai_rater_checks.py) holds the
checks that come before the seal opens.

1. **The model.** Both raters run on Fable 5.2, `claude-fable-5-2`, the
   maintainer's choice. It is neither the judge's model nor the arms'. The
   harness holds that ID, so no session can name another. A run is given
   no model of its own, so it runs on its session's. Its record must show
   replies from that model alone, and its answer must name it on its first
   line. `rater-enter` refuses any other, and the result records the ID.
   Both sessions use one reasoning effort and leave it alone. Each run's
   log line records its effort, and a run whose effort is not that of the
   first run in its folder is refused.
2. **One session per rater.** Each rater has its own Claude Code session on
   that model, which briefs that rater and no other:
   - `rater-batches` writes the rater's packets as prompts;
   - the session gives each prompt to a fresh run;
   - `rater-enter` enters each answer in the rater's sheet.

   Each session runs from an empty folder of its own outside any
   repository, such as `~/exp001/ai-raters/person-1`, and saves no
   memory there. Its runs are then given no project instructions, no
   memory index and no git status. They are given what Claude Code gives
   every run on this machine:
   - its own system prompt for the agent, the folder and the date;
   - the names of the tools, skills and agents installed, and the
     connectors' instructions;
   - the maintainer's own instructions file.

   None of these says anything of EXP-001. `rater-enter` refuses a run
   whose copy of the maintainer's own file is not that of the first run in
   its folder. The session runs the harness's commands from a checkout of
   the commit that merges this document, and each log line records the
   commit. The session never opens a packet file to read it; the commands
   do. The session that built the harness briefs neither rater, and never
   reads a packet from the scored run.
3. **A run.** A fresh run of the rater's model is a subagent of the
   rater's session: Claude Code's own general-purpose agent, which a run
   is when its session names no agent type. It is given one message, the
   brief `rater-brief` prints, and nothing else from its session. The
   brief tells it to read its prompt and do what it says. A prompt holds:
   - the rubric, as the people would have had it;
   - the notes agreed in practice (rule 7);
   - up to eight packets in the rater's own order, each followed by the
     cells to score. A prompt holds fewer when eight would make it longer
     than 50 000 characters or 1 800 lines, so one Read gives it whole.

   The run reads its prompt, calls no other tool, and hands its answer
   back. `rater-enter` takes the answer from the run's own record, which
   Claude Code keeps for every run. The record is entered from where
   Claude Code wrote it, unchanged, with the small file beside it that
   names the run's agent type. `rater-enter` refuses the record when it
   shows:
   - any message to the run but its brief, before, during or after the
     run;
   - any agent type but Claude Code's own general-purpose agent;
   - any instructions file but the maintainer's own, a memory index or a
     git status;
   - anything of a kind Claude Code is not known to give every run, such
     as a hook's added text or a recalled memory, whatever it holds;
   - a tool call other than one Read of the prompt, by its path alone, a
     Read that did not give the whole prompt, or a reply that holds
     anything but its words and those calls.

   That run is discarded and held again whole.
4. **Off limits.** Neither a run nor a rater's session reads, from the
   scored run:
   - the seal, `sealed/`;
   - the judge's answers and calls, `judging/`;
   - the meetings' transcripts, `pairs/`;
   - `packets.json`;
   - the run's report;
   - the other rater's packet file or sheet.

   The same holds in the practice run, so its seal and its judge's scores
   cannot shape the notes. There a session may read its own rater's
   packets, both raters' sheets, the differences between them and the
   combined list of notes, as rule 7 says. A run reads nothing but its
   prompt. The scored run's two sheets are never compared before the seal
   opens.
5. **Scores as given.** `rater-enter` refuses an answer that does not give
   exactly the cells asked for, with 0, 1 or 2 for a criterion and right or
   wrong for a recall question. It refuses one that could be read two
   ways, too: a cell or packet given twice, a second JSON block, or words
   after the block. It never changes a cell already scored, and enters
   nothing once the seal is open. A run that fails is held again whole,
   from the same prompt: refused, cut off, or calling another tool.
6. **Everything is kept.** `rater-enter` copies each run's record beside
   its prompt before checking it, with a copy to publish, and logs every
   run in `runs.jsonl`: entered, kept or refused, and why, with how the
   run was set up. A refusal made before the record is read, such as a
   changed prompt, is logged too, and nothing kept is ever written over.
   These stay outside the repository until every score is in, then are
   published with the result:
   - each prompt, and each entered or kept answer;
   - each run's record, a refused run's too, without what Claude Code
     adds around the messages, which names the account and holds the
     maintainer's own instructions and memory. The maintainer reads a
     refused run's copy before it is published, since it can hold
     whatever a wrong brief said;
   - each session's `runs.jsonl`, which holds the SHA-256 of every full
     record. The maintainer keeps the full records, unpublished.

   What is published still names the home folder, in each brief's path,
   and in `runs.jsonl` each record's path and the path and hash of the
   maintainer's own instructions file.

   Before the seal opens, each session runs `rater-audit` on every folder
   of prompts it used and its one folder of run records. A rater's session
   starts no other runs, so the audit lists any record there that no log
   names, whatever its first message was. It lists a logged record that
   has changed since or is gone, too. It must list none, and it refuses a
   folder with no records in it. The result lists every run held again,
   and why.
7. **Practice first.** As part 2 §2 has the people do, both raters first
   score the practice run's packets, the same way but with no notes:
   `rater-batches` refuses notes there, and requires them on a scored run.
   `rater-differences` lists the cells they scored differently.

   The practice is also the harness's first real use. If a run that
   followed these rules is refused there, the harness is corrected by a
   pull request the maintainer reviews, before any rater reads a scored
   packet, and the result reports it. Once the practice is scored,
   `rater-ready` on the practice run lists anything that differs between
   the two raters' set-ups, so it is put right before the scored run. Then:
   - each rater's session writes a notes prompt from practice files only:
     its practice prompts, both raters' practice sheets and those
     differences. A notes prompt keeps to the limits of rule 3, and
     `rater-brief` refuses one that does not. To fit, a session first
     leaves out the rubric each practice prompt repeats, then the packets
     the raters scored alike. The prompt asks for short notes on how the
     anchors apply, with the model line first;
   - the session that built the harness puts the two lists side by side,
     and each rater's session writes a second prompt that asks to accept
     or reject the combined list note by note;
   - each notes prompt goes to a fresh run with its brief, and
     `rater-keep` checks and keeps that run as `rater-enter` checks a
     scoring run.

   The session that built the harness writes the one notes file from the
   two kept answers: each note both accept, word for word, in the combined
   list's order. Both raters' sessions pass that file to `rater-batches`.
   Before either session briefs a scored run, `rater-same-notes` must pass
   on both folders of scored prompts. It compares the notes as the prompts
   hold them. The notes are published with the result. The practice scores
   are thrown away.
8. **Opening the seal.** The session that built the harness runs `score`,
   which writes the blank sheets and reports progress. Once every score is
   in and both sessions' audits list nothing, it runs `rater-ready` on the
   scored window and both raters' folders of prompts. That check passes
   only when every scored prompt holds the same notes and each sheet
   holds, in every cell, exactly what its entered runs gave. It also lists
   anything that differs between the runs' set-ups, such as their effort
   or Claude Code's version, and the result reports each line: by then no
   score can be given again. Then, after telling the maintainer, it opens
   the seal with `score --open`, by 2026-11-27 18:12 UTC.

## 6. Who read what, and when, so far

The result completes this record. What is known when this document merges:

- 2026-11-05 and 06: the scored run was held, its packets drawn and the
  judge's scores taken, by the harness without anyone reading a packet.
- To 2026-11-10: the session that built the harness opened nothing in the
  scored run's directory.
- 2026-11-10: a planning session listed the names, sizes and dates of the
  files in `~/exp001`, `~/exp001/scored` and the scored window's five
  folders. It opened none of them.

## Related documentation

- [EXP-001 pre-registration, part 2](EXP-001-preregistration-scoring.md) —
  scoring as registered, which this document departs from.
- [The harness, third part](EXP-001-harness-scoring.md) — scoring a run,
  and the AI raters' commands.
- [Process glossary](../methodology/process-glossary.md#ai-rater) — AI rater
  and LLM judge.
