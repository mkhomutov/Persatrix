# RFC 0026 — PR Implementation Plan

**RFC**: [0026-declarative-facts-tier.md](0026-declarative-facts-tier.md)
**Created**: 2026-05-12
**Branch prefix**: `feature/v031-rfc0026-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.1-plan.md Phase 1 (combined plans PR)](../v0.3.1-plan.md#phase-1--author-the-two-rfc-pr-plans)

---

## Overview

RFC 0026 promotes `key_facts` out of episode summaries into a first-class **declarative-fact tier**: at interaction close, an LLM extractor emits structured tuples `(subject, predicate, object, certainty, source_interaction_id, asserted_at)` persisted to a dedicated `facts` table indexed by `subject` and injected via the [RFC 0017 `MemoryBudget`](0017-persona-memory-injection-budget.md) allocator.

This is the §A deliverable from the [Memory Quality Roadmap](../memory-quality-roadmap.md#a-promote-key_facts-to-a-declarative-fact-tier) and the highest-leverage fix for the [dementia test](../memory-quality-roadmap.md#quality-bar--the-dementia-test). v0.3.1 ships **all three RFC phases in full**.

This plan splits the work into **6 PRs**. Each stays under the [BRANCHING.md](../BRANCHING.md) 500-line soft cap.

**Prerequisite**:
- v0.3.0 merged (✅ released 2026-05-12).
- RFC 0020 ✅ Implemented — PR 4 summarize-on-close hook is the extension point for the extractor.
- [RFC 0017](0017-persona-memory-injection-budget.md) ✅ Implemented — `MemoryBudget` allocator is the injection seam.

**Cross-RFC sequencing**: **PR 1 of this plan depends on [RFC 0031 PR plan PR 3](0031-pr-plan.md#pr-3-featurev031-rfc0031p1-sessions-py--python-migrations--persona-runtime-threading--cross-process-integration)** — the new `facts` table is created with `session_id TEXT NOT NULL DEFAULT 'legacy'` from day one, mirroring the column convention RFC 0031 PR 3 lands. Acceptance from [v0.3.1-plan §Phase 2 workstream sequencing](../v0.3.1-plan.md#phase-2--implement-the-two-rfcs).

### Open-question resolutions locked at plan-authoring time

[RFC 0026 §Open Questions](0026-declarative-facts-tier.md#open-questions) — none gate Phase 0, but several need pinning so PR 2 (the extractor) does not re-litigate during review.

- **OQ #1 — predicate vocabulary scope.** Small + extensible. PR 2 authors a ≈30-verb allowlist across attribute / preference / commitment / relationship + self-* (per OQ #10). Later additions land via PR amendment to the RFC.
- **OQ #2 — tier-budget slice.** `budget_tokens: 200` initial default (≈13% of RFC 0017's 1500-token allocator). Calibration lives in [RFC 0008 calibration review](0008-calibration-review.md), not this plan.
- **OQ #3 — multi-form subject (`Bob` vs `Robert`).** Deferred to a v0.3.x follow-up. v0.3.1 stores the canonical form per §C; an alias table is a separate scope.
- **OQ #4 — cross-agent fact sharing.** Out of scope — v0.4.0 pairs with RFC 0008 §H.
- **OQ #5 — extraction model selection.** PR 2 default: inherit from `optimization.yaml → context_management.summarization.model`. The config knob `memory.facts.extraction_model` is added but defaults to `null` (inherit).
- **OQ #6 — negative facts / state-history retention.** Deferred to a v0.3.x follow-up issue. Phase 3 ships latest-asserted-wins only; superseded rows stay queryable via a future `recall(..., include_history=True)` flag if dogfood data shows the persona attempts state-history queries.
- **OQ #7 — cross-fact semantic contradiction.** Out of scope (RFC 0027 §B reflection-time sweep).
- **OQ #8 — inferred facts.** Out of scope (v0.4.0 alongside RFC 0027).
- **OQ #9 — operator-seeded facts.** Out of scope for v0.3.1; v0.3.x follow-up.
- **OQ #10 — self-as-subject coverage.** PR 2 vocabulary includes `self.*` predicates (e.g., `self.has_preference`, `self.holds_value`, `self.committed_to`) alongside user-facing ones. Locked before the extractor prompt freezes.

### Sequencing

**Recommended merge order**: **PR 1 → PR 2 → PR 3 → PR 4 → PR 5 → PR 6**.

---

## Dependency Graph

```
RFC 0031 PR 3 (Python session_id columns)  ← external dep
  ↓
