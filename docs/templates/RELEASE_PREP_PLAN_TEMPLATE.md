# vX.Y.Z Release Preparation Plan

**Status**: 🔄 In progress — PR 0 open
**Target version**: vX.Y.Z (*<Codename>* — <what closes: issue IDs, RFC phases; which cuttable items were taken, not cut>)
**Created**: YYYY-MM-DD
**Branch prefix**: `feature/vXYZ-release-prep-`
**Target**: `main`
**Merge strategy**: Squash merge per `docs/BRANCHING.md`
**Master plan**: `docs/vX.Y.Z-plan.md` §Phase 2 + Phase 3

**Scope**: the release-prep workstream for the vX.Y.Z surface. All <n>
implementation PRs are merged — <workstreams and their PRs> — so `main` is a
usable RC tip. Which PR landed what is in the release baseline (below or split
out) and the master plan's Master Progress Overview.

This document owns the prep sequencing: **live execution** of `<MT ID>`; a
docs verification pass + a new release checklist; version bump
`X.Y.(Z-1)` → `X.Y.Z`; CHANGELOG `[X.Y.Z]` curation + dating; final pre-tag
verification.

**The release gate is the live arc — and it carries <n> evidence obligations.**
A green leg that never exercised the contested surface is not proof:

1. **<Claim A>** — <why storage or a count alone cannot show it; the artifact
   that does (a per-dispatch table, a triple, a count)>.
2. **<Claim B>** — …
3. **<An absence bar>** — satisfied by any empty read, so the leg reads <both
   partitions / a positive control> across <the restart / the window>.

**Out of scope**: new feature work; <the next version's bundle>; <the issues
that are Known Gaps to state, not work to do>.

**Documentation timing policy** (carry-forward): public docs updated *before*
the tag; post-tag-only artifacts (tag link, release URLs, "Released" stamps)
→ Phase 4.

---

## Progress Overview

| PR | Track | Title | Branch | Status | GitHub PR | Merged |
|----|-------|-------|--------|--------|-----------|--------|
| 0 | — | This plan + master-plan / ROADMAP reconciliation | `feature/vXYZ-release-prep-plan` | 🔀 | — | — |
| 1 | A | MT execution report — the arc live; closes <issue IDs> | `feature/vXYZ-release-prep-mt` | ⬜ | — | — |
| 2 | A | README + ROADMAP + guide verification + release checklist + Known Gaps | `feature/vXYZ-release-prep-docs` | ⬜ | — | — |
| 3 | B | Version bump (`X.Y.(Z-1)` → `X.Y.Z`) + CHANGELOG `[X.Y.Z]` curation & dating | `feature/vXYZ-release-prep-version-bump` | ⬜ | — | — |
| 4 | B | Final pre-tag verification & release notes | `feature/vXYZ-release-prep-final` | ⬜ | — | — |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged

**Dependencies**: PR 1 waits on PR 0. PR 2 waits on PR 1 (the checklist cites
the report). PR 3 → PR 2; PR 4 → PR 3.

---

## Current state (baseline, YYYY-MM-DD)

> Guidance: if this section would push the plan over the cap, split it into
> `docs/vX.Y.Z-release-baseline.md` and keep only the "differs from last
> release" list here. PR 2 copies the previous checklist forward; every fact
> below that differs from that checklist must be named, or a copied row is
> *wrong*, not merely stale.

### Issue roll-up
| Issue | Slotted by | State on the RC tip | Closes at |
|-------|-----------|---------------------|-----------|

### Version state — all strings at `X.Y.(Z-1)`; PR 3 bumps.
### Schema / migration state — <N> migrations this release: <store, from → to, PR, reader>. <Drop-in, or forward-only with a downgrade caution?>
### Wire-compatibility state — <additive proto fields? none?>
### Manual test state — <MT IDs, versions, which legs are new>
### Eval / golden-trace state — <n recipes; re-record needed?>
### Changelog state — `[Unreleased]` has <n> entries; bullets per story, not per PR.
### Dependency notices state — `make notices` <will / will not> show a delta.

