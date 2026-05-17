# RFC 0029 — PR Implementation Plan (Phase 1 — v0.3.2 scope)

**RFC**: [0029-personal-society-storage-split.md](0029-personal-society-storage-split.md)
**Created**: 2026-05-17
**Branch prefix**: `feature/v032-rfc0029p1-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.2-plan.md Phase 1 (combined plans PR)](../v0.3.2-plan.md#phase-1--author-the-two-rfc-pr-plans)

---

## Overview

RFC 0029 draws the personal/society storage boundary behind a `MemoryStore` facade so every later cross-agent RFC pins against one decision instead of re-litigating "another table in `memory.db`". The RFC spans six phases; **only Phase 1 lands in v0.3.2** — facade promotion, no Postgres. Phases 2–6 (capability tokens, Postgres society backend, migration tooling, society-tier consumers, conditional file split) are reserved for v0.4.0 and carry no PR rows here — see [§Future Phases](#future-phases).

Phase 1 is a **pure refactor**: behaviour is identical, existing tests pin the API surface and must pass unchanged ([RFC §Test Strategy](0029-personal-society-storage-split.md#test-strategy)). Its load-bearing value is for v0.4.0 — [RFC 0028](0028-agent-decision-policy-engine.md) is hard-blocked on the frozen facade ([RFC §Phased Implementation Plan ordering invariant](0029-personal-society-storage-split.md#phased-implementation-plan)), which is why the [v0.3.2 master plan](../v0.3.2-plan.md#acceptance-for-v032) promotes "facade frozen" to a plan-level acceptance gate.

Phase 1 splits into **5 PRs**, per the [RFC §Decision/Next Steps step 3](0029-personal-society-storage-split.md#decision--next-steps) breakdown — facade promotion, lint rule + deprecation warnings, downstream call-site refactor — plus a review-follow-ups PR and a Phase 1 closeout, mirroring the [RFC 0017 PR plan](0017-pr-plan.md) / [RFC 0034 PR plan](0034-pr-plan.md) structure.

**Prerequisite**: RFC 0011 (Channels & Bridges) merged — the channel-history caller landed by [RFC 0011 PR 5](0011-pr-plan.md) is migrated to `MemoryStore` as part of the facade rename. RFC 0026 (Declarative Facts Tier — shipped v0.3.1) and RFC 0031 Phase 1 (session columns — shipped v0.3.1) are both merged; Phase 1 routes the RFC 0026 facts-tier calls through the new facade and the facade carries the RFC 0031 `session_id` already present on the tier APIs.

**No Postgres**: Phase 1 adds no Postgres dependency. Society-tier facade methods raise the `SocietyBackendUnavailable` hierarchy ([RFC §C](0029-personal-society-storage-split.md#c-memorystore-facade)); single-agent mode never opens a Postgres connection ([RFC §Goal 3](0029-personal-society-storage-split.md#goals)).

### Sequencing

**Recommended merge order**: **PR 1 → PR 2 → PR 3 → PR 4 → PR 5**. PR 1 promotes the facade and migrates the RFC 0011 channel-history caller as part of the rename. PR 2 (lint rule + deprecation warnings) and PR 3 (downstream call-site refactor) both depend only on PR 1 and are otherwise independent; the recommended order lands the guard rail before the bulk sweep so PR 3's diff is reviewed against an enforced boundary. PRs 4–5 are review follow-ups and the Phase 1 closeout.

This plan is independent of the [RFC 0023 PR plan](0023-pr-plan.md) at the implementation layer — the two v0.3.2 RFCs have disjoint surfaces (memory facade vs. the LLM-call / wallet path) and merge in parallel. Only the [v0.3.2 release-prep phase](../v0.3.2-plan.md#phase-4--v032-release-prep-execution) joins them.

---

## Dependency Graph

```
PR 1 (Promote MemoryFacade → MemoryStore in agents/memory/store.py;
      MemoryFacade kept as a thin alias shim for one minor version;
      society-tier methods raise SocietyBackendUnavailable;
      RFC 0011 channel-history caller migrated as part of the rename)
  ↓
