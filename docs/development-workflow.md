# Persatrix — Development Workflow

> **Last updated**: 2026-09-06

> **Scope note (2026-09-06).** This document describes the **RFC-level** cycle: how one RFC goes from authoring to closure. It is still accurate for that. The **release-level** cycle that has run every version since v0.3.0 — sequencing amendment → master plan with scope locks → implementation PRs → release-prep plan → release-prep PRs 1–4 → tag → post-release follow-up — is described in [docs/methodology/release-cycle.md](methodology/release-cycle.md); this document nests inside its Phase 1. The review discipline the phases below rely on is in [methodology/review-process.md](methodology/review-process.md), and the process vocabulary in the [process glossary](methodology/process-glossary.md).

This document describes the end-to-end development lifecycle for Persatrix, from version planning through RFC closure. It connects the strategic planning loop to the tactical processes documented in [CONTRIBUTING.md](../CONTRIBUTING.md) (PR process), [BRANCHING.md](BRANCHING.md) (branch naming), and [rfcs/README.md](rfcs/README.md) (RFC format and lifecycle).

> **Live trackers** (auto-generated from per-file YAML front-matter):
> - [`docs/rfcs/INDEX.md`](rfcs/INDEX.md) — every RFC with status, type, target, created date. Regenerate with `make rfcs`.
> - [`docs/issues/INDEX.md`](issues/INDEX.md) — every internal issue with status, severity, area. Regenerate with `make issues`.

---

## Table of Contents