**Differs from the vX.Y.(Z-1) checklist**: <the list PR 2 must not copy over>.

---

## Track A — Documentation + Manual Tests

### PR 1 — Manual Test Execution Report

**Branch**: `feature/vXYZ-release-prep-mt` · **Opens**: once PR 0 merges.

Scope: create `docs/manual-tests/vX.Y.Z-execution-report.md` from
`docs/templates/EXECUTION_REPORT_TEMPLATE.md`; execute on a clean checkout
against the `main` RC tip:

- **The whole `<MT ID>` arc** live on a real provider — all <n> legs — with the
  evidence obligations above recorded **verbatim**.
- `make eval-replay` green; the offline `make demo-autonomous` smoke ($0);
  regression spot checks.
- Findings triage: <what is release-blocking>; capture/reasoning misses become
  scoped fixes or `Accepted-with-known-gap` rows citing a tracked issue; red
  legs become in-release fix PRs, not re-deferrals.
- Closures: <issue IDs> → `resolved` citing the report (+ `make issues`).

Acceptance: report `✅ Complete`; every row `Pass` / `Accepted-with-known-gap`
— zero `Fail`, zero `Pending`.

### PR 2 — Docs + Release Checklist

**Branch**: `feature/vXYZ-release-prep-docs`

Scope: README Roadmap row; ROADMAP Version Map → release prep + concise header
refresh; **verify** every doc this release edited (<list>) against shipped
behaviour; create `docs/vX.Y.Z-release-checklist.md` from
`docs/templates/RELEASE_CHECKLIST_TEMPLATE.md`, carrying: the <N>-migration
gate, this cycle's suites by name, the enumerated test targets (never "make
test" alone), Known Gaps (<issue IDs> + PR 1 findings).

Acceptance: `make validate` + doc gates clean; the checklist lists every
pre-tag gate; ROADMAP/README rows correct.

---

## Track B — Release Engineering

### PR 3 — Version Bump + Changelog Curation & Dating

**Branch**: `feature/vXYZ-release-prep-version-bump`

Scope: `make bump-version VERSION=X.Y.Z` + `cd cli && cargo update --workspace`;
`make all` + `make ui`; curate `[Unreleased]` → dated `[X.Y.Z]`; write the
**Upgrade Notes** whose obligations were fixed at Phase 0:

1. <migrations by store and direction>
2. <coherence trade>
3. <metric / wire shape change>
4. <what stays byte-identical>

Acceptance: all version strings at `X.Y.Z`; curated dated `[X.Y.Z]`; prior
sections untouched.

### PR 4 — Final Pre-Tag Verification & Release Notes

**Branch**: `feature/vXYZ-release-prep-final`

Scope: full checklist sweep on a clean checkout — `make test` (all four legs),
`cargo test`, `make lint`, `make validate`, `make proto && git diff
--exit-code`, `make check-licenses`, `make notices` (<delta expected?>),
`make generate-sanitizer-patterns-check`, `make ui` + `make ui-test` +
`make ui-html-check`, `make eval-replay` on the post-bump tip, the separate
`mypy tests/` leg, the offline Docker smoke. Then ROADMAP → `✅ All pre-tag
gates green`; this plan → complete; draft release notes (curated changelog +
Upgrade Notes + Known Gaps + the closing evidence quoted from the PR 1
report).

> Do **not** write "Released" here — the tag does not exist until after this
> PR merges. Phase 4 flips ROADMAP, this plan and the checklist with the tag
> links.

Tag procedure (after PR 4 merges):

```bash
git tag -a vX.Y.Z -m "vX.Y.Z — <Codename>"
git push origin main --tags
```

---

## Status hygiene

<Which ROADMAP / master-plan rows flip at each PR; `make issues` after every closure.>

## Related documentation

- `docs/methodology/release-cycle.md` §Phase 2–3
- `docs/vX.Y.Z-plan.md` · `docs/vX.Y.(Z-1)-release-checklist.md` (the one PR 2 copies forward)
