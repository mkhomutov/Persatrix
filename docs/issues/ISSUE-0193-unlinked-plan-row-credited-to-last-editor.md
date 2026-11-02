---
id: ISSUE-0193
summary: "A plan row that says 🔀 and links no PR is credited to the last squash-merge that touched its line, by git blame. That is the row's own PR only if nobody has edited the line since, and the plan-row writer now acts on the answer: a later PR that corrects a typo in the row, or a plan PR that writes a row for someone else's open PR, gets its own number and date written into the row as a ✅, and nothing checks a ✅. Every one of the 44 such rows in the repository today was credited correctly."
status: open
severity: low
area: scripts/checks
created: 2026-11-01
refs:
  - scripts/checks/plan_status.py
  - scripts/checks/plan_row_flip.py
  - docs/issues/ISSUE-0190-merged-pr-plan-row-reddens-main.md
---

# ISSUE-0193: An unlinked plan row is credited to whoever edited it last

## Summary

A PR usually writes its own plan row before its number exists, so the row
links nothing. To find which PR the row belongs to, `pr_that_wrote` asks
`git blame --first-parent` who last wrote the line. That is a guess: the last
writer is the row's own PR only when nobody has touched the line since.

## Context

The guess is older than the writer, and used to be harmless. If it named the
wrong PR, the check failed a row that was fine, and a person looked at it.

Since [ISSUE-0190](ISSUE-0190-merged-pr-plan-row-reddens-main.md) the
pre-commit hook acts on the guess by itself, and the result is a ✅ row, which
nothing checks again. Two cases would go wrong:

- **A later edit.** PR A's unlinked row is still 🔀, and PR B fixes a typo in
  it. When PR B merges, the row is flipped with PR B's link and date.
- **A row for someone else's PR.** A plan PR writes a 🔀 row for a PR still
  open elsewhere, with no link. When the plan PR merges, the row is flipped as
  if the plan PR were the one it describes.

The first needs an edit to a row that is open for only hours. The second goes
against how rows are written: a row for another PR links it. The independent
review of the ISSUE-0190 fix counted the unlinked rows in the repository's
plans and found all 44 credited correctly, so this is a hole, not a live
defect.

## Options

1. Credit the commit that first added the line, not the last to touch it
   (`git log --diff-filter=A -L`-style). A plan PR's row is still credited
   wrongly, and the walk is slower than one blame.
2. Compare the row's Branch cell with the merged PR's head branch. This is the
   strongest evidence, but the head branch is not in the log, so it would need
   the network, which the hook does not use.
3. Have the writer print a line for every row it credits by blame, so that the
   diff reviewer sees the guess. This changes nothing by itself.

## Notes

> 2026-11-01 — filed from the review of the ISSUE-0190 fix (F-4).