PR 1 (facts table schema + FactStore CRUD + delete_by_subject primitive)
  ↓
PR 2 (Extractor wired into RFC 0020 PR 4 summarize-on-close + predicate allowlist + audit log)
  ↓
PR 3 (FactStore.recall + MemoryBudget tier slot + config knobs + tier ordering)
  ↓
PR 4 (Reinforcement + retraction + tier-provenance instrumentation + MT-MEMORY-005 expected-outcomes update)
  ↓
PR 5 (Review follow-ups)
  ↓
PR 6 (RFC close)
```

---

## PR Sequence

### PR 1: `feature/v031-rfc0026-facts-schema-store` — Facts Schema + FactStore + Erasure Primitive

**Depends on**: [RFC 0031 PR plan PR 3](0031-pr-plan.md#pr-3-featurev031-rfc0031p1-sessions-py--python-migrations--persona-runtime-threading--cross-process-integration) merged. The new `facts` table is created with `session_id TEXT NOT NULL DEFAULT 'legacy'` from day one, matching the column convention RFC 0031 PR 3 lands.

#### Scope

| File | Change |
|------|--------|
| `agents/memory/facts.py` | **New** — `Fact` dataclass (`fact_id` ULID, `agent_id`, `subject`, `predicate`, `object`, `certainty`, `source_interaction_id`, `asserted_at`, `last_recalled_at`, `superseded_by`, `session_id`); `FactStore` with `store`, `recall`, `supersede`, `prune`, `delete_by_subject`. No extractor wiring yet (PR 2). Predicate validation is a callable injection seam — Phase-1 ships a permissive stub; PR 2 wires the allowlist. |
| [`agents/memory/_migration_handlers.py`](../../agents/memory/_migration_handlers.py) | New handler `_apply_migration_<N>` (next available after RFC 0031 PR 3's handler): `CREATE TABLE facts (...) WITH session_id TEXT NOT NULL DEFAULT 'legacy'`; `CREATE INDEX idx_facts_subject_agent ON facts(agent_id, subject)`; `CREATE INDEX idx_facts_session ON facts(session_id)`. No-op early-return guard if `facts` table already exists (RFC 0020 PR 6 finding #4 precedent). |
| [`agents/memory/migrations.py`](../../agents/memory/migrations.py) | Wire the new handler into the umbrella runner. |
| `agents/observability/metrics.py` | Counters: `facts.stored`, `facts.superseded`, `facts.extraction_failed` (the last is a placeholder; PR 2 wires the increment site). |
| `tests/unit/python/test_fact_store.py` | **New** — CRUD; supersede chain; `session_id` round-trip with default `"legacy"`; `delete_by_subject` traverses both `subject` and `source_interaction_id` (per §H). |
| `tests/unit/python/test_facts_schema_migration.py` | **New** — fresh DB; upgrade from legacy DB; idempotence; no-op early-return on missing prerequisite tables. |

#### Key implementation details

- `FactStore.delete_by_subject(subject_id) -> dict[str, int]` returns `{"facts_deleted_by_subject": N, "facts_deleted_by_source_interaction": M}`. This is the **storage primitive** RFC 0013's `SubjectErasure` will wire into when RFC 0013 implements (v0.5.0 per [ROADMAP RFC Master Index](../../ROADMAP.md#rfc-master-index)). Phase 1 of RFC 0026 ships the primitive; the umbrella `SubjectErasure.delete` wiring is RFC 0013's responsibility. Without the primitive, the first GDPR / CCPA traversal after v0.3.1 ships would silently miss extracted facts — flagged in [RFC 0026 §H](0026-declarative-facts-tier.md#h-subject-erasure-rfc-0013-traversal).
- `FactStore` API mirrors `EpisodicMemory`'s shape — same DB connection model, same migration framework.
- `session_id` carries through `FactStore.store` the same way RFC 0031 PR 3 threads it on `EpisodicMemory`. No recall-side filtering yet (that's RFC 0031 Phase 2, not this plan's scope).
- `superseded_by` chain enables latest-asserted-wins retraction (PR 4 ships the policy; PR 1 ships the data shape).

#### Tests

- Store + recall round-trip on a fresh table.
- Supersede chain: writing a fact with same `(agent_id, subject, predicate)` and later `asserted_at` writes `superseded_by` on the older row.
- `delete_by_subject` traverses both columns; audit map contains both subtotal keys.
- Schema migration idempotence; no-op early-return when prerequisite tables are missing.
- `session_id` defaults to `'legacy'` when caller omits; round-trips when supplied.

#### PR checklist

- [ ] `pytest tests/unit/python/test_fact_store.py tests/unit/python/test_facts_schema_migration.py -v` passes.
- [ ] `ruff check agents/memory/` clean; `mypy agents/memory/` clean.
- [ ] [RFC 0026 row in ROADMAP](../../ROADMAP.md#rfc-master-index) → `🚧 Implementing` on this PR opening.
- [ ] [v0.3.1-plan Master Progress Overview](../v0.3.1-plan.md#master-progress-overview) row 2 → 🔄 In progress.

---

### PR 2: `feature/v031-rfc0026-extractor` — Extractor at Interaction Close + Predicate Allowlist + Audit Log

**Depends on**: PR 1 merged.
**Purpose**: Wire the fact extractor into the existing RFC 0020 PR 4 summarize-on-close LLM call. One LLM call, two structured outputs.

#### Scope

| File | Change |
|------|--------|
| [`agents/persona_runtime/summarize_close.py`](../../agents/persona_runtime/summarize_close.py) | Extend the existing summarize-on-close prompt to a **two-output** structured prompt: (a) prose summary, (b) JSON list of fact tuples. Single LLM call, structured outputs parsed into the existing `interaction.summary` write **plus** new `FactStore.store` calls. Atomic transaction wraps both writes per [RFC 0026 §Phase 1 step 4](0026-declarative-facts-tier.md#phase-1-schema--extractor). Parse failure on facts (not summary) commits summary + increments `facts.extraction_failed`. |
| `agents/memory/facts.py` | Predicate allowlist constant `_PREDICATE_ALLOWLIST` (≈30 verbs, includes `self.*` per OQ #10); `FactStore.store` rejects unknown predicates (raises `ValueError`; counted under `facts.extraction_failed`). Subject canonicalization helper per §C (counterparty → `sender_id`; existing canonical reuse; otherwise case + whitespace normalize). |
| `agents/persona_runtime/summarize_close.py` | Use the model from `optimization.yaml → context_management.summarization.model` per OQ #5; no separate model alias in Phase 1. |
| `agents/memory/facts.py` | Audit log entry per `store` and per `supersede` write — uses the existing `agents/observability/audit.py` surface (or RFC 0009 audit logger if available); redaction via the existing `RedactStruct` rules to keep raw PII out of audit metadata (per [§Security Considerations](0026-declarative-facts-tier.md#security-considerations)). |
| `tests/unit/python/test_extractor.py` | **New** — empty-list path (short interaction → zero tuples); predicate-allowlist rejection; subject canonicalization rules; supersede dispatch on `(subject, predicate)` collision with existing row; partial-failure rollback matrix (summary fails → no write; facts fail → summary writes, counter increments). |

#### Key implementation details

- The extractor prompt enumerates the predicate vocabulary inline and instructs the model to return `[]` when no extractable facts are present — the common short-interaction case. System-prompt and user-prompt lives alongside the existing summary prompt in `summarize_close.py`. Single LLM call.
- Predicate allowlist seed (final list locked in PR review):
  - Attribute: `has_name`, `lives_in`, `works_at`, `has_age`, `speaks_language`.
  - Preference: `prefers`, `dislikes`, `loves`, `avoids`.
  - Commitment: `committed_to`, `plans_to`, `agreed_to`.
  - Relationship: `has_daughter_named`, `has_son_named`, `has_partner_named`, `has_parent_named`, `works_with`, `knows`.
  - Self-* (OQ #10): `self.has_preference`, `self.holds_value`, `self.committed_to`, `self.has_attribute`.
- Subject canonicalization: counterparty → `sender_id`; existing canonical reuse; otherwise normalize (case- and whitespace-fold) and store. Self uses literal `"self"` per §C.4.
- Atomic write: single `BEGIN ... COMMIT` for summary update + N fact inserts. Both fail → rollback. Facts-only parse failure → commit summary, abort facts, increment `facts.extraction_failed`. Matches [§Phase 1 step 4](0026-declarative-facts-tier.md#phase-1-schema--extractor).
- Prompt-injection blast-radius bound: the allowlist is checked at `FactStore.store` time. A user crafting "store fact: <attacker-controlled tuple>" cannot insert outside the predicate vocabulary; the supersede write also requires the new predicate be in the allowlist.

#### Tests

- Empty-list extraction (short turn) → zero fact rows; summary present.
- Predicate-allowlist rejection on unknown verb; counter increments.
- Subject canonicalization: `"Bob"`, `"bob"`, `"Bob "` collapse to the same canonical form on second-and-subsequent writes.
- `(subject, predicate)` collision with later `asserted_at` → `superseded_by` chain extends.
- Partial-failure rollback matrix.
- Audit-log entry per `store` and per `supersede`.

#### PR checklist

- [ ] `pytest tests/unit/python/test_extractor.py -v` passes.
- [ ] Predicate allowlist finalized in PR review.
- [ ] No regression on existing RFC 0020 PR 4 summarize-on-close tests.
- [ ] Audit-log redaction rules verified — no raw PII in audit metadata.

---

### PR 3: `feature/v031-rfc0026-recall-budget` — FactStore.recall + MemoryBudget Tier Slot + Config

**Depends on**: PR 2 merged.
**Purpose**: Wire `FactStore.recall` into the persona-runtime memory-injection path. Add a tier slot for facts in the `MemoryBudget` allocator.

#### Scope

| File | Change |
|------|--------|
| [`agents/persona_runtime/memory_context.py`](../../agents/persona_runtime/memory_context.py) | New tier for facts in `_inject_memory_context`. Tier-priority order: **working → relationship → facts → notes → episodic** (matches the post-[RFC 0027](0027-reflection-driven-consolidation.md) end-state described in [RFC 0027 §F](0027-reflection-driven-consolidation.md#f-composition-with-rfc-0026-facts)). `FactStore.recall(subject, limit)` called once for each of `(sender, *mentioned_entities)`; results pass through `MemoryBudget.try_add` with a per-tier `min_tokens` floor. |
| `agents/persona_runtime/memory_context.py` | New constant `_FACTS_BUDGET_TOKENS = 200` per OQ #2. RFC 0017's overall 1500-token allocator budget is unchanged; the facts slot is a soft per-tier floor enforced at the call site, not a separate allocator. |
| [`config/agents.yaml`](../../config/agents.yaml) | New `memory.facts.{enabled, budget_tokens, extraction_model}` block per [§E](0026-declarative-facts-tier.md#e-composition-with-recall_notes). |
| [`schemas/agent.schema.json`](../../schemas/agent.schema.json) | Schema additions for the new block. |
| `agents/observability/metrics.py` | Counter `facts.injected{tier=facts}` increments per admitted fact. |
| `tests/integration/test_facts_recall.py` | **New** — fact stored at interaction N is injected at interaction N+1 when the subject reappears, *without* the subject string appearing in the query (dementia-test core invariant). Tier-ordering test: when facts saturate their slice, notes and episodic still receive their share. |

#### Key implementation details

- The five-tier ordering matches the post-RFC-0027 end-state described in [RFC 0027 §F](0027-reflection-driven-consolidation.md#f-composition-with-rfc-0026-facts). The notes-vs-episodic relative ordering is owned by [RFC 0017 OQ #1](0017-persona-memory-injection-budget.md#open-questions) and was settled by the RFC 0017 PR 6 RFC amendment.
- The facts tier slot reuses the same `MemoryBudget.try_add` pattern as the other tiers — no new allocator. Calling order in `_inject_memory_context` ensures facts are admitted before notes / episodic, so a subject's high-signal short facts displace lower-signal prose under budget pressure.
- Config knob `memory.facts.enabled: true` is the v0.3.1 default. Operators can disable for diagnostic purposes.
- Headers for the facts section are charged against the token budget — the PR-2 review precedent (header tokens add ~5 per tier) holds. Add the header inside the `if items:` block via `budget.try_add(header)` so the under-report bug from RFC 0017 PR 2 finding #2 does not recur.

#### Tests

- Subject-indexed recall round-trip on a fresh DB.
- Dementia-test core: fact in turn N injected at turn N+1 without subject-string keyword overlap.
- Tier-ordering: facts admitted before notes when budget is tight.
- Config: `memory.facts.enabled: false` skips the tier; no `facts.injected` increment.
- Token-bound invariant: RFC 0017's `_MEMORY_BUDGET_TOKENS = 1500` cap holds with the facts tier wired in.

#### PR checklist

- [ ] `pytest tests/integration/test_facts_recall.py -v` passes.
- [ ] No regression on RFC 0017 token-bound contract (`test_token_bound_holds_with_large_content` and companions).
- [ ] Schema entry validated by `make validate`.

---

### PR 4: `feature/v031-rfc0026-reinforcement-retraction` — Salience + Retraction + Tier Provenance + MT Expected-Outcomes

**Depends on**: PR 3 merged.
**Purpose**: Ship use-based reinforcement, latest-asserted-wins retraction, and per-turn tier-provenance instrumentation so [MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md) leg-failure diagnoses can disambiguate recall miss from reasoning miss.

#### Scope

| File | Change |
|------|--------|
| `agents/memory/facts.py` | `last_recalled_at` updated on every `MemoryBudget`-admitted recall. Latest-asserted-wins retraction: on `store`, if a row exists with same `(agent_id, subject, predicate)` and older `asserted_at`, write `superseded_by`. Recall filters superseded rows by default. |
| [`agents/persona_runtime/memory_budget.py`](../../agents/persona_runtime/memory_budget.py) (or memory_context allocator surface) | **Per-turn tier-provenance instrumentation** (MQ-11 — see [v0.3.x-sequencing §Risks](../v0.3.x-sequencing.md#risks) row 5). The allocator emits a per-turn structured-log record `{tier: str, item_id: str, tokens_admitted: int}` via the existing RFC 0018 structured-log surface. The instrumentation is owned at the allocator level so every tier (relationship, facts, notes, episodic) is captured uniformly. |
| `agents/memory/facts.py` | Audit-log entry per `supersede` write. |
| [`docs/manual-tests/MT-MEMORY-005-dementia-test.md`](../manual-tests/MT-MEMORY-005-dementia-test.md) | **Expected-outcomes table updated** per [v0.3.1-plan Phase 2 cross-cutting acceptance](../v0.3.1-plan.md#phase-2--implement-the-two-rfcs): Legs 1 (named entity), 2 (preference), 5 (self-consistency) expected pass; Legs 3, 4 unchanged. MT execution itself happens in [v0.3.1 release-prep Phase 4 PR 1](../v0.3.1-plan.md#phase-4--v031-release-prep-execution). |
| `tests/integration/test_facts_reinforcement.py` | **New** — `last_recalled_at` advances on admit; supersede chain; superseded row absent from default recall; tier-provenance record emitted per admission. |

#### Key implementation details

- Reinforcement composes with [RFC 0008 §G](0008-agent-memory-context-optimization.md#g-memory-eviction-decay-and-validation) decay via the same scoring seam (per RFC 0026 Goal 3). The full reinforcement formula lands in the [RFC 0008 calibration review](0008-calibration-review.md); this PR ships the `last_recalled_at` write only — the formula consumes it.
- Retraction-race serialization handled by the per-agent `asyncio.Lock` already in `_LLMPersonaAgent` (per [§Security Considerations](0026-declarative-facts-tier.md#security-considerations)). No new lock.
- Tier-provenance instrumentation owned by the allocator, not `FactStore` — emit at admission time so every tier is captured uniformly. The per-turn record is the diagnostic signal that lets MT-MEMORY-005 leg-failure analyses identify *which tier* missed when a leg fails (recall miss vs reasoning miss).

#### Tests

- Reinforcement: `last_recalled_at` updated on admit.
- Retraction: latest-asserted-wins; superseded row absent from default recall.
- Provenance: per-turn instrumentation captures every admitted item with `tier`, `item_id`, `tokens_admitted`.

#### PR checklist

- [ ] `pytest tests/integration/test_facts_reinforcement.py -v` passes.
- [ ] [MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md) expected-outcomes table reflects Legs 1 / 2 / 5 → expected pass.
- [ ] Tier-provenance entry emitted on every `MemoryBudget`-admitted recall (asserted in the new test).

---

### PR 5: `feature/v031-rfc0026-followups` — Review Follow-Ups

**Depends on**: PR 4 merged.
**Purpose**: Apply review findings from PRs 1–4. Follows the [RFC 0017 PR plan §PR 6 precedent](0017-pr-plan.md) — "From PR N review" subsections, each finding paraphrased inline (no link to local review reports per [.github/copilot-instructions.md](../../.github/copilot-instructions.md)).

#### Scope

Items populated during review. Reserved skeleton:

- "From PR 1 review" — schema / FactStore findings.
- "From PR 2 review" — extractor / allowlist / audit findings.
- "From PR 3 review" — recall / budget integration findings.
- "From PR 4 review" — reinforcement / retraction / provenance findings.

#### PR checklist

- [ ] All deferred review findings addressed or downgraded to tracked issues with rationale.
- [ ] `make test` + `make lint` clean.

---

### PR 6: `feature/v031-rfc0026-close` — RFC Close

**Depends on**: PR 5 merged.

#### Scope

| File | Change |
|------|--------|
| [`docs/rfcs/0026-declarative-facts-tier.md`](0026-declarative-facts-tier.md) | Status → `✅ Implemented`. |
| [`ROADMAP.md`](../../ROADMAP.md) | RFC 0026 row → `✅ Implemented`; component statuses updated; `Last updated` refresh. |
| [`docs/rfcs/0026-pr-plan.md`](0026-pr-plan.md) | [Progress Overview](#progress-overview) filled with merged-PR numbers and dates. |

No code changes; doc-only.

#### PR checklist

- [ ] RFC 0026 status = `✅ Implemented`.
- [ ] [v0.3.1-plan Master Progress Overview](../v0.3.1-plan.md#master-progress-overview) row 2 → ✅.

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| The combined summarize + extract prompt is bigger than the standalone summarize prompt; LLM may produce malformed JSON more often. | PR 2 ships the partial-failure rollback (summary commits even if facts parse fails; counter increments). `facts.extraction_failed` rate surfaces in observability; tuning happens in PR 4 / dogfood. |
| Predicate allowlist locks the vocabulary at PR 2 author time; a missing predicate produces silent rejection at extraction. | The allowlist is co-located with the extractor prompt — extending it is a one-line PR. PR 2 ships the rejection-rate counter so a hot-spot predicate surfaces in observability. |
| RFC 0013 erasure surface does not exist yet (target v0.5.0), so PR 1's `delete_by_subject` primitive has no in-tree caller until then. | The primitive is shipped now to avoid the GDPR / CCPA blind spot called out in [RFC 0026 §H](0026-declarative-facts-tier.md#h-subject-erasure-rfc-0013-traversal). Without it, RFC 0013 would land later and have to retro-patch every memory tier. PR 1 carries an inline comment naming RFC 0013 as the eventual caller; an entry is added to [RFC 0013 §C](0013-legal-ethical-compliance.md) as a tracked-issue follow-up. |
| Tier ordering change in `_inject_memory_context` perturbs the RFC 0017 token-bound contract. | PR 3 retains the 1500-token allocator budget unchanged; the facts slot is a per-tier floor enforced at the call site. The existing RFC 0017 token-bound tests still pass; PR 3 checklist enforces it. |
| PR 1 opens before RFC 0031 PR plan PR 3 merges → facts table created without `session_id` column → non-additive migration later. | Strict cross-RFC merge ordering pinned at the top of this plan and in [v0.3.1-plan §Phase 2 workstream sequencing](../v0.3.1-plan.md#phase-2--implement-the-two-rfcs). Reviewers reject PR 1 if [RFC 0031 PR plan PR 3](0031-pr-plan.md) is not on `main`. |
| Header tokens not charged against the budget — repeat of RFC 0017 PR 2 finding #2. | PR 3 adds the header via `budget.try_add(header)` inside the `if items:` block, not as a free prepend. Explicit checklist item; test in `test_facts_recall.py` pins the `result.memory_admitted_tokens` invariant. |
| MT-MEMORY-005 expected-outcomes table updated in PR 4 without running the test means a false-green claim. | PR 4 updates the *expected*-outcomes column only. Actual test execution is owned by [v0.3.1 release-prep Phase 4 PR 1](../v0.3.1-plan.md#phase-4--v031-release-prep-execution); the gate to flip Legs 1 / 2 / 5 from "expected pass" to "passed" is empirical, not documentary. |

---

## ROADMAP Hygiene

Per [.github/copilot-instructions.md](../../.github/copilot-instructions.md) "Status Hygiene":

- **PR 1 opens** → RFC 0026 → `🚧 Implementing`; [v0.3.1-plan Master Progress Overview](../v0.3.1-plan.md#master-progress-overview) row 2 → 🔄 In progress.
- **Each PR merges** → fill the [Progress Overview](#progress-overview) row.
- **PR 6 merges** → RFC 0026 → `✅ Implemented`; master-plan row 2 → ✅; `Last updated` refresh.

---

## Progress Overview

| # | Title | Branch | Status | GitHub PR | Merged |
|---|-------|--------|--------|-----------|--------|
| 1 | Facts schema + FactStore + erasure primitive | `feature/v031-rfc0026-facts-schema-store` | ⬜ Not started | — | — |
| 2 | Extractor + predicate allowlist + audit | `feature/v031-rfc0026-extractor` | ⬜ Not started | — | — |
| 3 | Recall + MemoryBudget tier slot + config | `feature/v031-rfc0026-recall-budget` | ⬜ Not started | — | — |
| 4 | Reinforcement + retraction + tier provenance + MT update | `feature/v031-rfc0026-reinforcement-retraction` | ⬜ Not started | — | — |
| 5 | Review follow-ups | `feature/v031-rfc0026-followups` | ⬜ Not started | — | — |
| 6 | RFC close | `feature/v031-rfc0026-close` | ⬜ Not started | — | — |

---

## Related Documentation

- [RFC 0026 — Declarative Facts Tier](0026-declarative-facts-tier.md) — canonical spec.
- [RFC 0031 PR plan](0031-pr-plan.md) — Phase 1 column-convention dependency.
- [RFC 0017 — Persona Memory Injection Budget](0017-persona-memory-injection-budget.md) — `MemoryBudget` allocator consumer.
- [RFC 0020 — Interaction Lifecycle](0020-interaction-lifecycle.md) — interaction-close hook the extractor wires into (RFC 0020 PR 4 summarize-on-close).
- [RFC 0013 — Legal & Ethical Compliance](0013-legal-ethical-compliance.md) — `SubjectErasure` (target v0.5.0) will wire `FactStore.delete_by_subject` into the umbrella traversal.
- [RFC 0008 — Agent Memory & Context Optimization](0008-agent-memory-context-optimization.md) — reinforcement formula source.
- [RFC 0027 — Reflection-Driven Consolidation](0027-reflection-driven-consolidation.md) — §F end-state tier ordering.
- [Memory Quality Roadmap §A](../memory-quality-roadmap.md#a-promote-key_facts-to-a-declarative-fact-tier) — design rationale and dementia-test framing.
- [MT-MEMORY-005 — Dementia test](../manual-tests/MT-MEMORY-005-dementia-test.md) — Phase 3 acceptance gate (Legs 1, 2, 5).
- [v0.3.1-plan.md](../v0.3.1-plan.md) — master plan.
