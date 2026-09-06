# Persatrix — Git Branching Strategy

> **Last updated**: 2026-09-06 — rewritten to describe what the repository
> actually does. The 2026-04 version prescribed `release/*` and `hotfix/*`
> branches, a stabilisation period, and artifact publishing; none of that was
> ever used (twenty tags, all cut from `main`, zero release branches). How a
> release is made is in [methodology/release-cycle.md](methodology/release-cycle.md).

## Overview

Trunk-based development. `main` is the single long-lived branch and is always
releasable; every change reaches it through a short-lived branch and a
squash-merged pull request. Chosen because the project ships small versions
often, the codebase is polyglot (a change usually spans Go, Python, and Rust
or the console), and a single maintainer plus AI assistants gain nothing from
release-branch ceremony.

## Branches

### `main`

- All work merges here via PR; squash merge; linear history is required.
- Required status checks are listed in the
  [enforcement matrix](methodology/enforcement-matrix.md#enforcement-levels);
  `strict` mode means a branch must be current with `main` before it can merge.
- Release tags (`vX.Y.Z`) are cut from `main` after release-prep PR 4 merges.
- No direct pushes.

### Short-lived branches

| Prefix | Use | Share of merged PRs |
|--------|-----|---------------------|
| `feature/vNNN-<component>-<description>` | Implementation work for version `vN.N.N`; the version prefix (`v0315-`) groups a cycle's PRs | ~70 % |
| `docs/<description>` | Documentation, process, and planning PRs not tied to a code change | ~12 % |
| `fix/<description>` | Bug fixes outside a version's planned PR list | ~10 % |
| `ci/`, `chore/`, `test/` | Tooling, CI, dependency, and test-only changes | few |
| `dependabot/…` | Automated dependency bumps | few |

Lifetime: hours to a few days. Branch from `main`; **rebase** onto `main`
rather than merge to keep the branch linear (the squash discards branch
history anyway). Delete after merge (`gh pr merge --delete-branch`).

### Cross-language changes stay in one branch

Adding a gRPC method touches `proto/`, Go, and Python; a knob touches Go, the
Rust CLI's lockstep guard, and the console. These land in **one** PR — a
split would leave `main` broken between merges. The Rust suite and the
proto-staleness gates exist to catch the split anyway.

## Commit convention

[Conventional Commits](https://www.conventionalcommits.org/). The PR title is
the squash commit's subject, so the title is what `commitlint.yml` checks.

```
<type>(<scope>): <description>
```

Types the PR-title check accepts: `feat`, `fix`, `perf`, `refactor`, `docs`,
`ci`, `test`, `chore`, `style`, `build`, `revert`, `infra`. Scope is
optional; common scopes are package or area names (`channels`, `memory`,
`server`, `cli`, `proto`, `release`, `methodology`, `checks`), and a version
tag (`v0315`) is used for a cycle's implementation PRs. Several scopes may be
comma-separated (`test(manual-tests,docs)`).

Body: **why**, not what — the diff shows what. Reference the plan row, RFC
section, or issue the change serves. Never add `Co-Authored-By` or assistant
attribution trailers.

## Pull requests

**Title**: the commit subject above.

**Body** — the shape every PR since v0.3.x has used, pre-filled by
`.github/PULL_REQUEST_TEMPLATE.md`:

```markdown
## What        — one paragraph
## Why         — the plan row / RFC / issue, and the defect or gap
## How         — the two or three decisions a reviewer must check
## Not in this PR — what was deliberately left out and where it lives
## Gates       — the commands run and their results
## Review      — F-n findings and their dispositions (added after review)
```

**Size**: target under 500 changed lines of meaningful change. This is
guidance the checks do not enforce, and about a third of merged PRs exceed
it — almost all of them documentation-heavy (execution reports, plans,
frozen release evidence). Code PRs should split: a migration in its own PR
ahead of its reader; a store change before its wiring; a follow-up PR for
review findings that outgrow the original scope.

**Review**: every PR, before merge, per
[methodology/review-process.md](methodology/review-process.md). Findings are
recorded in the PR body and the plan; local review reports are never linked.

**Merge**: squash, once every required check is green and every finding has
a disposition. Pull `main`, flip the plan row, check ROADMAP.

## Tags and releases

Semantic versioning `vX.Y.Z`. Tags are annotated (`git tag -a vX.Y.Z -m
"vX.Y.Z — <codename>"`) and pushed with `git push origin main --tags` after
release-prep PR 4 merges; the GitHub Release body is the curated changelog
plus Upgrade Notes and Known Gaps. Releases carry no binary assets today.
The whole sequence — master plan, implementation PRs, release-prep PRs 0–4,
tag, post-release follow-up — is in
[methodology/release-cycle.md](methodology/release-cycle.md).

Breaking changes to config schemas or the agent interface happen at MINOR
boundaries and ship with a migration; a PATCH never breaks a schema.

## FAQ

**Can I commit directly to `main`?** No — branch protection blocks direct
pushes, and every change goes through a PR so CI and a review run.

**My branch depends on another unmerged branch.** Merge the dependency first
and rebase; stacked branches are allowed but rebase badly, so prefer smaller,
faster-merging PRs.

**Rebase or merge to update my branch?** Rebase. Required checks are `strict`,
so an out-of-date branch cannot merge until it is current; the pre-commit hook
regenerates `FILEMAP.md` on the rebased commits.

**What about hotfixes?** A `fix/*` branch to `main`, then a patch release
through the normal cycle. There are no release branches to backport to.

## Related documentation

- [methodology/release-cycle.md](methodology/release-cycle.md) — how a version is planned and shipped
- [methodology/review-process.md](methodology/review-process.md) — the review every PR gets
- [methodology/enforcement-matrix.md](methodology/enforcement-matrix.md) — which checks are required
- [CONTRIBUTING.md](../CONTRIBUTING.md) — setup and quality gates
