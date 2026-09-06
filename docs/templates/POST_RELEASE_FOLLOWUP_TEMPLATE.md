# Post-release follow-up — PR body template

Title: `docs(release): vX.Y.Z post-release follow-up — Phase-4 backfills + next-milestone pointers`

> Guidance: this is a PR body, not a document. Open it after the tag is pushed
> and the GitHub Release is published. Nothing in the tree may still say the
> release is pending when it merges. Anything the plan promised for Phase 4
> that cannot be done is recorded, not forced.

---

vX.Y.Z tagged at `<sha>` and the GitHub Release published YYYY-MM-DD
(<n> assets). This backfills the post-tag half PR 4 deliberately left open.

## Statuses → Released, with the tag link

- [ ] README roadmap row
- [ ] ROADMAP: Version Map row, `Last updated` (concise), Current phase / milestone
- [ ] `docs/vX.Y.Z-release-checklist.md` status line + §5 boxes + §7 rows
- [ ] `docs/vX.Y.Z-release-prep-plan.md` status + Progress Overview
- [ ] `docs/vX.Y.Z-plan.md` release-prep row + Decision / next steps
- [ ] `docs/manual-tests/README.md` execution-report row

## Forward pointer

- [ ] ROADMAP Current phase → **the next ratified version** (per `docs/v0.3.x-sequencing.md`
      §Amendment YYYY-MM-DD), not the next major train if a version sits in between
- [ ] README "what's next" line, if it has one

## Closures reflected

- [ ] Every issue this release closed carries `status: resolved`, `closed`, `closed_pr`
- [ ] `make issues` / `make rfcs` clean
- [ ] RFC phases shipped this release recorded in the ROADMAP RFC Master Index

## Backfills

- [ ] `scripts/checks/file_size_allowlist.py`: any entry for this cycle's plan or release-prep plan retired (the checker now treats them as frozen evidence — `[STALE-ALLOWLIST]` names any left)
- [ ] Execution-report index row, if PR 1 did not add it
- [ ] New issues the release surfaced: <IDs>, each with a dated note and a slot

## NOT done, and recorded rather than forced

<Any Phase 4 obligation that cannot be met as written, with the reason, so the
next cycle does not inherit a promise that cannot be kept. Delete this section
if empty.>

## Gates

pre-commit 9/9 · doc_audit 0/0 · `file_size --strict` clean · `make issues-check rfcs-check` clean
