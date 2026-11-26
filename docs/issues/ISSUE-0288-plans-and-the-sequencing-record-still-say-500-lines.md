---
id: ISSUE-0288
summary: "The size check warns about a code file over 700 lines since the fix of ISSUE-0231, and no sequencing amendment records that move. Ruling (e)'s own text, the v0.4.0 plan, the file-size tables of five PR plans whose PRs are not written yet, and one code comment still give 500 as the mark and read as current. None of them could be edited by that fix."
status: open
severity: low
area: docs
created: 2026-11-26
refs:
  - docs/v0.3.x-sequencing.md
  - docs/v0.4.x-sequencing.md
  - docs/v0.4.x-sequencing-2026-10-21.md
  - docs/v0.4.0-plan.md
  - docs/rfcs/0012-pr-plan.md
  - docs/rfcs/0009-phases-3-4-pr-plan.md
  - docs/rfcs/0021-phase2-pr-plan.md
  - docs/rfcs/0041-pr-plan.md
  - docs/rfcs/0042-pr-plan.md
  - docs/rfcs/0028-pr-plan.md
  - agents/channel_receive.py
  - docs/methodology/decisions.md
  - docs/documentation-guide.md
  - scripts/checks/file_size.py
  - docs/issues/ISSUE-0231-size-check-counts-comment-lines.md
  - https://github.com/mkhomutov/Persatrix/pull/1146
---

# ISSUE-0288: Plans and the sequencing record still say 500 lines

## Summary

[#1146](https://github.com/mkhomutov/Persatrix/pull/1146) moved the size
check's warning for a code file from 500 lines to 700, on the maintainer's
decision of 2026-11-21
([ISSUE-0231](ISSUE-0231-size-check-counts-comment-lines.md)). It changed
every statement of the number in the standing texts. It was not allowed
to edit a plan, a sequencing document or an RFC, and it left code
comments alone. So some texts that a reader takes as current still give
500 as the mark. This issue is their open home.

## Context

A ruling holds until a later amendment says otherwise
([decisions](../methodology/decisions.md#sequencing-amendments), rule 5).
The warning at 500 lines is part of ruling (e) of the sequencing Amendment
2026-09-12. The move to 700 was decided ahead of an amendment, and no
amendment records it yet. The
[documentation guide](../documentation-guide.md#size-limits) carries a
dated entry in the meantime.

| Place | What it says | What is so |
|---|---|---|
| `docs/v0.3.x-sequencing.md`, ruling (e) | "a warning at 500 lines and a hard fail at 800" | The warning starts over 700 lines. The failure is unchanged. |
| `docs/v0.4.x-sequencing.md` and `docs/v0.4.x-sequencing-2026-10-21.md`, the lines that carry rulings (b) to (e) over | Ruling (e) holds as written | It holds except for the warning's number. |
| `docs/v0.4.0-plan.md`, Risks | "Hot files near 500 lines", to be split "at a real seam when a PR edits one (ruling e)" | No check says anything about a file near 500 lines. The rule asks for a split only over 700. |
| `docs/rfcs/0012-pr-plan.md`, File-size constraints and its PR 10 section | PRs 5 and 10 split a file "at a real seam first" at 499 or 500 lines; PR 8 splits first "if that would cross 500" | The check does not warn until a file is over 700 lines, so the rule no longer asks these PRs to split. |
| `docs/rfcs/0009-phases-3-4-pr-plan.md`, File-size constraints and its PR 14 section | PR 14 splits `agents/server_servicers.py` first, at 502 lines; "the next PR to add a line" splits `agents/server.py`, at 500 | The same. `agents/server_servicers.py` is 393 lines today. |
| `docs/rfcs/0021-phase2-pr-plan.md`, File-size constraints and its PR 4 and PR 5 sections | PR 4 splits `memory_context.py` first, at 500 lines; PR 5 "adds one line, or splits first" | The same. |
| `docs/rfcs/0041-pr-plan.md` and `docs/rfcs/0042-pr-plan.md`, File-size constraints | "cap = 500"; RFC 0041's plan makes an extraction a mandatory first step for a file "at 500/500" | These two were out of date before the fix of ISSUE-0231: a code file has failed only over 800 lines since [#967](https://github.com/mkhomutov/Persatrix/pull/967). `agents/base.py` is 412 lines today. |
| `agents/channel_receive.py`, the module docstring | "the 500 lines at which `scripts/checks/file_size.py` warns" | Present tense, and false since that fix: the check warns over 700. Of the 393 places in code that name the figure, it is the only one that describes the check as it is now; the others say why a file was cut on its day. |

Read and not listed: the file-size table of `docs/rfcs/0028-pr-plan.md`
has nine rows, and none tells a PR to split first or names the figure.
The plans of RFC 0029 Phase 3, 0044 Phase 1, 0051, 0052 and 0053 have
such a section too; their work is shipped, so they are records.

## Impact

A session that writes one of those PRs from its plan splits a file the
rule no longer asks it to split, at a seam chosen by line count, which is
what ISSUE-0231 set out to stop. A reader who compares the sequencing
record with the checker finds two numbers. Nothing fails, and v0.4.0 is
paused, so no PR starts from these plans before the strategy review.
Proposed low, as the tracker's other findings where a text lags the code
and nothing breaks. The severity is proposed here, not ruled.

## Proposed fix / investigation path

- The next sequencing amendment carries a line for the new figure, or
  the maintainer rules that these two numbers move by another route and
  the methodology says so.
- The v0.4.0 plan's risk row is corrected when the plan is next edited.
- Each PR plan's table is corrected by the first PR that reopens that
  plan: the row says whether the PR still splits the file, and why, with
  no line count as the reason.
- The docstring of `agents/channel_receive.py` is corrected by the first
  change that edits that file for another reason.

This issue closes when the amendment line exists and no row above is
left.

## Notes

> 2026-11-26 — filed by the review of
> [#1146](https://github.com/mkhomutov/Persatrix/pull/1146), which could
> not edit any of these places. Each was read on `main` at `c409027b`.
