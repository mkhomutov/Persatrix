---
id: ISSUE-0231
summary: "The file-length check counts every line, and about half the lines in the Go and Python production code are comments, so the 500-line mark is really about 250 lines of code. File lengths pile up at the mark: 31 code files sit at 499 or 500 lines and only 6 are over, although the failing limit is now 800. Most of that pile is older than the relaxed limit."
status: open
severity: low
area: scripts/checks
created: 2026-11-10
refs:
  - scripts/checks/file_size.py
  - docs/documentation-guide.md
  - docs/methodology/release-cycle.md
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
