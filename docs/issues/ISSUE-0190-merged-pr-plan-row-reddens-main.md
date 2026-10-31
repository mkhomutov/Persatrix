---
id: ISSUE-0190
summary: "A PR that a plan tracks still has its own progress row at 🔀 PR open when it squash-merges, since it cannot say merged before it has. The plan-status check then fails Docs hygiene, a required check, on main and on every open PR until another PR flips the row. That happened after each of v0.4.0's nine implementation merges so far, and two of the flips were written twice by sessions racing for the same row. The merged-PR history check tolerates the same lag and its hook catches the file up; the plan-status check has neither."
status: open
severity: medium
area: scripts/checks
created: 2026-10-27
refs:
  - scripts/checks/plan_status.py
  - scripts/merged_prs.py
  - .github/workflows/ci.yml
  - docs/methodology/release-cycle.md
  - docs/development-workflow.md
  - docs/rfcs/0029-phase3-pr-plan.md
  - docs/rfcs/0009-phases-3-4-pr-plan.md
  - docs/rfcs/0028-pr-plan.md
  - https://github.com/mkhomutov/Persatrix/pull/1065
---

# ISSUE-0190: A merged PR's own plan row turns main red until another PR flips it

## Summary

When a PR that a plan tracks merges, its row in the plan's progress table
still says 🔀 (PR open): a PR cannot write ✅ and a merge date before it has
merged. The plan-status check then fails `main`, and every open PR with it,
until some later PR flips the row. Nothing makes that flip on its own, so
each merge needs a follow-up PR, and two sessions sometimes write the same
one.

## Context

The release cycle says the plan row "flips at PR open and at merge"
([Phase 1](../methodology/release-cycle.md#phase-1--implementation-prs)).
The flip at merge has no commit to ride on: a squash merge lands only the
PR's own diff.

`scripts/checks/plan_status.py` reads every open-cycle plan and every RFC
and issue PR plan. A 🔀 row is stale once every PR it links is a
squash-merge subject on `main`'s first-parent log, and a 🔀 row that links
nothing is judged by the squash merge that wrote it (#910). So a PR's own
row passes while the PR is open and is stale from the moment it merges. The
check runs in the Docs hygiene job on every push to `main` and on every
PR's merge ref, and Docs hygiene is one of `main`'s twelve required checks,
with branches required to be up to date.

`scripts/merged_prs.py` met the same problem with `docs/merged-prs.md`,
which can never hold the merge that lands it. Its `--check` passes while
the committed rows trail the log by the newest merges only, and the
pre-commit hook catches the file up on the next commit anywhere. The
plan-status check has neither half: no tolerance, and no step that writes
the flip.

What it cost in v0.4.0, after every implementation PR so far. `main` was
green before #1053. Times are the squash merges' local times (+1100).

| Red from | Merged | Green again | What flipped it |
|----------|--------|-------------|-----------------|
| 2026-10-23 21:23 | #1053 (RFC 0021 Ph 2 PR 1) | 2026-10-24 17:10 | #1054 carried #1053's flip but left its own row at 🔀; #1058 flipped that. #1056 wrote the same flip and, merged 19 minutes later, landed only its merged-PR history row |
| 2026-10-24 20:50 | #1057 (RFC 0029 Ph 3 PR 1) | 21:37 | #1059 |
| 2026-10-25 20:12 | #1060 (RFC 0029 Ph 3 PR 2) | 22:14 | #1061 carried the flip so its own Docs hygiene would pass; #1062, a flip PR of its own, was closed |
| 2026-10-27 17:16 | #1063 (RFC 0029 Ph 3 PR 3) | 20:23 | #1065 |
| 2026-10-28 07:02 | #1067 (RFC 0029 Ph 3 PR 4) | 21:47 | #1068 |
| 2026-10-29 21:30 | #1069 (RFC 0009 Ph 3 PR 3) | 21:59 | #1070 |
| 2026-10-30 20:38 | #1071 (RFC 0009 Ph 4 PR 5) | 21:29 | #1073 |
| 2026-10-31 20:16 | #1072 (RFC 0028 PR 2) | when #1074 merges | #1074 |

Each red run failed the same step, "Check no plan row announces an
already-merged PR as open", on the merged PR's own row.

## Impact

- While `main` is red, a PR that does not carry the flip cannot merge short
  of an admin override: its merge ref holds `main`'s stale row, so its
  required Docs hygiene fails.
- Every merge costs a follow-up PR, or a flip carried in an unrelated one,
  and someone has to notice the red check first.
- Sessions race for the flip: #1056 and #1058 wrote the same row, and #1062
  was closed when #1061 carried it.
- A red Docs hygiene becomes routine, so a real failure in the same job is
  easier to miss.

## Proposed fix / investigation path

Three ways, which can be combined; the choice is for whoever slots this.

1. **The PR flips its own row before it merges.** Its last commit writes ✅,
   the link and the expected date. No tooling, but the date is a guess: a
   merge that slips past midnight is a day off, and the check reads only
   the marker, so a wrong date goes through.
2. **The check tolerates the lag**, as `merged_prs.py --check` does: a row
   whose PR is the newest squash merge passes. Alone this only moves the
   failure to the next merge, so it needs a writer as well.
3. **A writer makes the flip.** `plan_status.py --fix` would flip a stale
   row to ✅ with its PR's link and the date `docs/merged-prs.md` gives it,
   and the pre-commit hook would run it as it runs `merged_prs.py`, so the
   next commit anywhere carries the flip. A workflow on push to `main` that
   opens the flip PR is the other writer, but it would need write access,
   which the CI workflow does not grant.

## Notes

> 2026-10-27 — filed from PR 1065's review (F-12). That PR flips RFC 0029
> Phase 3 PR 3's row after #1063 merged, and the four flips before it are
> in the table above.
