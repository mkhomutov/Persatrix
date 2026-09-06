# <RFC NNNN — Title> — PR Implementation Plan
<!-- or: # ISSUE-NNNN — PR Implementation Plan (<the residual this covers>) -->

**RFC / Issues**: `docs/rfcs/NNNN-<slug>.md` <or the issue IDs>
**Status**: ⬜ Not started
**Created**: YYYY-MM-DD
**Branch prefix**: `feature/vXYZ-<component>-`
**Target**: `main`
**Merge strategy**: Squash merge per `docs/BRANCHING.md`
**Spawned from**: <the review, amendment, or plan that asked for this>

> Guidance: an RFC's plan splits its phased implementation into PRs under 500
> lines each; an issue-owned plan does the same for residual work too large
> for one PR. Sizes are calibrated (v0.1 actuals ran 1.7× naive estimates).
> Reserve a final PR for review follow-ups and closeout.

---

## Overview

<What the RFC or issue delivers, in one or two paragraphs. What is already in
place and must not be rebuilt (list the seams by path).>

### Why these are one workstream

<If two changes must ship in the same release, say what defect shipping either
alone would leave.>

### What is already in place (do not rebuild)

- `<path>` — <what it provides> (<PR that landed it>).

---

## Phase 0 — design gate (if any)

| Axis | Question | Answer | Date |
|------|----------|--------|------|
| <axis> | <question the PRs cannot start without> | <answer, or "open — PR n cannot start"> | YYYY-MM-DD |

---

## Progress Overview

| PR | Title | Branch | Status | GitHub PR | Merged |
|----|-------|--------|--------|-----------|--------|
| 1 | <title> | `feature/vXYZ-…` | ⬜ | — | — |
| 2 | <title> | … | ⬜ | — | — |
| n | Review follow-ups + closeout | … | ⬜ | — | — |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged · ✂️ Cut

## Dependency graph

```
PR 1 (store, dormant) ──► PR 2 (wiring + gate) ──► PR 3 ──► PR 4 ──► PR n (closeout)
```

<Name every hard edge and why it is not optional.>

---

## PR Sequence

### PR 1 — `feature/vXYZ-<slug>`

**Scope**: <files / packages touched>.
**Implementation**: <the two or three decisions a reviewer must be able to check>.
**Tests**: <the failing test written first; the regression class it pins>.
**Migration**: <none | store vN → vN+1 in this PR, reader lands in PR k>.
**Acceptance**: <one observable line>.
**Estimate**: ~<n> lines (calibrated).

**Checklist**
- [ ] Failing test first, confirmed red
- [ ] Implementation; `make lint` / suites green for the language
- [ ] Docs / glossary / status hygiene moved together
- [ ] Review findings recorded below

**Review findings**

| Finding | Severity | Description | Disposition |
|---------|----------|-------------|-------------|
| F-1 | <High/Medium/Low/Info> | <one line> | Fixed in-PR / → deferred to PR n / filed as ISSUE-NNNN / accepted-with-known-gap |

### PR 2 — …

### PR n — Review follow-ups + closeout

**Purpose**: address findings deferred from PRs 1–(n-1), grouped by component;
flip the RFC / issue status; record divergences. Findings are paraphrased
inline — never a link to a local review report.

#### From PR 1 review
- <finding, paraphrased> — <what this PR does about it>

---

## Risks

| Risk | Mitigation |
|------|------------|
| <risk> | <mechanism> |

## Notes

> YYYY-MM-DD — <dated running notes; amendments to this plan are dated entries here or a `§Amendment` section>

## Related documentation

- `docs/methodology/release-cycle.md` §Phase 1 · `docs/methodology/review-process.md`
- `docs/development-workflow.md` §Phase 3 — the RFC-level sizing guidance
