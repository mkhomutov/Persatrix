---
id: ISSUE-0149
summary: "The pre-commit hook rebuilds and stages docs/merged-prs.md on every commit, so nearly every PR carries catch-up rows for other PRs, and two PRs that did this at different points on `main` conflict on the file (#896 and #897 conflict on it alone) — although `merged_prs.py --check` passes without those rows; proposes updating the file only in the release sweep"
status: open
severity: low
area: scripts
created: 2026-09-10
refs:
  - scripts/pre_commit.py
  - scripts/merged_prs.py
  - docs/merged-prs.md
  - docs/methodology/automation-catalogue.md
  - docs/methodology/branch-protection.json
  - ROADMAP.md
---

## Summary

The pre-commit hook rebuilds `docs/merged-prs.md` and stages it on every
commit. The file can never list the PR that carries it, so almost every PR
adds catch-up rows for the PRs merged since it branched. Two open PRs that did
this at different points on `main` then conflict on the file, and because
branch protection requires an up-to-date branch, each needs a manual rebase.
Found in the review of PR #903 (finding F-15).

## Context

- **The hook.** `_GENERATED` in `scripts/pre_commit.py` lists
  `docs/merged-prs.md`. On every commit, `_regenerate` runs
  `scripts/merged_prs.py` and then `git add`s the output.
- **The check does not need the rows.** `scripts/merged_prs.py --check`
  passes when the committed rows are a suffix of the rows it generates from
  the log, so a file that lags behind `main` still passes. With #903's hunk
  reverted, `--check` stays green on the branch, on the CI merge ref, and on
  `main` after the merge.
- **The cost.** On 2026-09-10, #896 and #897 showed as conflicting on GitHub,
  and a test merge against `main` conflicted only in this file. Each carried a
  hook hunk (count 875 → 876 plus a #894 row) that collides with the rows
  `main` gained later. `docs/methodology/branch-protection.json` sets
  `"strict": true`, and GitHub's update-branch button cannot resolve a
  content conflict.

## Impact

Any open PR that is not updated before a sibling merges picks up a conflict in
a file nobody edited by hand. Each fix is small but it repeats, and a
hand-resolved conflict that keeps rows from both sides breaks the suffix rule
and fails `--check`.

## Proposed fix

Remove the `merged prs` entry from `_GENERATED`, and bring the file up to date
once per release sweep (or from a scheduled job). Update the places that
describe today's behaviour: its row in
`docs/methodology/automation-catalogue.md`, the `scripts/merged_prs.py`
docstring, and the ROADMAP.md section "How to Update This File" ("regenerates
itself on the next commit"). The trade-off: the file lags `main` between
sweeps, which `--check` already allows.

## Notes

> 2026-09-10 — filed from the PR #903 review (F-15). #903 itself merges
> cleanly; its hunk follows the documented behaviour.
