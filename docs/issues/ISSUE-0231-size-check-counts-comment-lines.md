---
id: ISSUE-0231
summary: "The file-length check counts every line, and about half the lines in the Go and Python production code are comments, so the 500-line mark is really about 250 lines of code. File lengths pile up at the mark: 31 code files sit at 499 or 500 lines and only 6 are over, although the failing limit is now 800. Most of that pile is older than the relaxed limit."
status: resolved
severity: low
area: scripts/checks
created: 2026-11-10
closed: 2026-11-26
closed_pr: 1146
refs:
  - scripts/checks/file_size.py
  - tests/unit/python/test_file_size_code_warning.py
  - docs/documentation-guide.md
  - docs/methodology/release-cycle.md
  - docs/methodology/process-glossary.md
  - docs/methodology/review-process.md
  - CLAUDE.md
  - docs/issues/ISSUE-0288-plans-and-the-sequencing-record-still-say-500-lines.md
  - docs/issues/ISSUE-0143-debt-sweep-26-files-at-size-cap.md
  - docs/issues/ISSUE-0229-channel-router-holds-every-governance-state.md
  - docs/issues/ISSUE-0230-persona-class-is-one-class-cut-into-mixins.md
---

# ISSUE-0231: The size check counts comment lines, and comments are half the code

## Summary

`scripts/checks/file_size.py` measures a code file with
`len(text.splitlines())`: code, comments, docstrings and blank lines
alike. Comments and docstrings are about half of the non-blank lines in
the production code, so a file reaches the 500-line warning with roughly
250 lines of code in it.

## Context

Measured over the 1,757 tracked Go, Python, Rust, JavaScript and Svelte
files, with the generated gRPC stubs left out (the check also scans
style sheets, YAML and TOML):

| Length | Files |
|---|---|
| exactly 500 | 19 |
| exactly 499 | 12 |
| 480 to 500 | 104 |
| over 500 | 6 (the longest is 578) |

Share of non-blank lines that are comments or docstrings: Python
production code 47%, Go production code 47%, Rust about 26%, and the web
console 20% with its tests or 27% without. The Python figure covers the 277 files under `agents/` outside tests
and generated stubs.

The release cycle already records that "a seam chosen by line count
refills the pile"
([ISSUE-0143](ISSUE-0143-debt-sweep-26-files-at-size-cap.md)), and the
rule was relaxed on 2026-09-12 to a warning at 500 and a failure at 800.
Of the 31 files at 499 or 500 lines, 23 were already there that day and
15 have not been touched since; 8 reached the mark after it. What is new
here is the reason the mark bites so early.

Signs that the mark, not the code, decides where files end:

- About half of the Python production files and a third of the Go ones
  have a comment that explains their layout by the size limit.
- Many Python modules re-export names from the sibling they were split
  from. `agents/memory/interactions.py` defines nothing of its own.
- The two largest objects were split into many files without being
  taken apart
  ([ISSUE-0229](ISSUE-0229-channel-router-holds-every-governance-state.md),
  [ISSUE-0230](ISSUE-0230-persona-class-is-one-class-cut-into-mixins.md)).

## Impact

Comments, which are a strength of this code base, count against the
file that carries them. The cheapest ways to get under the mark are to
cut a file at an arbitrary line or to trim the explanation, and the rule
forbids the second. Nothing fails below 800 lines, so today the cost is
a warning and the habit it keeps alive; that is why this is rated low.

## Proposed fix / investigation path

Count code lines only (not comments, docstrings or blank lines) for the
500-line warning, or raise the warning to match what 500 lines was meant
to allow. Judge a split by whether the new file can be described without
naming the file it came from. This changes a recorded methodology
ruling, so it needs the maintainer's decision.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; the length counts were measured twice. The Python comment
> share first read 52%; a recount in review, with comments taken from
> the tokenizer and docstrings from the syntax tree, gives 47%.

> 2026-11-26 — resolved by [#1146](https://github.com/mkhomutov/Persatrix/pull/1146),
> on the maintainer's two choices of 2026-11-21.
>
> Measured again first, on `main` at `c409027b`. Of 1,842 tracked Go,
> Python, Rust, JavaScript and Svelte files (generated stubs left out),
> 17 are exactly 500 lines long, 11 are 499, 108 are between 480 and
> 500, 5 are over 500 (the longest is still 578), and none is over 700.
> Comments and docstrings are 47.6% of the non-blank lines of the 283
> Python production files under `agents/`, and comments are 47.1% of
> those of the 249 Go production files under `internal/` and `cmd/`. The
> counts moved a little with sixteen days of merges; the finding stands.
>
> **The warning moves from 500 lines to 700.** The check still counts
> every line, and a code file still fails only over 800. Counting code
> lines only was considered and not taken: it needs a comment parser for
> each of five languages, and a miscount would fail CI. With the warning
> at 700 the check lists no code file today, where it listed 5.
>
> **The split rule is written down**: a split is good when the new file
> can be described without naming the file it came from. It stands
> beside "split at a real seam when a change edits the file for another
> reason, never in a sweep" in the documentation guide's Size Limits, in
> `CLAUDE.md` and in the review process, and the process glossary gains
> the term Real seam.
>
> Where the decision is recorded: a dated entry in the documentation
> guide's Size Limits, next to the sentence on ruling (e) of the
> sequencing Amendment 2026-09-12, and this note. No sequencing amendment
> records the move yet, so ruling (e)'s own text still says 500. Every
> current statement of the two numbers in the standing texts was changed.
> A unit test holds a list of those sentences and fails when a listed
> sentence stops giving the checker's numbers. It does not catch a
> statement that is added, in a listed file or anywhere else.
>
> What was left as it is. No code file was split or joined. On `main`,
> 393 places in 354 code files name the 500-line figure as the reason for
> a file's layout, counted with line breaks folded, so that a figure at
> the end of one comment line and "lines" at the start of the next count.
> They say why a file was cut on the day it was cut, and they stay. One
> of them is different: the module docstring of
> `agents/channel_receive.py` says, in the present tense, that the check
> warns at 500 lines, which is false once this is merged. It was not
> edited either. The 28 files at 499 or 500 lines stay as they are. The
> check no longer warns when one of them grows, but PR plans in progress
> still tell their PRs to split some of them first.
>
> Plans, sequencing documents, RFCs, release records, the strategy review
> and the changelog's earlier entries still say 500, as records of their
> day. Some of them are read as current and were not edited, because the
> fix edits no plan, sequencing document or RFC: ruling (e)'s own text and
> the later amendments' line that rulings (b) to (e) hold, the v0.4.0
> plan's risk row on "hot files near 500 lines", and the file-size tables
> of PR plans whose PRs are not written yet.
> [ISSUE-0288](ISSUE-0288-plans-and-the-sequencing-record-still-say-500-lines.md)
> lists them with the present-tense comment and stays open until each is
> corrected. A ruling holds until a later amendment says otherwise
> ([decisions](../methodology/decisions.md#sequencing-amendments), rule 5),
> so a line for the new figure in the next amendment is the maintainer's
> to add.
