---
id: ISSUE-0139
summary: "Every master plan and release-prep plan since v0.3.0 sits in GRANDFATHERED_FILES with the exit condition 'remove once the release is archived', but nothing defines or performs archival: nine plan entries have accumulated and none was ever retired (#838 recorded the first attempt failing — dropping v0.3.14-plan.md would have turned the file-size gate red on a 4 112-word frozen record). Frozen released plans are release evidence, the same category file_size.py already excludes by pattern for execution reports and checklists; the fix is a pattern exclusion for plans whose version tag exists, so only the open cycle's plan needs an allowlist entry."
status: resolved
severity: low
area: ci
created: 2026-09-06
closed: 2026-09-06
closed_pr: 858
refs:
  - scripts/checks/file_size.py
  - scripts/checks/file_size_allowlist.py
  - docs/documentation-guide.md
  - docs/methodology/release-cycle.md
---

## Summary

Released version-cycle plans have no archival mechanism, so the allowlist
entries that promise to go away at archival never do.

## Context

`scripts/checks/file_size_allowlist.py` says master plans and release-prep
plans are "removable once the release is archived". The v0.3.14 post-release
follow-up ([#838](https://github.com/mkhomutov/Persatrix/pull/838)) tried to
honour that for `docs/v0.3.14-plan.md` and recorded why it could not: the
plan is 4 112 words against the 3 000 cap, so removing the entry fails
`file_size.py --strict`; the only alternatives were deleting ~1 100 words of
ratified contract from a historical record, or pattern-excluding master
plans — which `file_size.py`'s own comment rejects because plans are edited
during their cycle. The PR body notes "nothing implements archival: there is
no docs/archive/, and all NINE plan entries (v0.3.0 onward) carry the same
never-executed exit condition."

The same file already resolves this tension for two other write-once
categories: execution reports and release checklists are excluded by pattern
(`_EXTRA_EXCLUDES`) because "they are frozen against a tag and can never
shrink back under the cap". A released plan is in that category the moment
its tag exists; only the *open* cycle's plan is still edited.

## Impact

- The allowlist grows by one or two entries per release with a false exit
  condition, which the docstring itself calls "the honest case for an
  allowlist" — it is no longer honest.
- Each post-release follow-up either re-inherits the promise or spends a
  paragraph explaining why it cannot keep it.
- The documentation guide now states the archival rule ("frozen in place at
  the post-release follow-up; frozen plans are release evidence, exempt from
  the cap") and needs the checker to agree.

## Proposed fix

In `file_size.py`, treat `docs/v<X.Y.Z>-plan.md`, `-scope-locks.md`,
`-plan-amendment-*.md`, `-release-prep-plan.md`, and `-release-baseline.md`
as excluded **when tag `v<X.Y.Z>` exists** (`git tag --list`), mirroring the
`git ls-files` source-of-truth pattern `doc_links.py` uses. Then drop every
released plan from `GRANDFATHERED_FILES`, leaving only the open cycle's
entries. Moving files into a `docs/archive/` is rejected: every plan is
linked from ROADMAP, issues, and later plans, and a move would break those
links for no gain.

## Notes

> 2026-09-06 — filed while writing the methodology set; the documentation
> guide's new "Where Documents Live" section states the rule this issue
> implements.

> 2026-09-06 — RESOLVED in #858, with one change from the proposal.
> `file_size.py` treats a version-cycle doc (plan, scope locks, plan
> amendment, release-prep plan, release baseline) as excluded once
> `CHANGELOG.md` carries the version's dated `## [X.Y.Z] - date` heading
> (`_is_released_version_doc`; a two-part `v0.2` matches `0.2.0`). The
> proposal said `git tag`; that shipped first and went red on its first CI
> run — actions/checkout fetches a pull_request ref at depth 1 with no tags
> and ignores the fetch-tags input in that mode — so the source moved to the
> tree, where a depth-1 checkout, a worktree, and a tarball all answer the
> same. The changelog is dated at release-prep PR 3, one PR before the tag;
> that one-PR window is accepted. Sixteen allowlist entries retired; a
> still-allowlisted released doc prints `[STALE-ALLOWLIST]` (advisory, so a
> release does not turn `main` red before the follow-up retires the entry);
> the narrow-exclusion tripwire still passes because an unreleased version
> stays capped. No changelog → nothing released → everything capped. The
> documentation guide's "Where Documents Live" section describes the rule.