PR 2 (Lint rule: block new direct-aiosqlite imports outside agents/memory/;
      DeprecationWarning on direct EpisodicMemory / RelationshipMemory construction)
  ↓
PR 3 (Downstream call-site refactor: persona_runtime/ + sub_agents/ + RFC 0026
      facts-tier paths migrated from the MemoryFacade shim to MemoryStore;
      tests/perf/personal_tier_latency.py harness lands)
  ↓
PR 4 (Review follow-ups)
  ↓
PR 5 (Phase 1 closeout — status: ⚠️ Partially Implemented;
      capture tests/perf/baselines/personal_tier_latency.json)
```

PR 1 is a pure rename + facade promotion; behaviour is identical. PR 2 and PR 3 both depend on PR 1 only — the arrow between them is the recommended review order, not a hard code dependency.

---

## PR Sequence

### PR 1: `feature/v032-rfc0029p1-facade-promotion` — `MemoryStore` Facade Promotion

**Depends on**: Nothing (v0.3.1 baseline; RFC 0011 / 0026 / 0031 Phase 1 all merged).
**Purpose**: Promote today's `MemoryFacade` to the typed `MemoryStore` facade in a new `agents/memory/store.py`, keep `MemoryFacade` as a thin alias shim for one minor version, and have society-tier methods raise the `SocietyBackendUnavailable` hierarchy. Pure refactor — behaviour identical. Implements the facade-promotion slice of [RFC §Phased Implementation Plan Phase 1](0029-personal-society-storage-split.md#phased-implementation-plan).

#### Scope

| File | Change |
|------|--------|
| `agents/memory/store.py` | **New** — `MemoryStore` facade home and `StoreConfig` dataclass per [RFC §C](0029-personal-society-storage-split.md#c-memorystore-facade). Personal-tier methods (episodes, notes, facts, bonds-self, commitments) delegate to the existing per-agent SQLite tiers — behaviour unchanged. Society-tier methods (`publish_to_pool`, `read_pool`, `query_inbound_trust`, …) raise the `SocietyBackendUnavailable` hierarchy — `SocietyDisabled` when `society_dsn=None`, `SocietyTransientError` otherwise. `record_action` raises `NotImplementedError` ([RFC §C](0029-personal-society-storage-split.md#c-memorystore-facade) — backend deferred to a later RFC). No `asyncpg`, no Postgres. |
| [`agents/memory/facade.py`](../../agents/memory/facade.py) | `MemoryFacade` becomes a thin alias of `MemoryStore` for one minor version ([RFC §Files Touched](0029-personal-society-storage-split.md#files-touched-estimated)). The legacy `retrieve_relevant(...)` method survives as a shim so any downstream caller missed by the call-site sweep keeps working. |
| Channel-history caller (RFC 0011 integration) | Migrated from `MemoryFacade.retrieve_relevant(...)` to `MemoryStore.retrieve_relevant(...)` **as part of the facade rename** ([RFC §Phased Implementation Plan ordering invariant](0029-personal-society-storage-split.md#phased-implementation-plan)). |
| [`config/agents.yaml`](../../config/agents.yaml) | New *optional* `memory.society_dsn` key — present but unconsumed in Phase 1 (single-agent mode is the only mode; a set DSN is accepted by the schema and ignored by the facade until Phase 3). |
| [`schemas/agent.schema.json`](../../schemas/agent.schema.json) | New optional `society` block under `memory` (`additionalProperties: false`). |
| `tests/unit/python/test_memory_store.py` | **New** — facade-surface tests: every personal-tier call returns identical results to the pre-rename `MemoryFacade`; society-tier calls raise `SocietyDisabled` with a message naming `memory.society_dsn`; `record_action` raises `NotImplementedError`. `test_single_agent_no_postgres` ([RFC §Test Strategy](0029-personal-society-storage-split.md#test-strategy)) — `MemoryStore(agent_id=...)` with `society_dsn=None` opens no Postgres connection. (Python: failing pytest first, no real network.) |
| `tests/unit/python/test_memory_facade*.py`, `agents/tests/test_persona_*.py` | No change — these existing suites pin the API surface and must pass unchanged through the `MemoryFacade` alias ([RFC §Test Strategy](0029-personal-society-storage-split.md#test-strategy)). |

#### Key implementation details

- **No tier-specific DSN escapes the facade** — callers never see `aiosqlite`. This is the v0.4-readiness invariant ([RFC §C property 1](0029-personal-society-storage-split.md#c-memorystore-facade)): when Phase 3 swaps in Postgres, no caller breaks.
- The `MemoryFacade` → `MemoryStore` rename keeps `MemoryFacade` as an importable alias so this PR does not have to touch every call site at once — PR 3 does the sweep. The one-minor-version shim is the documented compatibility window ([RFC §Phased Implementation Plan Phase 1](0029-personal-society-storage-split.md#phased-implementation-plan)); [v0.3.2 release-prep](../v0.3.2-plan.md#phase-3--v032-release-prep-plan) Upgrade Notes record it.
- Society-tier method *signatures* ship now (so the v0.4.0 boundary is frozen) but every body raises — Phase 1 promises the surface, not the backend.
- The RFC 0026 facts-tier methods are exposed on `MemoryStore` as personal-tier calls; routing the *callers* through them is PR 3's sweep — PR 1 only lands the methods.

#### Tests

- Every personal-tier method on `MemoryStore` returns results identical to the pre-rename `MemoryFacade` against the same fixture DB.
- `MemoryStore(agent_id="alice", society_dsn=None)` — every personal-tier call works; every society-tier call raises `SocietyDisabled`; no Postgres connection opened.
- The existing `test_memory_facade*.py` and persona integration suites pass unchanged through the `MemoryFacade` alias.

#### PR checklist

- [ ] `pytest tests/unit/python/test_memory_store.py tests/unit/python/test_memory_facade*.py agents/tests/test_persona_*.py -q` passes.
- [ ] `ruff check agents/` clean; `mypy agents/` clean.
- [ ] `make validate` passes against the new optional `memory.society` schema block.
- [ ] `MemoryStore` exposes the [RFC §C](0029-personal-society-storage-split.md#c-memorystore-facade) personal + society method surface; society-tier methods raise the `SocietyBackendUnavailable` hierarchy.
- [ ] `MemoryFacade` retained as a thin alias; RFC 0011 channel-history caller migrated to `MemoryStore`.
- [ ] No Postgres / `asyncpg` dependency added.
- [ ] [RFC 0029 row in ROADMAP](../../ROADMAP.md#rfc-master-index) → `🚧 Implementing` on this PR opening (first implementation PR); [v0.3.2-plan Master Progress Overview](../v0.3.2-plan.md#master-progress-overview) row 3 → 🔄 In progress.

---

### PR 2: `feature/v032-rfc0029p1-lint-deprecation` — Lint Rule + Deprecation Warnings

**Depends on**: PR 1 merged.
**Purpose**: Make the personal/society boundary enforceable. Add a lint rule blocking new direct-`aiosqlite` imports outside `agents/memory/`, and a `DeprecationWarning` on direct `EpisodicMemory` / `RelationshipMemory` construction. Implements the lint-rule + deprecation-warning slice of [RFC §Phased Implementation Plan Phase 1](0029-personal-society-storage-split.md#phased-implementation-plan).

#### Scope

| File | Change |
|------|--------|
| Lint config (`ruff` / repo lint setup) | Add a rule that flags a direct `import aiosqlite` (or `from aiosqlite import …`) in any file outside `agents/memory/` — the boundary the facade exists to enforce ([RFC §Phased Implementation Plan Phase 1](0029-personal-society-storage-split.md#phased-implementation-plan), [RFC §Security Considerations](0029-personal-society-storage-split.md#security-considerations)). |
| [`agents/memory/episodic.py`](../../agents/memory/episodic.py), [`agents/memory/relationship.py`](../../agents/memory/relationship.py) | Emit a `DeprecationWarning` on direct construction of `EpisodicMemory` / `RelationshipMemory` outside `agents/memory/` ([RFC §Test Strategy](0029-personal-society-storage-split.md#test-strategy)). Behaviour otherwise unchanged. |
| `tests/unit/python/test_memory_boundary.py` | **New** — assert direct `EpisodicMemory()` construction outside `agents/memory/` raises a `DeprecationWarning`; assert the lint rule fires on a fixture file with a direct `aiosqlite` import outside `agents/memory/` and stays silent inside it. |

#### Key implementation details

- The lint rule blocks *new* violations — it is scoped to files outside `agents/memory/`, where there should be zero existing direct-`aiosqlite` imports (personal-tier callers already go through the facade). PR 2 confirms a clean baseline; if the sweep finds a stray import it is migrated here or noted for PR 3.
- The `DeprecationWarning` targets *direct construction* (`EpisodicMemory(...)`, `RelationshipMemory(...)`), **not** the `MemoryFacade` alias — the alias is the supported one-minor-version compatibility path and stays warning-free. The warning is non-fatal; the test suite's `filterwarnings` configuration keeps it from failing unrelated runs while PR 3's sweep is in flight.
- Lint rule + deprecation warning are two halves of one guard rail ([RFC §Decision/Next Steps step 3](0029-personal-society-storage-split.md#decision--next-steps) groups them as one PR): the lint rule catches the import, the warning catches the construction.

#### Tests

- A fixture file with `import aiosqlite` outside `agents/memory/` fails the lint rule; the same import inside `agents/memory/` passes.
- Direct `EpisodicMemory()` / `RelationshipMemory()` construction from a test module raises `DeprecationWarning`; construction inside `agents/memory/` does not.

#### PR checklist

- [ ] `pytest tests/unit/python/test_memory_boundary.py -q` passes.
- [ ] `make lint` clean — the new rule has zero existing violations in the tree.
- [ ] Lint rule blocks new direct-`aiosqlite` imports outside `agents/memory/`.
- [ ] `DeprecationWarning` on direct `EpisodicMemory` / `RelationshipMemory` construction.

---

### PR 3: `feature/v032-rfc0029p1-callsite-refactor` — Downstream Call-Site Refactor

**Depends on**: PR 1 merged (PR 2 recommended-before per [§Sequencing](#sequencing)).
**Purpose**: Route every `persona_runtime/` and `sub_agents/` memory call — and the RFC 0026 facts-tier paths — through `MemoryStore`, off the `MemoryFacade` shim. After this PR the only `MemoryFacade` reference is the shim definition itself. Implements the downstream-call-site slice of [RFC §Phased Implementation Plan Phase 1](0029-personal-society-storage-split.md#phased-implementation-plan).

#### Scope

| File | Change |
|------|--------|
| `agents/persona_runtime/` memory call sites | Migrate every `MemoryFacade` reference to `MemoryStore` ([RFC §Goal 1](0029-personal-society-storage-split.md#goals) — every persona-runtime caller goes through the facade). Behaviour unchanged. |
| `agents/sub_agents/` memory call sites | Same migration for the sub-agent paths. |
| RFC 0026 facts-tier call sites | The facts-tier readers/writers shipped in v0.3.1 are routed through `MemoryStore`'s personal-tier facts methods ([v0.3.2-plan Phase 1 acceptance](../v0.3.2-plan.md#phase-1--author-the-two-rfc-pr-plans) — RFC 0026 facts-tier routing is in-scope for Phase 1). |
| `tests/perf/personal_tier_latency.py` | **New** — perf harness measuring `MemoryStore.recall_episodes` p99 against a fixed corpus ([RFC §Test Strategy](0029-personal-society-storage-split.md#test-strategy)). Lands here so it runs against the fully-wired `MemoryStore` surface; the *baseline* JSON it compares against is captured by PR 5 (post-Phase-1-merge). |
| `tests/unit/python/`, `agents/tests/` | Existing persona-runtime / sub-agent suites pass unchanged — the migration is mechanical and behaviour-preserving. Any test that constructed a `MemoryFacade` directly switches to `MemoryStore`. |

#### Key implementation details

- This is the bulk sweep — mechanical, behaviour-preserving. The PR 2 lint rule and `DeprecationWarning` make a missed call site visible; landing PR 2 first means PR 3 is reviewed against an enforced boundary.
- After PR 3 the `MemoryFacade` alias has no in-repo callers — it survives only as the documented one-minor-version external-compat shim, removed in v0.3.3.
- The perf harness is shipped now but the **gate is not enforcing yet** — there is no baseline until Phase 1 has merged. PR 5 captures `tests/perf/baselines/personal_tier_latency.json` and flips the harness into an enforcing CI gate. Phase 1 is a pure refactor, so the post-merge number is the legitimate "this is what the persona hot path costs after the rename" reference ([RFC §Test Strategy](0029-personal-society-storage-split.md#test-strategy)).

#### Tests

- Every persona-runtime and sub-agent memory test passes unchanged after the migration.
- A grep-style guard test (or the PR 2 lint rule extended) confirms no `MemoryFacade` reference remains outside the shim definition.
- `tests/perf/personal_tier_latency.py` runs and emits a p99 number (not yet gated).

#### PR checklist

- [ ] `pytest agents/tests/ tests/unit/python/ -q` passes.
- [ ] `ruff check agents/` clean; `mypy agents/` clean.
- [ ] Every `persona_runtime/` and `sub_agents/` memory call routes through `MemoryStore`.
- [ ] RFC 0026 facts-tier call sites routed through `MemoryStore`.
- [ ] No `MemoryFacade` reference remains outside the shim definition.
- [ ] `tests/perf/personal_tier_latency.py` runs (baseline capture + gate enforcement are PR 5).

---

### PR 4: `feature/v032-rfc0029p1-followups` — Review Follow-Ups

**Depends on**: PR 3 merged.
**Purpose**: Address review findings surfaced during PRs 1–3. Follows the [RFC 0017 PR plan §PR 6 precedent](0017-pr-plan.md) — "From PR N review" subsections, each finding paraphrased inline.

#### Scope

Items below are populated as PRs are reviewed. Per [.github/copilot-instructions.md](../../.github/copilot-instructions.md) ("Local-only files MUST NEVER be referenced in any committed file"), each entry paraphrases the finding inline and does **not** reference or link any local PR review report.

##### From PR 1 review

_None recorded at plan-authoring time._

##### From PR 2 review

_None recorded at plan-authoring time._

##### From PR 3 review

_None recorded at plan-authoring time._

#### PR checklist

- [ ] All deferred review findings addressed or downgraded to tracked issues with rationale.
- [ ] `make test` + `make lint` clean.

---

### PR 5: `feature/v032-rfc0029p1-close` — Phase 1 Closeout

**Depends on**: PR 4 merged.
**Purpose**: Mark Phase 1 implemented, capture the perf baseline, and hand off Phases 2–6 to v0.4.0.

#### Scope

| File | Change |
|------|--------|
| [`docs/rfcs/0029-personal-society-storage-split.md`](0029-personal-society-storage-split.md) | Status → `⚠️ Partially Implemented (Phase 1)`. Append a "Phase 1 implemented in v0.3.2" note to Decision/Next Steps. |
| [`ROADMAP.md`](../../ROADMAP.md) | RFC 0029 row → `⚠️ Partially Implemented`; target stays `v0.3.2 (Phase 1) + v0.4.0 (Phases 2–6)`; merged-PR rows for PRs 1–5; `Last updated` refresh. |
| `tests/perf/baselines/personal_tier_latency.json` | **New** — the post-Phase-1-merge baseline (`{"recall_episodes_p99_ms": <number>, "captured_at": <iso8601>, "captured_commit": <sha>}`) per [RFC §Test Strategy](0029-personal-society-storage-split.md#test-strategy). Flips `tests/perf/personal_tier_latency.py` from informational to an enforcing CI gate (fails on >20% regression). |
| [`docs/storage-architecture-roadmap.md`](../storage-architecture-roadmap.md) | SA-1 status flip per [RFC §Decision/Next Steps step 2](0029-personal-society-storage-split.md#decision--next-steps). |
| [`docs/rfcs/0029-pr-plan.md`](0029-pr-plan.md) | [Progress Overview](#progress-overview-phase-1) rows filled with merged-PR numbers and dates; all Phase 1 checklists complete. |

No code changes beyond the checked-in baseline JSON and the gate-enabling flag. `CHANGELOG.md` is **deferred to the v0.3.2 release process** ([v0.3.2-plan Phase 3 / 4](../v0.3.2-plan.md#phase-3--v032-release-prep-plan)).

#### Key implementation details

- The baseline is captured *after* the facade promotion has merged — Phase 1 is a pure refactor, so the post-merge p99 is the honest "cost of the persona hot path after the rename" number. The gate then protects v0.4.0 Phase 2/3 from accidentally routing personal-tier reads through Postgres ([RFC §Test Strategy](0029-personal-society-storage-split.md#test-strategy)).
- Full-RFC closeout waits for v0.4.0 (Phases 2–6); this PR is a **Phase 1** closeout — RFC 0029 → `⚠️ Partially Implemented`, not `✅ Implemented`.

#### PR checklist

- [ ] RFC 0029 status = `⚠️ Partially Implemented`.
- [ ] [ROADMAP RFC Master Index](../../ROADMAP.md#rfc-master-index) updated; merged-PR history includes PRs 1–5.
- [ ] `tests/perf/baselines/personal_tier_latency.json` captured; perf gate enforcing.
- [ ] [v0.3.2-plan Master Progress Overview](../v0.3.2-plan.md#master-progress-overview) row 3 → ✅ Merged.
- [ ] `make test`, `make lint`, `make validate` pass.

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| Phase 1 is billed as a pure refactor but touches every `persona_runtime/` and `sub_agents/` memory call site; unexpected coupling could slip the workstream. | [RFC §Test Strategy](0029-personal-society-storage-split.md#test-strategy): existing facade and persona integration tests pin the API surface and must pass unchanged. The `MemoryFacade` alias means PR 1 does not have to migrate every call site at once — PR 3's sweep is mechanical and reviewed against the PR 2 lint rule. |
| The PR 2 deprecation warning fires across the test suite during the PR 1→PR 3 window and noisy-fails unrelated runs. | The warning targets *direct construction*, not the `MemoryFacade` alias — persona-runtime callers use the alias and stay quiet. The warning is non-fatal and `filterwarnings`-scoped; PR 3 closes the window. |
| The perf gate has no baseline until Phase 1 merges, so a regression inside Phase 1 itself is not caught. | Phase 1 is a pure refactor — behaviour identical, existing tests are the regression guard. The perf gate exists to protect *v0.4.0* Phase 2/3 against Postgres routing; capturing the baseline post-Phase-1-merge ([RFC §Test Strategy](0029-personal-society-storage-split.md#test-strategy)) is the correct reference point, not earlier. |
| Society-tier method signatures ship in Phase 1 but every body raises — a caller could mistake the surface for working functionality. | The `SocietyBackendUnavailable` hierarchy raises with a message naming `memory.society_dsn`; `test_single_agent_no_postgres` pins the contract. v0.3.2 ships single-agent only — there is no society backend to mistake it for. |
| RFC 0029 Phase 1 slips and pressure builds to bundle it into the RFC 0023 workstream. | The two v0.3.2 RFCs are independent ([RFC §Phased Implementation Plan](0029-personal-society-storage-split.md#phased-implementation-plan)). Per [v0.3.2-plan §Risk and mitigations](../v0.3.2-plan.md#risk-and-mitigations), if Phase 1 slips RFC 0023 ships v0.3.2 and the facade work moves to a v0.3.2.x point release. |
| This plan rots as PRs 1–3 land. | Each PR's checklist updates the [Progress Overview](#progress-overview-phase-1) and the [v0.3.2-plan Master Progress Overview](../v0.3.2-plan.md#master-progress-overview); the [ROADMAP Hygiene](#roadmap-hygiene) rules below are part of every PR. |

---

## ROADMAP Hygiene

Per [.github/copilot-instructions.md §Status Hygiene](../../.github/copilot-instructions.md) and [v0.3.2-plan §ROADMAP hygiene](../v0.3.2-plan.md#roadmap-hygiene):

- **This PR-plan PR opens / merges** → no RFC 0029 status change — RFC 0029 stays `📋 Proposed`. The [RFC Master Index](../../ROADMAP.md#rfc-master-index) *target* flips to `v0.3.2 (Phase 1) + v0.4.0 (Phases 2–6)` in this PR.
- **PR 1 opens** → RFC 0029 row → `🚧 Implementing` (first implementation PR); [v0.3.2-plan Master Progress Overview](../v0.3.2-plan.md#master-progress-overview) row 3 → 🔄 In progress.
- **Each PR merges** → fill the [Progress Overview](#progress-overview-phase-1) row with the PR number and date.
- **PR 5 merges** → RFC 0029 row → `⚠️ Partially Implemented`; [v0.3.2-plan Master Progress Overview](../v0.3.2-plan.md#master-progress-overview) row 3 → ✅ Merged; `Last updated` refresh.

---

## Future Phases

Reserved for v0.4.0. Out of scope for this plan; tracking notes only — no PR rows. The canonical breakdown is [RFC §Phased Implementation Plan](0029-personal-society-storage-split.md#phased-implementation-plan).

- **Phase 2 — Capability-token plumbing.** Typed `StoreConfig.capability_token`, `CapabilityDenied` on missing scope. Soft-depends on RFC 0009 Phase 4; mock verifier until then.
- **Phase 3 — Postgres society backend.** `asyncpg` pool, `shared_pools` / `pool_entries` / `bonds_inbound` schema, write-through projection with the local outbox, Postgres backend for `internal/channels/`.
- **Phase 4 — Migration tooling.** `persatrix memory migrate` / `rollback`, read-only SQLite fallback, deprecation-window warnings.
- **Phase 5 — Society-tier consumers.** RFC 0028 `decision_records` and RFC 0013 `audit_chain` schemas land directly in the society Postgres.
- **Phase 6 — Conditional per-tier SQLite file split (SA-5).** Gated on a Phase 3 contention benchmark; default outcome is "not needed".

The v0.4.0 PR plan for Phases 2–6 opens when the v0.4.0 plan opens; the [RFC 0029 PR plan](0029-pr-plan.md) is extended there, not replaced.

---

## Progress Overview (Phase 1)

| # | Title | Branch | Status | GitHub PR | Merged |
|---|-------|--------|--------|-----------|--------|
| 1 | `MemoryStore` facade promotion | `feature/v032-rfc0029p1-facade-promotion` | ⬜ Not started | — | — |
| 2 | Lint rule + deprecation warnings | `feature/v032-rfc0029p1-lint-deprecation` | ⬜ Not started | — | — |
| 3 | Downstream call-site refactor | `feature/v032-rfc0029p1-callsite-refactor` | ⬜ Not started | — | — |
| 4 | Review follow-ups | `feature/v032-rfc0029p1-followups` | ⬜ Not started | — | — |
| 5 | Phase 1 closeout | `feature/v032-rfc0029p1-close` | ⬜ Not started | — | — |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged · ⏭ Deferred

---

## Related Documentation

- [RFC 0029 — Personal/Society Storage Split](0029-personal-society-storage-split.md) — canonical spec.
- [v0.3.2-plan.md](../v0.3.2-plan.md) — master plan; row 3 of the Master Progress Overview is this workstream.
- [RFC 0017 PR plan](0017-pr-plan.md) / [RFC 0034 PR plan](0034-pr-plan.md) — structural templates (Phase-1-only plan with a reserved `## Future Phases` section).
- [RFC 0023 PR plan](0023-pr-plan.md) — the paired v0.3.2 workstream (disjoint surface — the LLM-call / wallet path).
- [docs/storage-architecture-roadmap.md](../storage-architecture-roadmap.md) — SA-1, the planning doc RFC 0029 spawns from.
- [RFC 0011 — Channels & Bridges](0011-channels-bridges.md) / [RFC 0011 PR plan](0011-pr-plan.md) — the channel-history caller PR 1 migrates.
- [RFC 0026 — Declarative Facts Tier](0026-declarative-facts-tier.md) — v0.3.1 facts tier routed through the facade in PR 3.
- [RFC 0028 — Agent Decision Policy Engine](0028-agent-decision-policy-engine.md) — the v0.4.0 RFC hard-blocked on this Phase 1 facade freeze.