- [Overview](#overview)
- [Phase 1 — Version Planning](#phase-1--version-planning)
- [Phase 2 — RFC Authoring](#phase-2--rfc-authoring)
- [Phase 3 — PR Plan Creation](#phase-3--pr-plan-creation)
- [Phase 4 — Core Implementation](#phase-4--core-implementation)
- [Phase 5 — Follow-Up PRs](#phase-5--follow-up-prs)
- [Phase 6 — Refactoring Assessment](#phase-6--refactoring-assessment)
- [Phase 7 — Documentation & Diagrams](#phase-7--documentation--diagrams)
- [Phase 8 — RFC Close](#phase-8--rfc-close)
- [Status Hygiene](#status-hygiene)
- [Issue Files](#issue-files-for-deferred-or-cross-cutting-findings)
- [Worked Example — RFC 0005](#worked-example--rfc-0005)

---

## Overview

Development follows a repeating cycle per RFC, nested inside version-level planning:

```
Version Planning
  └─ for each RFC (in dependency order):
       RFC Authoring
         → PR Plan Creation
           → Core Implementation (with continuous finding capture)
             → Follow-Up PRs (batched review findings)
               → Refactoring Assessment
                 → Documentation & Diagrams
                   → RFC Close
```

Each phase has clear entry/exit criteria and produces specific artifacts. The cycle repeats for every RFC within a version, then the next version is planned.

---

## Phase 1 — Version Planning

**Entry**: Previous version complete (or project kickoff).
**Exit**: Version scope defined, RFC list and dependency chain documented in [ROADMAP.md](../ROADMAP.md).

### Activities

1. Define the version's goal and scope (e.g., v0.2 = "Agent Societies").
2. Identify the RFCs needed to deliver the version scope.
3. Establish the RFC dependency chain — which RFCs must merge before others can start.
4. Document the RFC list, dependency graph, and planned components in ROADMAP.md.

### Artifacts

- ROADMAP.md RFC Tracker table with planned RFCs (status: not yet written).
- ROADMAP.md dependency chain diagram.
- ROADMAP.md Planned Components table.

### Example (v0.2)

```
RFC 0005 (PersonaAgent + Memory)
    ↓
RFC 0006 (Efficiency & Execution Limits)
    ↓
RFC 0007 (Conditional & Looped Control Flow)
    ↓
RFC 0008 (Sub-Agent Spawning)
    ↓
RFC 0009 (Channels + Bridges)
    ↓
RFC 0010 (Protocols + Organizations)
```

---

## Phase 2 — RFC Authoring

**Entry**: Previous RFC in chain is Implemented (or no dependency).
**Exit**: RFC status transitions to `👍 Accepted`.

### Activities

1. Write the RFC following the format in [rfcs/README.md](rfcs/README.md).
2. Include all required sections for the RFC type (design, phased implementation plan, security considerations, test strategy).
3. Review the RFC — verify feasibility, security, cross-component impact.
4. Accept the RFC: transition status from `📋 Proposed` → `👍 Accepted`.
5. Update ROADMAP.md RFC Tracker with the new status.

### Artifacts

- `docs/rfcs/NNNN-kebab-case-title.md` at `👍 Accepted` status.
- ROADMAP.md updated.

---

## Phase 3 — PR Plan Creation

**Entry**: RFC accepted.
**Exit**: PR plan document created, RFC status transitions to `🚧 Implementing`.

### Activities

1. Split the RFC's phased implementation plan into PRs of <500 lines each (per [BRANCHING.md](BRANCHING.md)).
2. Define the PR dependency and parallelization graph.
3. Estimate PR sizes with a calibration factor based on historical data (e.g., 1.7× for RFC 0005 based on v0.1 actuals).
4. For each PR, specify: scope (files touched), key implementation details, test requirements, and a checklist.
5. Reserve a final PR slot for review follow-ups and RFC close (this will be populated during Phase 4).
6. Transition RFC status to `🚧 Implementing`.
7. Update ROADMAP.md with PR count and status.

### Artifacts

- `docs/rfcs/NNNN-pr-plan.md` with full PR sequence, dependency graph, and estimates (start from [`docs/templates/PR_PLAN_TEMPLATE.md`](templates/PR_PLAN_TEMPLATE.md)).
- RFC status at `🚧 Implementing`.
- ROADMAP.md updated.

### Sizing guidance

| Naive estimate | Calibrated (1.7×) | Action |
|----------------|--------------------|--------|
| < 300 lines | < 500 lines | Single PR |
| 300–500 lines | 500–850 lines | Split into 2 PRs |
| > 500 lines | > 850 lines | Split into 3+ PRs |

Calibration factors should be updated per-RFC based on actuals from the previous RFC.

---

## Phase 4 — Core Implementation

**Entry**: PR plan created.
**Exit**: All planned core PRs merged.

### Activities

1. Implement PRs in the dependency order defined in the PR plan.
2. For each PR:
   a. Create the feature branch (`feature/v0X-component-description`).
   b. Implement the changes, write tests, verify lint/test/validate pass locally.
   c. Open the PR with a Conventional Commit title and structured body.
   d. **Review the PR** — conduct a thorough review (architecture, security, testing, edge cases).
   e. **Record review findings** — append a "Review findings" table to the PR's section in the PR plan. Each finding gets a severity (Medium/Low/Info) and an action.
   f. **Apply immediate fixes** — findings that are small and in-scope for the current PR get fixed immediately (committed on the branch before merge).
   g. **Defer remaining findings** — findings that are out-of-scope or cross-cutting get deferred to the follow-up PRs. Tag them with `→ deferred to PR N`.
   h. Merge the PR (squash merge to `main`).
   i. Update ROADMAP.md merged PR count (the Merged PR History itself is generated — `docs/merged-prs.md`).
   j. Mark the PR plan checklist items as complete.
3. Repeat for each core PR in sequence.

### Key principle: Continuous finding capture

Review findings are recorded *per PR review*, not batched at the end. Each PR section in the plan accumulates its own findings table. This ensures:

- No findings are lost between review and implementation.
- Each finding has traceability to its source PR.
- The follow-up PR scope is built incrementally, not estimated from memory.

> **Escape hatch**: If review findings reveal a fundamental design flaw that cannot be addressed as a follow-up, return to [Phase 2](#phase-2--rfc-authoring) to revise the RFC before continuing implementation.

### Issue files for deferred or cross-cutting findings

Findings that are out-of-scope for the current RFC cycle (or surface outside of a PR review) are captured as issue files in [`docs/issues/`](issues/README.md). Use an issue file when:

- A finding is deferred beyond the current follow-up PR batch.
- A finding is cross-cutting (affects multiple RFCs or components) and has no immediate home in a PR plan.
- A finding surfaces outside PR review (e.g. during manual testing, a spike, or an AI-assisted audit).

Issue files are not a replacement for the PR plan's review findings table — keep per-PR findings in the plan. Issue files handle the longer tail.

The tracker uses a hybrid layout: a per-issue file (`ISSUE-NNNN-slug.md`) is the source of truth for each finding, and [`docs/issues/INDEX.md`](issues/INDEX.md) is auto-regenerated as the live summary table.

**Creating an issue file:**
1. Copy [`docs/issues/ISSUE-TEMPLATE.md`](issues/ISSUE-TEMPLATE.md) to `docs/issues/ISSUE-NNNN-slug.md` (next free `NNNN`).
2. Fill `id`, `status`, `severity`, `area`, `created`, and `refs` (link to the RFC or PR plan where the finding originated). For trivial findings a one-line **Summary** is enough; the other sections are optional.
3. Run `make issues` to regenerate [`docs/issues/INDEX.md`](issues/INDEX.md).
4. Add a cross-reference inline in the source document. The relative path depends on the source location — see the table in [`docs/issues/README.md`](issues/README.md#cross-reference-paths).

**Closing an issue file:** set `status: resolved`, fill `closed:` and `closed_pr:`, run `make issues`, and reference the issue ID in the closing PR's commit message. Resolved files stay in `docs/issues/` so future readers can recall historical decisions with `grep` instead of `git log`. A periodic sweep can prune very old resolved files.

### Artifacts

- Merged PRs on `main`.
- PR plan updated with per-PR review findings and completed checklists.
- `docs/merged-prs.md` regenerated per merge (automatic in the pre-commit hook; `make merged-prs`).

---

## Phase 5 — Follow-Up PRs

**Entry**: All core PRs merged. Deferred findings accumulated in PR plan.
**Exit**: All deferred review findings addressed.

### Activities

1. Collect all deferred findings from the PR plan into an "Accumulated Follow-ups" section (if not already built incrementally).
2. Group findings by component/concern area (e.g., memory tier fixes, persona fixes, CLI fixes).
3. Size each group — if a group exceeds 500 lines, split into sub-PRs.
4. Add the follow-up PRs to the PR plan with full scope, implementation details, and checklists.
5. Implement each follow-up PR using the same review-and-merge cycle as Phase 4.
6. Review findings from follow-up PR reviews are either fixed in-place or added as new deferred items. Iterate until no High or Medium findings remain. Low/Info findings may be deferred to the next RFC cycle.

### Splitting strategy

The follow-up PR scope is often larger than initially expected. RFC 0005's single "PR 7" grew from ~50–150 lines estimated to 4 sub-PRs (7a–7d) totaling ~900–1,250 lines once all 60 findings were enumerated. Plan for this growth.

### Artifacts

- Follow-up PRs merged.
- PR plan updated with follow-up PR sections and checklists.

---

## Phase 6 — Refactoring Assessment

**Entry**: All follow-up PRs merged.
**Exit**: Refactoring PRs (if any) merged.

### Activities

1. Review the codebase for modules that grew past maintainability thresholds during the RFC implementation.
2. Apply refactoring triggers:

   | Trigger | Threshold | Action |
   |---------|-----------|--------|
   | File size | > 800 LOC | Split into focused modules |
   | Class responsibilities | > 2 distinct concerns | Extract into separate classes/modules |
   | Repeated patterns | Same logic in 3+ places | Extract shared utility |
   | Test file size | > 600 LOC | Split by test category |

3. If refactoring is needed, add refactoring PRs to the PR plan.
4. Implement refactoring PRs — each should be a pure structural change (no behavior changes) for easy review.
5. Verify all tests still pass after each refactoring PR.

### Example (RFC 0005)

| PR | Refactoring | Trigger |
|----|------------|---------|
| 8a | Split `persona.py` into `persona/` package | ~900 LOC after PRs 5a+5b+7b |
| 8b | Split `episodic.py` into `episodic/` package | ~800 LOC after PRs 3a+3b+3c+7a |
| 8c | Split `main.rs` into CLI modules | ~700 LOC after PRs 1b+6b+7c |

### Artifacts

- Refactoring PRs merged (if any).
- PR plan updated.

---

## Phase 7 — Documentation & Diagrams

**Entry**: All code PRs (core + follow-up + refactoring) merged. Codebase is in its final state for this RFC.
**Exit**: Architecture diagrams and documentation updated to reflect the implemented design.

### Rationale

Code changes accumulate across many PRs, but documentation and diagrams that explain *how the system works* should be written once the implementation is stable — not mid-flight when the design is still shifting. Placing this phase after refactoring ensures diagrams reflect the final module structure, not an intermediate state.

### Activities

1. Identify which architectural areas changed during the RFC implementation.
2. Create or update **Mermaid diagrams** in `docs/diagrams/` covering:
   - **Component interaction diagrams**: how the major modules connect (gRPC, REST, internal calls).
   - **Data flow diagrams**: request lifecycle, workflow execution flow, memory read/write paths.
   - **Module structure diagrams**: package/module organization after refactoring.
   - **Sequence diagrams**: key runtime flows (e.g., persona event dispatch, memory recall, tick loop).
3. Update prose documentation where diagrams reveal gaps or stale descriptions:
   - Architecture sections in spec docs.
   - README or CLAUDE.md if component boundaries changed.
4. Ensure every diagram has a title and a brief caption explaining what it shows.
5. Add the documentation PR to the PR plan with scope and checklist.

### Diagram conventions

| Convention | Rule |
|------------|------|
| Format | Mermaid (`.md` files with fenced `mermaid` blocks) — renders natively on GitHub |
| Location | `docs/diagrams/` directory, one file per diagram or logical group |
| Naming | `NNNN-kebab-description.md` where `NNNN` matches the RFC number |
| Scope | Each diagram should fit on one screen — split complex flows into sub-diagrams |
| Labels | Use actual module/file names, not abstract boxes |

### Artifacts

- Diagram files in `docs/diagrams/`.
- Updated prose documentation (if needed).
- PR plan updated with documentation PR section.

---

## Phase 8 — RFC Close

**Entry**: All PRs (core + follow-up + refactoring + documentation) merged.
**Exit**: RFC status transitions to `✅ Implemented`.

### Activities

1. Verify all PR plan checklists are complete.
2. Verify all tests pass (`make test`), all linters pass (`make lint`), config validation passes (`make validate`).
3. Transition RFC status to `✅ Implemented` in the RFC file.
4. Update ROADMAP.md:
   - RFC Tracker: status → `✅ Implemented`, merged count = total.
   - Component Status tables: update affected components.
   - Final PR appears in `docs/merged-prs.md` on the next regeneration.
5. The close PR itself should be minimal (status updates only, ~50–100 lines).

### Artifacts

- RFC file at `✅ Implemented`.
- ROADMAP.md fully updated.
- PR plan fully checked off.

### Then

Return to [Phase 2](#phase-2--rfc-authoring) for the next RFC in the dependency chain. When all RFCs for the version are implemented, return to [Phase 1](#phase-1--version-planning) for the next version.

> **Note**: Documentation PRs (Phase 7) are not counted toward the RFC's "code complete" milestone. They can overlap with the RFC close PR if the documentation changes are small enough.

---

## Status Hygiene

Status is tracked across multiple documents. **Before and after every task**, verify consistency:

| Document | What to check |
|----------|---------------|
| RFC file | `Status:` field matches actual state. Use [lifecycle markers](rfcs/README.md#rfc-lifecycle). |
| PR plan | Checklist items (`- [x]` / `- [ ]`) reflect completed work. Review findings recorded. |
| [ROADMAP.md](../ROADMAP.md) | RFC Tracker (merged count, status), Component Status tables. The Merged PR History is generated (`docs/merged-prs.md`) and checked by `make plan-status-check` / CI for rows that still say "PR open" after the PR merged. |

Rules:

1. Starting implementation of an RFC → status to `🚧 Implementing` in both RFC file and ROADMAP.
2. PR merged → update PR plan checklist and RFC merged count immediately; the merged-PR history regenerates itself, and `plan-status-check` flags any row left saying "PR open".
3. All PRs merged → status to `✅ Implemented` in RFC file and ROADMAP.
4. Component moves from stub to working → update Component Status table in ROADMAP.
5. Never leave a stale status.
6. Creating a new RFC → add it to the ROADMAP RFC Tracker table.

---

## Worked Example — RFC 0005

RFC 0005 (Persona Agent & Memory System) is the first v0.2 RFC and demonstrates the full lifecycle:

| Phase | What happened | Artifacts |
|-------|--------------|-----------|
| **1. Version Planning** | v0.2 scope defined: personas, memory, channels, bridges. RFC dependency chain: 0005 → 0006 → 0007 → 0008. | ROADMAP.md v0.2 section |
| **2. RFC Authoring** | RFC 0005 written with 6 implementation phases. Reviewed and accepted. | `docs/rfcs/0005-persona-agent-memory.md` |
| **3. PR Plan** | Split into 11 core PRs (1a–6b). Sizes calibrated at 1.7× based on v0.1 actuals (~73–138% overrun). Reserved PR 7 for follow-ups. | `docs/rfcs/0005-pr-plan.md` |
| **4. Core Implementation** | 11 PRs implemented and merged (#47–#57). Each PR reviewed; findings recorded in PR plan per-PR sections (60 total findings: 48 assigned to follow-ups, 2 fixed in-place, 10 deferred beyond scope). | PRs #47–#57 merged |
| **5. Follow-Up PRs** | PR 7 split into 4 sub-PRs (7a–7d) when 48 findings exceeded 500-line limit. Grouped by component: memory (7a), persona+validation (7b), CLI (7c), close (7d). | PRs 7a–7d (0/4 merged) |
| **6. Refactoring** | Assessment identified 3 files exceeding 800 LOC. PRs 8a–8d split oversized modules. | PRs 8a–8d merged (#64–#67) |
| **7. Documentation** | Architecture diagrams covering system components, data flows, and module structure. PR 9 planned. | Pending |
| **8. RFC Close** | PR 7d will transition RFC 0005 to `✅ Implemented`. | Pending |

### Key lessons from RFC 0005

- **Calibrate estimates**: v0.1 PRs overran by 73–138%. Applying a 1.7× factor to RFC 0005 produced more accurate estimates.
- **Plan for follow-up growth**: The original single "PR 7" (50–150 lines) grew to 4 sub-PRs (~900–1,250 lines) once all review findings were enumerated.
- **Record findings continuously**: Per-PR review tables in the PR plan prevented finding loss and made follow-up PR scoping straightforward.
- **Split early**: When a follow-up PR group approaches the 500-line limit, split preemptively rather than hoping it fits.

---

## Related Documentation

- [Methodology](methodology/README.md) — the release-level cycle, decision mechanisms, review process, and process glossary this RFC cycle sits inside
- [CONTRIBUTING.md](../CONTRIBUTING.md) — PR process, quality gates, development setup
- [BRANCHING.md](BRANCHING.md) — Branch naming, lifecycle, merge strategy
- [rfcs/README.md](rfcs/README.md) — RFC format, lifecycle, and templates
- [ROADMAP.md](../ROADMAP.md) — Version progress, RFC tracker, merged PR history
- [Documentation Guide](documentation-guide.md) — Documentation update conventions
- [Consistency Checklist](docs-consistency-checklist.md) — Documentation consistency verification
- [rfcs/README.md](rfcs/README.md) — RFC format and lifecycle (conventions); [rfcs/INDEX.md](rfcs/INDEX.md) is the live table (`make rfcs`)
- [issues/README.md](issues/README.md) — Deferred and cross-cutting findings tracker (conventions); [issues/INDEX.md](issues/INDEX.md) is the live table (`make issues`)
