# RFC 0008 — PR Implementation Plan (scaffold)

**RFC**: [0008-agent-memory-context-optimization.md](0008-agent-memory-context-optimization.md)
**Created**: 2026-04-25
**Branch prefix**: `feature/v030-rfc0008-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.0-plan.md Phase 1 (combined plans PR)](../v0.3.0-plan.md#phase-1--author-the-six-rfc-pr-plans)

> **Status**: 🔨 Scaffold — PR rows have branch names, scopes, and dependency links pinned, but per-PR key-implementation-detail and tests sections are placeholders. Flesh out before the first implementation PR opens.

---

## Overview

RFC 0008 introduces the per-step context budget allocator, the `MemoryFacade` for task agents, the delegation contract + merge engine, and shared memory pools. Full RFC scope ships in v0.3.0.

This plan splits the work into **6 PRs**.

> **Estimate calibration**: 1.7× factor per [RFC 0017 PR plan precedent](0017-pr-plan.md#overview).

**Prerequisite**: RFC 0006 Phase 1 already shipped in v0.2.0. No v0.3.0 RFC merge dependency — RFC 0008 sits at the top of the v0.3.0 dep chain alongside RFC 0020.

**Cross-RFC sequencing** (downstream consumers — this plan must merge ahead of them):
- PR 1 of this plan must merge before [RFC 0007 PR plan](0007-pr-plan.md) PR 3 opens — `repeat_until` loop budget integration requires per-step context-budget allocation.
- PR 2 (`MemoryFacade` for task agents) must merge before [RFC 0011 PR plan](0011-pr-plan.md) PR 5 (Phase 3) opens — channel-scoped recall calls `MemoryFacade.retrieve_relevant`.
- PR 2 must also merge before [RFC 0020 PR plan](0020-pr-plan.md) PR 4 opens — RFC 0020 PR 4's summarize-on-close path calls into the `MemoryFacade.compress` hook introduced here. (RFC 0020 PR 4's depends-on row already pins this direction.)

---

## Dependency Graph

```
PR 1 (Phase 1 — context budget + packaging foundation)
  ↓
PR 2 (Phase 2 — MemoryFacade for task agents + eviction/TTL)
  ↓
PR 3 (Phase 3 — DelegationRequest/Result + merge engine)
  ↓
PR 4 (Phase 4a — shared pool ACL + provenance)
  ↓
PR 5 (Phase 4b — confidence decay + procedural revalidation)
  ↓
PR 6 (Review follow-ups + RFC close)
```

---

## PR Sequence

### PR 1: `feature/v030-rfc0008-context-budget` — Phase 1: Context Budget + Packaging

**Depends on**: Nothing (RFC 0006 Phase 1 already shipped).
**Estimated size**: ~400–500 lines.

#### Scope (high-level)

- `internal/scheduler/` — context budget allocation per step.
- `internal/executor/` — dispatch contract extensions for context package + budget.
- `internal/cost/` — extend budget accounting with context-package metrics.
- New candidate-selection + relevance-scoring module.
- Extractive compression + deterministic truncation order.
- Context-assembly metrics in step metadata.

#### Key implementation details *(TBD before PR opens)*
#### Tests *(TBD)*

#### PR checklist

- [ ] ROADMAP.md row for RFC 0008 → `🚧 Implementing`
- [ ] Master Progress Overview row 4 → 🔄 In progress

---

### PR 2: `feature/v030-rfc0008-memory-facade` — Phase 2: MemoryFacade for Task Agents

**Depends on**: PR 1.
**Estimated size**: ~350–500 lines.

#### Scope (high-level)

- `agents/memory/facade.py` — `MemoryFacade` with `store_observation`, `retrieve_relevant`, `compress`.
- Task-agent integration via `agents/task_agent.py`.
- Config + schema updates for task memory policies.
- Basic eviction + TTL policy.

#### Key implementation details *(TBD)*
#### Tests *(TBD)*

#### PR checklist

- [ ] `MemoryFacade.retrieve_relevant` exposes the `tags` filter required by RFC 0011 P3
- [ ] `MemoryFacade.compress` exposes the hook required by RFC 0020 PR 4

---

### PR 3: `feature/v030-rfc0008-delegation-merge` — Phase 3: Delegation Contract + Merge Engine

**Depends on**: PR 1.
**Estimated size**: ~400–500 lines.

#### Scope (high-level)

- `DelegationRequest` + `DelegationResult` data contracts.
- Merge strategy implementation + conflict handling.
- Observability for merge outcomes + dropped fields.

#### Key implementation details *(TBD)*
#### Tests *(TBD)*

---

### PR 4: `feature/v030-rfc0008-shared-pools-acl` — Phase 4a: Shared Pool ACL + Provenance

**Depends on**: PR 2 + PR 3.
**Estimated size**: ~300–450 lines.

#### Scope (high-level)

- Shared pool ACL + provenance policy.
- Curated publish workflow from isolated to shared memory.

#### Key implementation details *(TBD)*
#### Tests *(TBD)*

---

### PR 5: `feature/v030-rfc0008-procedural-revalidation` — Phase 4b: Confidence Decay + Revalidation

**Depends on**: PR 4.
**Estimated size**: ~250–400 lines.

#### Scope (high-level)

- Confidence decay function over time.
- Stale-procedural-memory revalidation pipeline.

#### Key implementation details *(TBD)*
#### Tests *(TBD)*

---

### PR 6: `feature/v030-rfc0008-close` — Review Follow-Ups + RFC Close

**Depends on**: PR 5.
**Estimated size**: ~150–300 lines.

| File | Change |
|------|--------|
| `docs/rfcs/0008-agent-memory-context-optimization.md` | Status → `✅ Implemented`. |
| `ROADMAP.md` | RFC 0008 row → `✅ Implemented`; merged-PR rows. |
| `docs/v0.3.0-plan.md` | Master Progress Overview row 4 → ✅. |

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| PR 1 budget contract disagrees with RFC 0007 loop budget contract | RFC 0007 PR plan must be authored after this one (Phase 1 master-plan ordering); review surfaces gaps early. |
| `MemoryFacade.compress` shape disagrees with RFC 0020 summarize-on-close call site | Cross-reference RFC 0020 PR 4 in this plan's PR 2 review checklist. |
| Phase 4 (shared pools) scope creeps into authentication territory belonging to RFC 0009 | ACL is policy-only; auth tokens are RFC 0009 P3–4 (deferred to v0.4.0). |

---

## ROADMAP Hygiene

- **PR 1 opens** → ROADMAP RFC 0008 → `🚧 Implementing`; Master Progress Overview row 4 → 🔄.
- **PR 6 merges** → ROADMAP RFC 0008 → `✅ Implemented`; row 4 → ✅.

---

## Scaffold TODOs

Before opening PR 1:
- [ ] Fill in "Key implementation details" + "Tests" sections for each PR.
- [ ] Pin estimated sizes against the RFC's Files Touched table.
- [ ] Add PR checklist items per the [RFC 0017 PR plan](0017-pr-plan.md) precedent.
