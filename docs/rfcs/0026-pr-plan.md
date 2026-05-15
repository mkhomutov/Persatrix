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

- **OQ #1 — predicate vocabulary scope.** Small + extensible. PR 2 authors a ≈25-verb allowlist across attribute / preference / commitment / relationship + self-* (per OQ #10). Relationship verbs are gender-neutral (`has_child_named`, not `has_son_named` / `has_daughter_named`) — the flat triple shape cannot carry the gender axis without leaking schema gap into the vocabulary (see [RFC §B](0026-declarative-facts-tier.md#b-extraction-at-interaction-close)). Later additions land via PR amendment to the RFC; rejected-verb telemetry (next bullet) is the data-driven feeder for what to amend.
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

**Recommended merge order**: **PR 1 → PR 2 → PR 3 → PR 4 → PR 5a → PR 5b → PR 5c → PR 5d → PR 5e → PR 6**. PR 5 was sliced into five sub-PRs once the aggregate deferred surface from PR 1 → PR 4 reviews crossed three concern boundaries — see [PR 5: Review Follow-Ups](#pr-5-review-follow-ups--sliced-into-pr-5a--pr-5e) for the slice table. PR 6 (RFC close) depends on every slice merged.

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
PR 5a (PR 1 review — symmetric latest-asserted-wins + source_interaction_id nullability)
  ↓
PR 5b (PR 2 review — envelope parse-failure observability)
  ↓
PR 5c (PR 3 review — storage / render defensive fixes)
  ↓
PR 5d (PR 3 review — tests + counter polish)
  ↓
PR 5e (PR 4 review — audit, chunking, edge cases)
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
| `agents/memory/facts.py` | Audit log entry per `store` and per `supersede` write. **Python audit-log surface to be selected during PR 2 implementation** — there is no shipped `agents/observability/audit.py` today and RFC 0009's `AuditLogger` lives Go-side ([`internal/security/audit.go`](../../internal/security/audit.go)) without a Python sibling. Default surface is the existing [`agents/observability/logging.py`](../../agents/observability/logging.py) structured logger with an explicit `audit=true` field (small surface, ships today); PR 2 review may swap in a dedicated Python audit module if one materialises before then. **Redaction**: enforce the same policy as Go's [`RedactStruct`](../../internal/security/redactor.go) (RFC 0009 §I), implemented Python-side via the existing [`Redactor`](../../agents/observability/redact.py) protocol — keeps raw PII out of audit metadata (per [§Security Considerations](0026-declarative-facts-tier.md#security-considerations)). RFC 0026 [§G Audit and provenance](0026-declarative-facts-tier.md#g-audit-and-provenance) names `RedactStruct` as the cross-language policy anchor; this row pins the Python-side mechanism. |
| `tests/unit/python/test_extractor.py` | **New** — empty-list path (short interaction → zero tuples); predicate-allowlist rejection; subject canonicalization rules; supersede dispatch on `(subject, predicate)` collision with existing row; partial-failure rollback matrix (summary fails → no write; facts fail → summary writes, counter increments). |

#### Key implementation details

- The extractor prompt enumerates the predicate vocabulary inline and instructs the model to return `[]` when no extractable facts are present — the common short-interaction case. System-prompt and user-prompt lives alongside the existing summary prompt in `summarize_close.py`. Single LLM call.
- Predicate allowlist seed (final list locked in PR review):
  - Attribute: `has_name`, `lives_in`, `works_at`, `has_age`, `speaks_language`.
  - Preference: `prefers`, `dislikes`, `loves`, `avoids`.
  - Commitment: `committed_to`, `plans_to`, `agreed_to`.
  - Relationship: `has_child_named`, `has_partner_named`, `has_parent_named`, `works_with`, `knows`. The earlier draft of this plan named `has_daughter_named` / `has_son_named` as separate verbs; PR 2 review collapsed those into the gender-neutral `has_child_named` so the flat `(subject, predicate, object)` triple shape does not need to grow one verb per (relation × gender) — see [RFC §B](0026-declarative-facts-tier.md#b-extraction-at-interaction-close).
  - Self-* (OQ #10): `self.has_preference`, `self.holds_value`, `self.committed_to`, `self.has_attribute`.
- **Rejected-predicate discovery telemetry.** On allowlist miss, the extractor records the verbatim post-normalisation verb to the structured-log surface via the `persatrix.facts.rejected_predicate` field (separate from the per-tuple WARNING that carries the full raw dict, which is too PII-laden and noisy for aggregation). Per-process dedup keeps each distinct verb to one record so the discovery surface is the unique vocabulary, not a per-tuple repeat; an in-process cap (256 distinct strings) bounds memory against a pathological LLM emitting unique-per-call garbage. The counter `agent.facts.extraction_failed` remains unchanged — it counts rejections, the new log surfaces *which* verbs were rejected for growing the allowlist from observed workload (see [RFC §B](0026-declarative-facts-tier.md#b-extraction-at-interaction-close)).
- Subject canonicalization: counterparty → `sender_id`; existing canonical reuse; otherwise normalize (case- and whitespace-fold) and store. Self uses literal `"self"` per §C.4.
- Close-path sequencing: `EpisodicMemory.update_episode_summary` commits first (its own `BEGIN/COMMIT`); `FactStore.store` calls follow, each in its own per-row transaction. Envelope (JSON) parse failure → summary commits as raw text, facts dispatch skipped, no counter bump at this layer (see [PR 5 follow-ups — combined-envelope truncation observability](#from-pr-2-review)). Per-tuple failure → `facts.extraction_failed` += 1 per tuple, batch continues. Summary commit failure → facts dispatch skipped (janitor backfills the summary on next sweep). Matches [§Phase 1 step 4](0026-declarative-facts-tier.md#phase-1-schema--extractor) — the earlier "single `BEGIN ... COMMIT`" wording was reconciled in PR 2 review because each tier owns its own `aiosqlite` connection (see [agents/persona.py](../../agents/persona.py) FactStore comment), and a literal cross-tier transaction wrap is not implementable at this layer.
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
| `agents/observability/metrics.py` | Counter `facts.injected{tier=facts}` increments per admitted fact. **`metrics.py` is at the 500-line cap exactly after PR 1** (review-friendly soft limit from `scripts/checks/file_size.py`); add the new counter via the existing `_metrics_facts.register(self, meter)` extension seam rather than inline in `_Instruments.__init__` so the cap is preserved.  Same pattern PR 1 established for `facts.{stored,superseded,extraction_failed}`. |
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
| [`agents/persona_runtime/facts_section.py`](../../agents/persona_runtime/facts_section.py) | **Add `"self"` to `_subject_seeds`** so introspective `self.*` facts (the OQ #10 predicate class — `self.has_preference`, `self.holds_value`, `self.committed_to`, `self.has_attribute`) admit at recall time.  PR 3 ships extractor-side writes to `subject="self"` but seeds only from `event.sender_id`, leaving introspective rows write-only; the `last_recalled_at` reinforcement write this PR adds must fire on those rows too for MT-MEMORY-005 Leg 5 (self-consistency) to flip green.  **Fan out the `render_facts_section` shape**: PR 3 ships a single-subject header (`f"Known facts about {facts[0].subject}:\n"`) under the Phase-1 single-seed invariant; once the seed list grows to two, the section needs one block per subject (or a pluralised header) so a mixed-subject `facts` list does not silently mislabel `self.*` rows under the sender's header.  Pin with a test that stores a `self.has_preference` row and asserts admission at the next event, plus a multi-subject test that asserts the rendered section labels each subject's facts correctly.  Deferred from PR 3 review (see PR 5 §From PR 3 review). |
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

### PR 5: Review Follow-Ups — sliced into PR 5a → PR 5e

**Depends on**: PR 4 merged.
**Purpose**: Apply review findings from PRs 1–4. Follows the [RFC 0017 PR plan §PR 6 precedent](0017-pr-plan.md) — "From PR N review" subsections, each finding paraphrased inline (no link to local review reports per [.github/copilot-instructions.md](../../.github/copilot-instructions.md)).

#### Why this PR is sliced

The aggregated deferred surface from the PR 1 → PR 4 reviews crosses three concern boundaries (storage primitive, recall-side rendering, telemetry / audit) and several thousand lines of test coverage. Landing it as one PR would push individual files past the 500-line review-friendly cap and bundle independent decisions into one review thread. PR 5 ships as **five tightly-scoped slices** (5a → 5e), each closing one concern and ≲ 500 LOC:

| Slice | Branch | Scope |
|-------|--------|-------|
| **PR 5a** | `feature/v031-rfc0026-followups-pr1` | From PR 1 review — storage primitive: symmetric latest-asserted-wins + `source_interaction_id` nullability lock. |
| **PR 5b** | `feature/v031-rfc0026-followups-pr2` | From PR 2 review — combined-envelope parse-failure observability (truncated JSON + missing-summary-key paths). |
| **PR 5c** | `feature/v031-rfc0026-followups-pr3a` | From PR 3 review — storage / render defensive fixes: L-1 header truncation, L-2 canonicalize in `FactStore.store`, L-3 null-budget guard, N-2 `recall_facts_for_event` cleanup, `extraction_model` config decision. |
| **PR 5d** | `feature/v031-rfc0026-followups-pr3b` | From PR 3 review — tests + counter polish: negative-path coverage, `TestTierOrdering` at prompt boundary, `tier="facts"` attribute decision, `agent.facts.injected` overcount fix. |
| **PR 5e** | `feature/v031-rfc0026-followups-pr4` | From PR 4 review — reinforcement audit (DR2-N-2), IN-list chunking (DR2-N-3), `at=0.0` edge cases (DR3-L-3), `enc.encode` caching (DR2-N-6), commit-cost calibration (DR2-N-8). |

Each slice opens its own PR and updates the Progress Overview row for PR 5 separately. PR 6 (RFC close) depends on **all five slices merged**.

#### Scope (across all slices)

##### From PR 1 review

The bulk of PR #339's review findings landed in PR 1 itself (docstring
refreshes, validation reordering, frozen `Fact`, `delete_by_subject` +
input-validation backfill tests, `agents/memory/__init__.py` re-exports).
The two items deferred from PR 1 review **both shipped in PR 5a** —
`feature/v031-rfc0026-followups-pr1`:

- **Symmetric latest-asserted-wins on insert.** PR 5a tightened the
  supersede-on-insert SELECT from `asserted_at < ?` (strict less-than)
  to `asserted_at <= ?` and added a forward-pass that finds any
  strictly-newer live row to mark the new row as self-superseded.
  Net effect: only one row per `(agent_id, subject, predicate)` stays
  live regardless of insert order, and equal-timestamp ties break in
  favour of the newer arrival.  The chain logic lives in the new
  [`agents/memory/_facts_supersede.py`](../../agents/memory/_facts_supersede.py)
  helper so [`agents/memory/facts.py`](../../agents/memory/facts.py)
  stays under the 500-line cap.  The earlier
  `TestAssertedAtMonotonicity` precondition pin was replaced by the
  symmetric-semantics test class
  [`TestSymmetricLatestAssertedWins`](../../tests/unit/python/test_fact_store_supersede.py)
  — six cases: chronological writes, older-arrival self-supersedes,
  equal-timestamp later-arrival wins, three-write out-of-order chain,
  cross-predicate isolation, per-agent ACL.  RFC §F was amended in the
  same PR to describe the symmetric shape.
- **`source_interaction_id` nullability.** Decision locked in PR 5a:
  amend RFC §A to permit `NULL` rather than tighten the column.
  Rationale: three legitimate callers (test fixtures, future RFC 0013
  erasure backfill, OQ #9 operator-seeded path) commit rows without a
  source interaction; tightening would force them to fabricate a
  synthetic id.  The audit-log surface still records the field (as
  `NULL` when absent) so provenance traceability is preserved on the
  production write path.  RFC §A amendment landed in PR 5a; `Fact`
  dataclass docstring updated to point at the amendment.

##### From PR 2 review

Most PR 2 review findings landed inline in PR 2 itself (RFC §Phase 1
step 4 sequencing wording reconciled with the implementation; the
misleading `FactStore` "reuses EpisodicMemory connection" comment in
[`agents/persona.py`](../../agents/persona.py) corrected; the
`canonicalize_subject` ASCII-only `.lower()` swapped for Unicode-aware
`.casefold()` so non-ASCII counterparties — e.g., German `ß` — do not
silently split across two `facts.subject` rows).

Deep-review (PR #340) findings that **also** landed inline in PR 2:

- **S2 — empty-summary envelope falls back to placeholder.** A well-
  formed JSON envelope with an empty (or whitespace-only) `summary`
  field used to commit `""` to the episode summary column *and* let
  the facts half dispatch against a missing prose half, violating
  the §G audit ordering ("the summary always exists before any
  `facts.store` row pointing back at this `interaction_id`"). The
  fix at the caller layer in
  [`summarize_close.py`](../../agents/persona_runtime/summarize_close.py)
  detects the empty `summary` field post-parse, emits
  `agent.interactions.summary.failed{reason=empty_field}`, and
  returns `(SUMMARY_UNAVAILABLE_TEXT, True, None)` — the
  `not summary_failed` gate in `finalize_closed_interaction` skips
  the facts dispatch jointly. Raising inside
  `split_combined_response` would have been caught by the backward-
  compat branch above and committed the raw JSON envelope as the
  summary text — *worse* than the pre-fix behaviour — so the check
  belongs at the caller, not the splitter. Pinned by
  `TestEmptySummaryFieldFallsBack` (unit) and
  `TestExtractorEmptySummaryEnvelope` (integration).
- **S3 — whitespace `sender_id` normalised at the function boundary.**
  `store_extracted_facts` precomputes `canonical_sender =
  canonicalize_subject(sender_id) if sender_id else None` *before*
  the per-tuple try-block; a whitespace-only `sender_id` ("   ")
  satisfies the truthiness check and reaches `canonicalize_subject`,
  which raises `ValueError("subject must not be empty")`. The
  exception escapes the helper, is caught by
  `dispatch_facts_from_response`'s broad `except Exception`, and
  drops the entire batch with no per-tuple
  `agent.facts.extraction_failed` increment — silent data loss
  indistinguishable from "no facts extracted." Fix is a one-line
  strip-at-boundary so a whitespace-only value collapses to `None`;
  reachability via the production close-path is low because
  `_interaction_sender` already filters whitespace, so this is
  defence-in-depth for direct callers (test fixtures, the future
  RFC 0026 OQ #9 operator-seeded write path). Pinned by
  `TestWhitespaceSenderIdNormalisedAtBoundary`.

The items deferred to this PR are:

- **Combined-envelope parse-failure observability — collapsed paths.**
  The caller's `try: split_combined_response(text) except FactsParseError:
  return (text, False, None)` block in
  [`agents/persona_runtime/summarize_close.py`](../../agents/persona_runtime/summarize_close.py)
  collapses three distinct failure shapes into one outcome:
  1. **Plain prose response** — desired backward-compat path (older
     mock clients, legacy LLM responses without the JSON envelope).
     Commits the prose as the summary; facts half is `None`. Correct.
  2. **Truncated JSON envelope** (PR #340 review) — the combined call
     shares the RFC 0020 PR 4 `max_tokens=256` cap tuned for the
     prose-only summary (~50–100 tokens); a 5-fact response is
     ~200–280 tokens, uncomfortably close to the cap. Mid-array
     truncation yields invalid JSON that the catch falls through to
     "commit as raw text" — broken JSON lands in the summary column;
     facts silently lost; no counter, no log.
  3. **Valid JSON object missing the `summary` key** (PR #340 deep-
     review S1) — `{"facts": [...]}` parses as a mapping but
     `split_combined_response` raises `FactsParseError("combined
     response missing required `summary` key")`. Same catch, same
     fall-through; the raw JSON envelope commits as the summary
     text. Indistinguishable from path (1) at the surface; the
     observability gap is identical to path (2).
  The PR #340 deep-review S2 case (well-formed envelope with
  empty `summary` field) was a fourth member of this cluster but
  has its own signal as of PR 2:
  `agent.interactions.summary.failed{reason=empty_field}` fires
  from a post-parse check at the caller. Paths (2) and (3)
  remain unsignalled — they are this follow-up's scope.
  Two options, pick one in PR 5 review:
  1. **Bump the combined-call cap** (e.g., 512) and **distinguish
     "valid-envelope-shape-but-not-prose" at the catch site** —
     simplest for path (2); needs the catch-site change to cover
     path (3). Costs tokens even on summary-only paths.
  2. **Add an `agent.facts.envelope_parse_failed` counter** with a
     `reason` attribute (`truncated` for path (2), `missing_summary`
     for path (3)) emitted at the catch site — distinguishes the
     three shapes deterministically (path (1) is "no JSON brace at
     all" or "valid JSON envelope round-trip"; the others trigger
     the counter with their distinct reasons). Keeps the cap tight,
     makes both failure paths observable separately from each other
     and from the "no facts" case.
  Test pins should assert the chosen signal fires for **both** the
  deliberately-truncated envelope **and** the missing-`summary`-key
  envelope, so a future regression in either path surfaces.
- **`:memory:` cross-tier `JOIN` support (optional refactor).** The
  PR 2 review noted that `FactStore` and `EpisodicMemory` each open
  their own `aiosqlite` connection (see PR 1's `shared_db` seam and
  the `FactStore(... shared_db=None)` call site in
  [`agents/persona.py`](../../agents/persona.py)); for file DBs the
  connections share the file and joins work, for `:memory:` test paths
  each connection is isolated and a `facts × episodes` join returns
  empty. No PR-2 caller relies on that join. Track here in case a PR 3
  recall test or PR 4 retraction-policy test eventually needs it —
  then thread the `EpisodicMemory` connection through the existing
  `shared_db` seam and add a regression test that pins the join works
  under `:memory:`. Until such a caller exists, defer.
- **Per-agent dimension on the rejected-predicate discovery log.**
  PR #340 review N3 — the `_REJECTED_PREDICATES_SEEN` dedup set in
  [`agents/persona_runtime/fact_extractor.py`](../../agents/persona_runtime/fact_extractor.py)
  is module-global. In a multi-tenant deployment where one process
  hosts multiple persona agents, the first agent that hits a rejection
  swallows the second agent's identical rejection — the
  `persatrix.facts.rejected_predicate` structured field is the
  process-wide unique vocabulary, not a per-agent surface. The
  per-tuple WARNING still carries the agent dimension via the
  RFC 0018 logging contextvars (`agent_id`), so the per-agent signal
  is recoverable from the log pipeline by joining the structured-
  field record against the contiguous per-tuple WARNING by
  `interaction_id`. Two options when PR 3 / PR 4 actually surface
  multi-tenant rejection data:
  1. **Defer indefinitely.** The per-tuple WARNING already carries
     `agent_id`; downstream aggregation can group there.  Process-
     scoped dedup keeps the discovery surface tight.
  2. **Widen the dedup key to `(agent_id, predicate)`.** Trade more
     log volume (one record per agent per verb instead of one per
     process per verb) for direct agent-attribution on the
     structured-field surface. Bump the cap proportionally so the
     in-process memory bound still holds against a pathological
     multi-tenant deployment.
  Trigger: when PR 3 / PR 4 telemetry shows a recurring near-miss
  verb pattern that operators want to attribute to a specific agent
  without joining log streams. Until that demand exists, defer.
- **Redactor idempotence — sentinel-based short-circuit.** PR #340
  deep-review S4. `_facts_audit.emit_audit` runs the registered
  `Redactor.redact()` explicitly, then `_logger.info(event,
  extra=payload)`. Once stdlib's `ProcessorFormatter.foreign_pre_chain`
  runs (post-`configure_logging`), the structlog chain's
  [`_apply_redactor`](../../agents/observability/logging.py)
  processor redacts the same record a second time. This is **by
  design** — the
  [`Redactor.redact` contract](../../agents/observability/redact.py)
  requires idempotence, which the `NoopRedactor` (v0.3.x default)
  trivially satisfies. The design relies on every future PII
  redactor being idempotent without enforcement. When a real
  redactor lands, add a sentinel marker (e.g., `_redacted: True`
  inside the payload) and short-circuit on re-application at the
  chain's redactor seam — the contract docstring already calls this
  out as the recommended pattern, but the actual implementation has
  no signal today. Trigger: when a non-`NoopRedactor` redactor
  lands in-tree (likely v0.4.x alongside the PII work tied to
  RFC 0013 §C). Until then, the test pin
  `TestRedactorFailureWarning` exercises the failure surface — the
  idempotence contract itself is documented but unguarded.
- **Predicate-vocabulary scope — non-person / world facts.** PR 2's
  allowlist (attribute / preference / commitment / relationship +
  `self.*`) is anthropocentric — every class assumes the subject is a
  person or agent. Facts the persona may acquire about the world
  (historical events, scientific knowledge, places, routines,
  observations) have no home in the current vocabulary. The
  recommendation is **not** to widen the existing classes — both the
  prompt-injection blast-radius bound (RFC §Security) and the §H
  erasure semantics depend on the allowlist staying scoped to
  relational state. Instead, when PR 3's recall path makes it
  observable that some class of stable non-person facts is missing
  from injection, evaluate three options in order:
  1. **LLM prior.** Stable world-knowledge (`"water boils at 100°C"`,
     `"WW2 ended in 1945"`) costs tokens to store and buys nothing the
     model does not already know; the default answer for this class is
     "do not store."
  2. **Episodic memory.** Time-stamped observations the persona made
     (`"Bob mentioned the library closes at 9pm"`) already have a home
     in episodes — the temporal frame is load-bearing for that class
     and a flat triple drops it.
  3. **Separate `world.*` predicate namespace** *if* PR 3 recall
     surfaces a class of stable, persona-relevant observations
     episodic cannot serve (e.g., `world.place_open_hours`,
     `world.event_occurred_on`). Keep the namespace enumerated and
     small, same blast-radius discipline as `self.*` from OQ #10.
     Reject generic predicates (`has_property`, `occurred_at`) with
     structured objects — they neutralise the allowlist as a security
     boundary and give recall nothing to key on.
  Trigger: revisit when PR 3 recall is in dogfood and the
  `persatrix.facts.rejected_predicate` discovery log (PR 2) shows a
  coherent non-person verb cluster the model keeps trying to emit.
  Until that data exists, defer — the current shape may be right.

##### From PR 3 review

Deep-review (PR #341) findings that **landed inline in PR 3**:

- **M-2 — section header names the subject, not the persona.** PR 3
  initially shipped `"Known facts about you:\n"` as the
  `facts_context` section header.  Because PR 3 admits facts about
  the canonical sender (the counterparty), the literal `"you"`
  reads to the LLM persona as a claim about *itself* — a row like
  `- bob has_child_named Mira` invites the model to interpret
  Mira as the persona's child, the exact persona-inversion
  footgun the dementia test is meant to fence off.  Fix folded
  into PR 3: header is now `f"Known facts about {subject}:\n"`
  derived from `facts[0].subject` (the canonical storage form
  already used at the row's join key).  Phase 1 invariant: every
  admitted fact shares one subject because `_subject_seeds`
  yields a single canonical sender; multi-subject seeding (see
  next bullet) will need to fan the section shape out — pinned in
  PR 4 scope below.  Pinned by `TestHeaderSubjectTemplated` in
  [`tests/integration/test_facts_recall.py`](../../tests/integration/test_facts_recall.py).
- **M-1 — canonical tier-order regression test was undercovered for
  `facts_context`.** `tests/unit/python/test_memory_context_priority_order.py`
  filtered `add_section_order` against a hard-coded four-tier list
  (`relationship_context`, `channel_history`, `episodic_recall`,
  `recent_notes`); the post-PR-3 `facts_context` section was added
  to `add_section_order` by the runtime but silently dropped by the
  filter so a future refactor that moved facts to a different slot
  (or out of the loop entirely) slipped past the pin.  The deep-
  review surfaced this as distinct from the deferred `TestTierOrdering`
  follow-up below (different file, different concern — the
  integration test pins one shape, the unit-priority test pins the
  canonical cross-RFC order).  Fix folded into PR 3: a
  `seeded_fact_store` fixture wires a real
  [`FactStore`](../../agents/memory/facts.py) with one stored fact
  about the canonical sender so `facts_context` admits; the
  `expected` list now reads `relationship → channel_history → facts
  → episodic → notes`; the TICK test asserts `facts_context not in
  order` mirroring the existing `channel_history not in order` shape
  (the `_subject_seeds` short-circuit on `sender_id=None`).
- **N-1 — superfluous `# type: ignore[attr-defined]` in
  `test_facts_recall.py` fixture.** The class-level annotations on
  `_MemoryContextMixin` (`_fact_store: FactStore | None`,
  `_facts_enabled: bool`, `_facts_budget_tokens: int`) at
  [`memory_context.py`](../../agents/persona_runtime/memory_context.py)
  make the three attributes type-checker visible from the test
  fixture; the `# type: ignore[attr-defined]` comments on the
  assignment lines were leftover from an earlier shape before the
  defaults landed.  Dropped in PR 3 so the next reader does not
  assume the attributes are private to `__init__`.
- **N-3 — schema description for `memory.facts.extraction_model`
  overstates support.**  The earlier wording ("Phase 1 ships the
  config surface; the PR 2 extractor honours it implicitly via the
  inherited summariser model") invited operators to set the knob
  expecting an override; the field has zero Python readers (the
  combined-call wiring at
  [`summarize_close.py`](../../agents/persona_runtime/summarize_close.py)
  reads only `optimization.yaml`).  Description updated in PR 3 to
  state the field is reserved with no v0.3.1 consumer and points
  at PR 5 for the final-shape decision (drop / plumb / warn — see
  the `extraction_model` deferred bullet below).  Independent of
  PR 5's decision; the description fix is correct regardless of
  which option lands.

The items deferred to this PR are:

- **`agent.facts.injected` overcount when the section is dropped on
  header admission failure.** PR 3 increments the
  `agent.facts.injected` counter inside the per-item budget loop in
  [`render_facts_section`](../../agents/persona_runtime/facts_section.py),
  but the section is only added to working memory if the
  `"Known facts about {subject}:\n"` header *also* admits — and the
  header `try_add` runs **after** the loop.  When the per-item
  passes drain the budget to `<= MIN_TOKENS_FACTS` and the header
  subsequently fails admission, the function returns `None`, no
  section reaches the prompt, but the counter has already ticked
  once per admitted item — silently violating the docstring
  contract ("counter reflects what reached the prompt, not what
  the recall layer returned") inherited from PR #260 review M-1.
  Latent today because the relationship + channel_history tiers
  rarely leave the global allocator close enough to its floor for
  the header pass to fail.  Two implementation options to pick in
  PR 5 review:
  1. **Reserve the header tokens up-front.** Move the header
     `try_add` to *before* the per-item loop; if the header itself
     cannot be admitted, return `None` immediately with no
     counter writes and no items consumed.  Aligns the
     cost-attribution order with the read order of the rendered
     section.  Cost: the per-tier soft-slice
     (`facts_budget_tokens`) accounting becomes slightly
     trickier — the header eats a slice the items had counted on.
  2. **Defer the counter writes until after `add_section`
     succeeds.** Keep a local pre-add tally inside the function
     and emit one `facts_injected.add(N, ...)` call after the
     header lands.  Keeps the per-item ordering as-is; trades one
     batched `add(N)` for N individual `add(1)` calls (no
     observable behaviour change for OpenTelemetry counter
     semantics).  Pairs naturally with PR 4's tier-provenance
     instrumentation since it already needs to record admitted
     items in a structured shape.
  Test pin should pre-fill `MemoryBudget` so the loop admits N
  fact lines but leaves no room for the header, then assert
  `counter_total(reader, "agent.facts.injected") == 0` *and*
  `get_section("facts_context") is None`.

- **`self.*` subject seed — extend `_subject_seeds` to include
  `"self"`.** PR 2's extractor can write `subject="self"` rows
  for the OQ #10 introspection predicates
  (`self.has_preference`, `self.holds_value`, `self.committed_to`,
  `self.has_attribute`); PR 3's `_subject_seeds` derives seeds
  only from `event.sender_id`, so introspective facts are
  write-only until the seed list grows.  The seam in
  [`facts_section.py`](../../agents/persona_runtime/facts_section.py)
  is narrow (a one-line append in `_subject_seeds`), but the
  feature sequences naturally with PR 4's reinforcement /
  retraction work because the `last_recalled_at` write must fire
  on `"self"` rows too for the MT-MEMORY-005 Leg 5
  (self-consistency) outcome to flip green.  Per the PR 4 scope
  table below, this PR formally moves "self-subject seed" into
  PR 4 acceptance.  Pinned by an explicit test that stores a
  `self.has_preference` row and asserts it admits at the next
  event with the same agent.

- **`memory.facts.extraction_model` is a config knob with no
  consumer.** [`config/agents.yaml`](../../config/agents.yaml)
  and [`schemas/agent.schema.json`](../../schemas/agent.schema.json)
  ship the field; no Python code reads it.  PR 5 picks one of
  three options:
  1. **Drop the field.** Smallest surface — until a real
     consumer exists, the knob is a footgun (operator sets
     `extraction_model: "claude-haiku-4-5"` expecting an
     override; gets a silent no-op).  Schema-removal is
     additive-compatible (existing configs without the key are
     still valid); existing configs *with* a non-null value
     would fail `additionalProperties: false` validation, which
     is the correct failure mode (an operator who wrote that
     value would otherwise believe it was honoured).
  2. **Plumb it through `summarize_close.py`.** Read the field
     in [`summarize_close.py`](../../agents/persona_runtime/summarize_close.py)
     and pass it as the `model=` override on the combined
     summarize + extract LLM call when non-null; fall back to
     the inherited `optimization.yaml` summariser model
     otherwise (preserves OQ #5).  Highest fidelity to the RFC
     wording — gives operators a per-persona extraction-model
     knob distinct from the summariser default.
  3. **`logger.warning` at config-load time when set non-null.**
     Cheapest non-disruptive option; turns a silent no-op into a
     loud one-record-per-startup signal without changing the
     schema surface.  Defensible as a hold-over while a real
     consumer lands, but not a steady state — pick (1) or (2)
     for the final shape.
  Decision lock during PR 5 review.

- **`tier="facts"` counter attribute is a constant in PR 3.**
  Every `agent.facts.injected` increment carries the same
  `tier="facts"` value — the dimension adds zero cardinality at
  Phase 1.  The justification at
  [`agents/observability/_metrics_facts.py`](../../agents/observability/_metrics_facts.py)
  is forward-compat with PR 4's per-turn tier-provenance
  dashboard, which adds `tier=` to the other tier counters
  (`agent.episodic.retrieved`, `agent.notes.injected`, …) so they
  all join on the same attribute key.  PR 5 picks one:
  1. **Add a one-line comment naming PR 4 as the consumer** so a
     future operator reading the metric definition does not
     assume the attribute is alive.  Keeps the forward-compat
     surface intact at zero behaviour cost.
  2. **Drop the attribute until PR 4 emits the second value.**
     Cleaner Phase-1 surface; PR 4 re-adds the attribute when
     it acquires meaningful cardinality.  Risk: dashboards
     written against `tier="facts"` between PR 3 and PR 4 break
     when the attribute reappears with a different lineage.
  Default in absence of operator demand: option (1).

- **Negative-path test coverage in
  [`tests/integration/test_facts_recall.py`](../../tests/integration/test_facts_recall.py).**
  PR 3 ships 9 tests (post-M-2 fold-in), covering the happy
  paths and the dementia-core leg, but three negative-path
  branches are unpinned:
  1. **`fact_store.recall` raises.** `recall_facts_for_event`
     catches `Exception`, logs `WARNING`, and returns `[]` —
     the log-and-continue idiom that parallels the relationship
     and episodic tiers.  Both tiers have explicit negative-path
     tests; the facts tier does not.  Add a test with
     `AsyncMock(side_effect=RuntimeError(...))` and assert the
     section is absent and the dementia-core invariant on other
     events is unaffected.
  2. **TICK / orchestrator event with no sender.**
     `_subject_seeds` returns `[]` when `event.sender_id` is
     `None` / empty / whitespace-only; `recall_facts_for_event`
     short-circuits.  Pin with an explicit `sender_id=None`
     event.
  3. **Header-dropped case** (pairs with the M-1 follow-up
     above) — also pins the counter-overcount regression.

- **`TestTierOrdering` asserts `add_section` call order, not the
  rendered prompt order.** The test wraps `add_section` and
  asserts the call sequence is `relationship → facts → notes`.
  That validates the allocate-loop's insertion order, but the
  actual prompt order is determined by
  `WorkingMemory.build_context`'s stable sort by `priority`
  (descending).  Today facts (7), channel_history (7), and
  episodic (7) share priority and Python's stable sort breaks
  ties on insertion order, so the test invariant matches the
  prompt output by happy coincidence.  If a future PR nudges
  `FACTS_SECTION_PRIORITY` to 6, the existing test still passes
  but the prompt order silently flips facts below notes — a
  regression the dementia-core leg would catch only on the
  Mira-style follow-up where notes prose displaces a fact.
  Either:
  1. **Assert against `_working_memory.build_context()` output**
     so the priority sort is exercised end-to-end.
  2. **Add a second test that snapshots `build_context()`** and
     pins the rendered tier order at the prompt boundary; keep
     the existing `add_section` test as a separate insertion-
     order pin.
  Default: option (2) — the two contracts (allocate order vs
  render order) are distinct and worth pinning separately so a
  future regression report points at the right layer.

- **L-1 — header truncation can elide the trailing `:\n`
  separator.**
  [`render_facts_section`](../../agents/persona_runtime/facts_section.py)
  builds the header as `f"Known facts about {subject}:\n"` and
  passes it to `MemoryBudget.try_add(min_tokens=MIN_TOKENS_FACTS)`.
  `try_add` returns either the original text, a truncated form
  with `…` ellipsis (when `count(text) > remaining` and
  `truncated_tokens >= min_tokens`), or `None`.  When the
  canonical subject is long enough (≈60+ chars) that the full
  header exceeds `remaining` but the truncated form still meets
  the 24-token floor, the returned header is something like
  `"Known facts about very_long_user…"` — the trailing `:\n` was
  the last thing in the source string and is lost to truncation.
  The subsequent `admitted_header + "\n".join(items)` then glues
  the first item directly to the truncated header with no
  separator: `"Known facts about very_long_user…- bob
  has_child_named Mira"`.  Reachability bound is narrow (long
  subject AND tight remaining at the point the header is
  admitted), so not a Phase-1 blocker, but worth a defensive fix.
  Three options for PR 5 to pick from:
  1. **Two-part header admission.** Admit `"Known facts about
     {subject}:"` (subject-bearing, may truncate) plus a separate
     guaranteed `"\n"` admission (≈1 token).  Costs one extra
     `try_add` on the happy path.
  2. **Drop on truncation.** After
     `admitted_header = budget.try_add(...)`, compare to the
     input string; if the returned form ends in `…` (truncated)
     return `None` instead of rendering a malformed section.
     Simplest; loses the section in a recoverable case.
  3. **Cap subject length in `canonicalize_subject`.** E.g., 200
     chars so the header can never exceed ≈50 tokens.  Out of
     scope for `facts_section`; touches the storage primitive
     ([`agents/memory/fact_predicates.py`](../../agents/memory/fact_predicates.py)).
     Pairs with L-2 below if both are taken together.
  Default: option (1) — keeps the rendering invariant intact
  without dropping recoverable sections, and the cost is one
  extra `try_add` only on the unhappy path.  Pin with a test
  that constructs a 250-char canonical subject, pre-fills the
  budget to leave just enough for a truncated header, and
  asserts the rendered section either drops cleanly or contains
  the trailing `:\n` separator (per the option PR 5 picks).

- **L-2 — asymmetric subject canonicalization between
  `FactStore.store` and the recall path.**
  [`FactStore.store`](../../agents/memory/facts.py) accepts the
  `subject` kwarg verbatim — only an empty-string check runs at
  the boundary, no
  [`canonicalize_subject`](../../agents/memory/fact_predicates.py)
  call.  PR 3's recall side canonicalizes before issuing
  `FactStore.recall` (via `_subject_seeds` →
  `canonicalize_subject`), and PR 2's extractor canonicalizes
  before storing, so the **production** write/read paths are
  consistent today.  But three callers bypass the extractor and
  write directly:
  1. Test fixtures (the new
     [`test_facts_recall.py`](../../tests/integration/test_facts_recall.py)
     uses `await fact_store.store(subject="bob", ...)` — happens
     to work because the canonical form of `"bob"` is also
     `"bob"`).
  2. Operator-seeded facts (RFC 0026 OQ #9 deferred follow-up).
  3. Future RFC 0013 erasure backfill (PR 1's
     `delete_by_subject` primitive is the eventual hook; an
     ingestion path that re-asserts subjects from a snapshot
     would need the same canonicalization).
  All three can silently write a non-canonical subject and miss
  recall, defeating the dementia-test invariant.  PR 3 makes
  this newly load-bearing because recall is now the dementia-
  test happy path.  Two options for PR 5:
  1. **Tighten `FactStore.store` to canonicalize internally.**
     The storage primitive becomes authoritative; direct callers
     do not need to remember to canonicalize.  Pin with a
     round-trip test that stores `subject="Bob "` (mixed case +
     trailing whitespace) and asserts recall on the canonical
     form returns the row.  Pairs naturally with L-1 option (3)
     if the cap-length policy lives in the same canonicalizer.
  2. **Pin the contract in the `FactStore.store` docstring** so
     direct callers know to canonicalize themselves.  Cheapest;
     leaves the footgun in place.
  Default: option (1) — cleaner storage-primitive contract and
  removes the footgun.  Pre-existing from PR 1/2 but surfaced
  by PR 3.

- **L-3 — `resolve_facts_config` not defensive against
  `null` budget knobs.**
  [`resolve_facts_config`](../../agents/persona_runtime/facts_section.py)
  reads `memory.facts.budget_tokens` as
  `int(facts_cfg.get("budget_tokens", DEFAULT_FACTS_BUDGET_TOKENS))`.
  If the resolved value is `None` (operator wrote
  `budget_tokens: null` in YAML and `additionalProperties` /
  `minimum` / `type: integer` did not gate it because the config
  bypassed schema validation), `int(None)` raises `TypeError` at
  agent construction time.  The
  [`schemas/agent.schema.json`](../../schemas/agent.schema.json)
  block rejects `null` (`type: integer`, `minimum: 0`) so a
  `make validate`-gated production config never reaches the
  raise.  But test fixtures, programmatic configs, and any
  path that bypasses `make validate` can hit it.  Fix is a
  one-line collapse:
  ```python
  raw = facts_cfg.get("budget_tokens")
  budget_tokens = DEFAULT_FACTS_BUDGET_TOKENS if raw is None else int(raw)
  ```
  Same defensive treatment applies to `extraction_model` if PR 5
  picks option (2) (plumb-through) on that deferred follow-up.
  Defence-in-depth; pair with a test that constructs the resolver
  call from a dict carrying `budget_tokens: None` and asserts the
  default falls through.

- **N-2 — `recall_facts_for_event(agent_id=...)` parameter is
  logging-only.**
  [`FactStore.recall`](../../agents/memory/facts.py) already
  filters by `self._agent_id` (the store is per-agent), so the
  `agent_id` kwarg passed into
  [`recall_facts_for_event`](../../agents/persona_runtime/facts_section.py)
  is consumed only by the `logger.warning(...)` template at the
  recall-failure log line.  `FactStore` exposes an `agent_id`
  property; the helper could read it off the store and drop the
  redundant kwarg.  Cosmetic refactor — useful because the
  current signature suggests the helper accepts an agent
  filter, which it does not.  Pin with no new test (signature
  change tracked by existing call sites; mypy / ruff catch
  drift).

##### From PR 4 review

Deep-review (PR #342) findings that **landed inline in PR 4**:

- **M-1 — phantom reinforcement when a per-subject header is
  dropped.** PR 4's initial cut called
  `MemoryBudget.record_admission` inside the per-item loop in
  [`render_facts_section`](../../agents/persona_runtime/facts_section.py)
  and admitted the `"Known facts about <subject>:\n"` header
  *after* the loop.  When the budget remainder after the items
  fell below the truncation floor (`MIN_TOKENS_FACTS = 24`),
  the header was dropped and the block was `continue`'d — but
  the per-item `record_admission` calls had already mutated
  `_admissions["facts"]`, so the subsequent
  `FactStore.mark_recalled` write at
  [`memory_context.py`](../../agents/persona_runtime/memory_context.py)
  targeted rows the LLM never saw.  Directly contradicted the
  PR description's contract ("the registry is the source of
  truth the reinforcement write reads off to target only the
  rows that reached the prompt").  Fix folded into PR 4: per-
  block `pending: list[tuple[fact_id, tokens_admitted]]` stages
  admissions locally; the registry + the `agent.facts.injected`
  telemetry counter fire only after the header admits
  successfully.  Pinned by
  `TestNoPhantomReinforcementOnHeaderDrop` (single-subject and
  multi-subject variants) in
  [`tests/integration/test_facts_reinforcement.py`](../../tests/integration/test_facts_reinforcement.py).

- **M-2 — TICK / sender-less events newly paid a DB cost.**
  PR 4's initial `_subject_seeds` shape always seeded
  `["self"]` so MT-MEMORY-005 Leg 5 (self-consistency) could
  read introspective `self.*` rows.  Side-effect: events with
  no resolvable sender (TICK, orchestrator-internal) now
  issued an unconditional `fact_store.recall(subject="self")`
  and defeated the PR-5 empty-context cost guard
  ([`memory_context.py` docstring §"Zero-admission events"](../../agents/persona_runtime/memory_context.py)).
  Fix folded into PR 4: `_subject_seeds` returns `[]` for
  sender-less events (restoring the pre-PR-4 short-circuit)
  and returns `[SELF_SUBJECT, canonical_sender]` for sender-
  bearing events.  User-facing legs always carry a sender so
  Leg 5 still flips green; TICK events stay free.  Pinned by
  `TestSubjectSeedsSenderlessShortCircuit` (unit) and
  `TestTickEventDoesNotQueryFactStore` (integration).  The
  pre-existing
  `test_priority_order_..._for_tick` assertion that
  `"facts_context" not in order` now passes for the right
  reason (the short-circuit) rather than incidentally
  (recall returned `[]` because no `self.*` rows existed).

- **M-3 — soft per-tier slice overage scales with subject
  count.**  `facts_tokens_used` in
  [`render_facts_section`](../../agents/persona_runtime/facts_section.py)
  accumulates item-line tokens only; the per-subject header is
  charged against the global budget but *not* against the
  slice.  With PR 4's multi-subject fan-out, the real upper
  bound on the tier's global-budget consumption is
  `facts_budget_tokens + N_subjects × header_tokens`, not the
  slice alone.  Today the overage is at most ~10 tokens (two
  seeds: `self` + sender, ~5 tokens each).  Documented inline
  in the `render_facts_section` docstring under a new
  "Soft-slice overage scales with subject count" section so a
  future operator tuning `memory.facts.budget_tokens` knows
  the slice is a soft floor on item-line tokens, not a hard
  cap on the tier.  No behaviour change.

- **N-1 — typo `"dianostics"` → `"diagnostics"`** in the
  module-level comment above `_provenance_logger` in
  [`memory_budget.py`](../../agents/persona_runtime/memory_budget.py).

- **N-2 — inverted test fixture.** The bulk-load loop in
  `test_dropped_fact_does_not_get_reinforced` stored 40 rows
  with `subject="bob"` paired with
  `predicate="self.has_attribute"`.  The `self.*` predicate
  namespace is conventionally paired with `subject="self"`
  rows (see
  [`agents/memory/fact_predicates.py`](../../agents/memory/fact_predicates.py)),
  and the predicate validator allows the mismatch (it checks
  the predicate alone), so the fixture worked but muddied the
  subject/predicate semantics it leaned on incidentally.
  Switched to `predicate="prefers"` (also allowlisted).

- **N-4 — docstring on
  `MemoryBudget.record_admission` overstated the env-gate
  scope.** Original wording said *"Side-effects are best-
  effort — a logging hiccup must never corrupt the registry
  the caller is about to read."*  True in spirit, but the
  registry mutation runs *unconditionally* before the env
  check; only the structured-log emission is best-effort.
  Reworded to make the asymmetry explicit so a future reader
  does not assume the registry write is gated too.

- **N-5 — dedup-location comment in `_subject_seeds` was
  slightly off.**  Original wording claimed duplicates were
  de-duplicated downstream by `recall_facts_for_event`'s
  `seen_ids` set; but the `if canonical == SELF_SUBJECT`
  branch in `_subject_seeds` *does* dedupe at the seed-list
  level (the downstream `seen_ids` covers fact-row dedup, not
  seed dedup).  Tightened the comment under the M-2 docstring
  rewrite.

The first-pass findings above (M-1, M-2, M-3, N-1, N-2, N-4,
N-5) all landed in the initial review-fix commit (`529e646`).
The phantom-reinforcement guard (M-1) and the TICK short-
circuit (M-2) are the two contracts the PR description made
explicit; both are now pinned by regression tests.  (The PR 3
review's "Header-dropped case" deferred item under "Negative-
path test coverage" is now satisfied by the M-1 fix's
regression test; that deferred bullet can be marked addressed
when PR 5 sweeps the residual PR 3 follow-ups.)

##### From PR 4 review — second deep-review pass (PR #342)

A second deep-review pass over the squashed PR 4 (`feature`
+ `review-fix`) caught one **Should-Fix** test-fixture bug
plus eight nice-to-have polish items.  Five of the eight
landed in this PR; four are deferred to PR 5 as documented
under PR 5 §"From PR 4 review — second-pass deferrals" below.

Findings that **landed inline** in PR 4 (second-pass review-fix
commit):

- **DR2-S-1 — `test_dropped_fact_does_not_get_reinforced`
  exercised supersession, not the per-tier slice.**  The
  original fixture in
  [`tests/integration/test_facts_reinforcement.py`](../../tests/integration/test_facts_reinforcement.py)
  stored 40 rows sharing `(subject="bob", predicate="prefers")`.
  [`FactStore.store`](../../agents/memory/facts.py)'s
  supersede-on-insert branch keys on `(agent_id, subject,
  predicate)`, so each successive store superseded its
  predecessor — only the last row was live, and the other 39
  were excluded by `recall`'s default `superseded_by IS NULL`
  filter **before** the allocator saw them.  The "no fact was
  dropped" assertion silently passed against the retraction
  filter, not the per-tier slice; a regression that broke the
  budget allocator's drop behaviour (e.g. raising
  `facts_budget_tokens` to 100 000 or removing the
  `if facts_tokens_used >= facts_budget_tokens: break` guard)
  would not flip this test red.  Fix folded in: cycle 17
  allowlisted predicates (each `(subject, predicate)` pair
  distinct) so every stored row stays live; add a sanity
  pre-check that `len(all_rows) == len(predicates)` so a
  future regression to colliding keys flips red before the
  allocator assertions degrade into a retraction-filter test;
  add `len(admitted) < len(all_rows)` to pin "budget did
  drop", not just "something was dropped".  This was the
  first-pass N-2 fix's residual — switching the predicate
  from `self.has_attribute` to `prefers` addressed the
  subject/predicate-namespace inconsistency but did not
  address the deeper supersession-masks-allocator-drop
  problem.

- **DR2-N-1 — `mark_recalled_for_agent` overwrote
  `last_recalled_at` unconditionally.**  The UPDATE in
  [`_facts_reinforce.py`](../../agents/memory/_facts_reinforce.py)
  wrote whatever `at` argument was passed, even when older
  than the existing column value.  RFC 0008 §G decay /
  validation composes with this column on a "newest recall
  wins" model — an older `at` clobbering a newer one would
  silently age the fact out.  Production `time.time()` is
  monotonic per-process so the failure mode is narrow (NTP
  step-back, the OQ #9 operator-seeded path replaying an
  older interaction, or test fixtures passing explicit `at`
  out of order), but the `MAX(COALESCE(last_recalled_at, 0),
  ?)` clamp is cheap insurance.  Fix folded in: tighten the
  UPDATE to clamp via `MAX`; the `COALESCE(..., 0)` is
  load-bearing because SQLite's `MAX(NULL, x) = NULL` would
  otherwise silently no-op the first call (column starts
  NULL).  Pinned by `test_older_at_does_not_clobber_newer`,
  `test_first_call_sets_from_null`, and
  `test_equal_at_is_idempotent` in
  [`tests/unit/python/test_fact_store_reinforcement.py`](../../tests/unit/python/test_fact_store_reinforcement.py).

- **DR2-N-4 — `MemoryBudget.record_admission(tier=…)` accepted
  arbitrary strings.**  Three call sites used `"facts"`,
  `"episodic"`, `"notes"`.  A typo at a future call site
  (`tier="fact"`) would silently populate an unread bucket —
  the facts-tier reinforcement read at
  [`FactStore.mark_recalled`](../../agents/memory/facts.py)
  looks up `admissions_by_tier("facts")`, sees `[]`, and
  skips the `last_recalled_at` write without surfacing
  anywhere.  Fix folded in: add a frozen `KNOWN_TIERS`
  allowlist (`{"facts", "episodic", "notes", "relationship",
  "channel_history"}` covering all canonical RFC 0027 §F
  tier names, even tiers not currently calling
  `record_admission` so future wiring lands on a known
  name); `record_admission` raises `ValueError` on
  unknown tiers.  The reader side (`admissions_by_tier`)
  stays permissive — a typo at a *read* site returns the
  empty-default, because the bug lives on the writer side.
  Pinned by `TestKnownTierAllowlist` in
  [`tests/unit/python/test_memory_budget_provenance.py`](../../tests/unit/python/test_memory_budget_provenance.py).

- **DR2-N-5 — soft-slice docstring implied even-share
  consumption across subjects.**  The M-3 docstring rewrite
  said *"the per-tier slice is shared across all blocks so
  a chatty sender cannot starve the persona's self-claims
  at the global allocator level"*, which is true at the
  **global** allocator (1500-token budget) but misleading at
  the **slice** level: once `facts_tokens_used` crosses
  `facts_budget_tokens` inside one block, the next block's
  outer-loop guard fires and the rest of the section is
  skipped.  With `_subject_seeds` returning
  `[SELF_SUBJECT, canonical_sender]` and `groups` preserving
  caller order, `self` blocks always render first, so this
  isn't a Leg-5 hazard in practice — but the docstring
  didn't say so out loud.  Fix folded in: spell out the
  per-block-sequential consumption shape and the
  `self`-first emit ordering as **load-bearing for Leg 5**
  in
  [`facts_section.py`](../../agents/persona_runtime/facts_section.py)
  under a new "Per-block slice consumption is sequential,
  not even" paragraph in `render_facts_section`'s
  docstring.  No behaviour change.

- **DR2-N-7 — provenance event name was carried only in the
  log message text.**  The
  `_provenance_logger.info("persatrix.memory.tier_admitted",
  extra=…)` call shipped the event identifier as the format
  string; structured fields (`tier`, `item_id`,
  `tokens_admitted`) landed on the LogRecord as attributes,
  but the event name itself was only grep-able via the
  human-readable message — brittle to future message-format
  changes.  Fix folded in: promote the event name to an
  `extra["event"]` field too, so downstream structured-log
  pipelines (Loki, ELK) can index on a stable key.  Message
  field stays populated for terminal-tailing.  Pinned by
  `test_event_name_is_promoted_to_structured_field`.

##### From PR 4 review — second-pass deferrals

Four nice-to-have findings from the second deep-review pass
(PR #342) that did not block PR 4 merge and are tracked here
for PR 5 to pick up.  Each is small in code surface but
touches a different concern (audit, defense-in-depth bound,
allocator amortisation, write-cost calibration), so they are
sequenced individually rather than batched.

- **DR2-N-2 — reinforcement writes are not audited.**
  [`FactStore.store`](../../agents/memory/facts.py) and
  `FactStore.supersede` both emit
  `_emit_audit("fact.store", …)` /
  `_emit_audit("fact.supersede", …)` via the RFC 0026 §G
  audit hook.
  [`FactStore.mark_recalled`](../../agents/memory/facts.py)
  does not.  The audit log is therefore blind to which facts
  were reinforced this turn, even though MT-MEMORY-005
  leg-failure analysis is one of the two consumers PR 4
  named.  The structured-log emission under
  `PERSATRIX_MEMORY_PROVENANCE=1` partially fills this gap,
  but it is env-gated and emits from the budget allocator,
  not from the storage layer — so an audit-log reader has no
  `FactStore`-level signal that reinforcement occurred.
  PR 5 decision: either emit
  `_emit_audit("fact.recalled", agent_id=…, fact_ids=ids,
  at=timestamp)` once per call (not per fact_id — audit
  volume stays bounded), or amend RFC 0026 §G to formally
  exclude reinforcement events from the audit surface.  Pair
  with a unit test in
  [`tests/unit/python/test_fact_store_reinforcement.py`](../../tests/unit/python/test_fact_store_reinforcement.py)
  that asserts a `fact.recalled` audit row lands after a
  `mark_recalled` call (path #1), or with an inline note in
  the `mark_recalled` docstring naming the audit-excluded
  contract (path #2).

- **DR2-N-3 — `mark_recalled_for_agent` does not chunk the
  IN-list.**  SQLite caps the parameter count per query at
  `SQLITE_MAX_VARIABLE_NUMBER` (32 766 in v3.32+, 999 in
  older builds).  Today the call site in
  [`memory_context.py`](../../agents/persona_runtime/memory_context.py)
  pulls IDs from `budget.admissions_by_tier("facts")`,
  bounded by `FACTS_RECALL_LIMIT=20` × ≤2 seeds = 40 IDs —
  orders of magnitude below either limit.  But the helper
  accepts an arbitrary `Iterable[str]` and is callable from
  other paths (RFC 0013 erasure backfill, RFC 0008
  calibration); a single-line chunk loop
  (`for chunk in chunks(ids, 900): …`) future-proofs the
  API without affecting today's hot path.  PR 5 decision:
  add the chunk loop to
  [`_facts_reinforce.py`](../../agents/memory/_facts_reinforce.py)
  and bump the docstring's "bounded by call-site" note to
  "bounded by helper-internal chunking".  Pin with a unit
  test that passes 1 500 IDs and asserts the call succeeds
  on a sqlite build with the older 999-variable cap (or that
  asserts the helper issues ≥2 UPDATE statements).

- **DR2-N-6 — triple `enc.encode` re-cost on oversized
  items.**  Pre-existing in
  [`memory_budget.py`](../../agents/persona_runtime/memory_budget.py),
  called out by the inline comment ("enc.encode() is invoked
  3× for oversized items").  With PR 4's new admission
  registry and the multi-block render, a tight budget can
  amplify this — a 200-token per-tier slice with a fact line
  that costs 60 tokens will see the oversized path on the
  third or fourth item, multiplied across the per-block
  fan-out.  Out of scope for PR 4 (the path existed before
  this PR), but the multi-block render makes it more
  reachable.  PR 5 decision: cache the token count off the
  first `enc.encode(text)` call in `try_add` and pass it to
  `_truncate_to_token_limit` so the truncation path can
  decode against the same token list rather than re-encoding.
  Pin with a benchmark / micro-bench under
  `tests/bench/` (RFC 0017 §B notes the bench-suite seam) or
  with an instrumented test that asserts `enc.encode` is
  called at most once per `try_add`.

- **DR2-N-8 — `mark_recalled_for_agent` issues its own
  `db.commit()` even when called inside an outer
  transactional caller.**  `FactStore` does not currently
  expose a transaction-scope manager, so all writes
  auto-commit per call (`store`, `supersede`, `prune`,
  `delete_by_subject` all commit before returning).  The
  reinforcement write therefore commits at the very end of
  every `_inject_memory_context` call — after the facts
  section has been added to working memory but before the
  caller builds the LLM prompt — a small write-amplification
  cost the persona's hot path now pays.  Today's call-site
  comment frames the failure as non-fatal because the section
  is already staged in working memory (so the LLM call still
  succeeds), but the write-cost is unmentioned.  PR 5 decision:
  calibrate against the MQ-12
  per-turn-cost budget; if the reinforcement commit shows
  up in the trace, either batch it with a future facts-tier
  write (RFC 0026 OQ #9 operator-seeded path is the most
  natural pair) or expose an explicit
  `FactStore.transaction()` context manager.  No-op if the
  calibration shows the commit is in the noise floor.

##### From PR 4 review — third-pass deferrals

Four low-priority findings from the third deep-review pass
(PR #342, `d241195`-onward) that did not block merge.  Each
was small enough that the PR-4 deliverable absorbed the three
medium findings inline:

* **M-1** — `budget.remaining < 8` assertion added to
  `TestNoPhantomReinforcementOnHeaderDrop` in
  [`test_facts_section.py`](../../tests/unit/python/test_facts_section.py),
  pinning the documented soft-overage trade-off so a future
  peek-then-commit refactor surfaces as a deliberate test update.
* **M-2** — `TestFirstSubjectBlockSurvivesUnderTightSlice` added
  to
  [`test_facts_section.py`](../../tests/unit/python/test_facts_section.py)
  (composed with the existing `_subject_seeds` unit pin) for the
  load-bearing self-first emit ordering claim.  Routed to the unit
  suite rather than the loose-slice integration pin at
  [`test_facts_reinforcement.py`](../../tests/integration/test_facts_reinforcement.py)
  to keep the integration file under the file_size cap; the
  integration site carries a pointer note to the unit pin so the
  next reader finds it.
* **M-3** — comment correction in
  [`memory_context.py`](../../agents/persona_runtime/memory_context.py)
  reframing the reinforcement-failure rationale around "section
  is staged in working memory" rather than the stale "prompt
  already shipped" wording (the prompt is built downstream of
  `_inject_memory_context`).

And the two zero-risk doc nits:

* **L-1** — registry-shape docstring correction in
  [`memory_budget.py`](../../agents/persona_runtime/memory_budget.py)
  (the registry stores ``item_id`` only; ``tokens_admitted`` rides
  the structured-log emission).
* **L-2** — forward-guard comment in
  [`facts_section.py`](../../agents/persona_runtime/facts_section.py)
  re-framing the `_subject_seeds` `except ValueError` as a
  forward-guard against future `canonicalize_subject` validation
  rather than a present-day path.

The remaining four ride PR 5.

- **DR3-L-3 — `mark_recalled` ``at=0.0`` and negative ``at``
  edge cases are uncovered.**  The monotonicity clamp in
  [`_facts_reinforce.py`](../../agents/memory/_facts_reinforce.py)
  uses ``MAX(COALESCE(last_recalled_at, 0), ?)``; ``at=0.0``
  on a NULL column flips the column to 0.0 (a state change),
  and ``at=-1.0`` collapses to ``MAX(0, -1) = 0`` regardless
  of existing value.  Neither case is reachable in production
  (``time.time()`` is monotone non-negative per-process), but
  the contracts pinned at
  [`test_fact_store_reinforcement.py`](../../tests/unit/python/test_fact_store_reinforcement.py)
  `test_older_at_does_not_clobber_newer` / `test_first_call_sets_from_null` /
  `test_equal_at_is_idempotent` leave a small gap where future SQL
  evolution could regress silently.  PR 5 decision: parameterise
  `test_first_call_sets_from_null` over ``[1.0, 0.0]`` and
  add a one-line ``at=-1.0`` no-op test against an already-
  populated column.  Low priority — the gap is academic until
  the OQ #9 operator-seeded-facts path or RFC 0013 erasure
  backfill starts supplying ``at`` values from sources other
  than ``time.time()``.

- **DR3-L-4 — `"Known facts about self:"` header label may
  invite persona-inversion in the LLM's output.**  The header
  in
  [`facts_section.py`](../../agents/persona_runtime/facts_section.py)
  was chosen at PR 3 to avoid the inverse ``"you"`` footgun
  when both ``self.*`` and sender rows are present.  The
  literal ``"self"`` label still risks the LLM reading it as
  a third-party entity called "Self," especially under the
  multi-block render where both ``"Known facts about self:"``
  and ``"Known facts about bob:"`` appear side-by-side.  No
  evidence from the third-pass review that the wording was
  empirically validated against a real persona — the choice
  is grounded in the inverse hazard, not in a measured
  MT-MEMORY-005 pass-rate.  PR 5 decision: spike whether
  ``"Known facts about you (the persona):"`` or
  ``"Known facts about yourself:"`` for the ``self`` block (and
  ``"Known facts about <canonical_sender>:"`` unchanged for the
  counterparty block) does better than the current header
  against MT-MEMORY-005 Leg 5 pass rate over a small set of
  seeded turns.  Defer the change unless the spike shows a
  measurable improvement — the PR-4 contribution to Leg 5
  (the seed + the reinforcement write) is the load-bearing fix
  and the label wording is a follow-on optimisation.

- **DR3-L-5 — `test_admitted_fact_ids_are_recorded_on_budget`
  uses raw module-attribute mutation with try/finally instead
  of `pytest.monkeypatch.setattr`.**  The try/finally restore
  in
  [`test_facts_reinforcement.py`](../../tests/integration/test_facts_reinforcement.py)
  is correct under exceptions, so this is purely stylistic.
  `monkeypatch.setattr(mc_mod, "MemoryBudget", _CapturingBudget)`
  would let pytest handle the restore as fixture teardown and
  drop the explicit dance.  PR 5 decision: refactor when
  next touching the file.  No standalone PR-5 work item — fold
  into whichever PR-5 change next edits the test.

- **DR3-L-6 — `facts_section.py` is at 439 lines (88% of the
  500-line `--strict` cap).**  PR 4's multi-subject fan-out
  added ~150 lines to
  [`facts_section.py`](../../agents/persona_runtime/facts_section.py);
  PR 5's deferred items do not touch this file, but PR 6's RFC
  close + future RFC 0027 §F end-state work may.  PR 5
  decision: monitor only, no action.  When the file does cross
  the cap, the natural split mirrors the storage-layer
  `_facts_audit.py` / `_facts_reinforce.py` carve-outs — pull
  the `render_facts_section` body (or the per-block staging
  helpers) into a sibling module.

#### PR checklist

- [ ] All deferred review findings addressed or downgraded to tracked issues with rationale.
- [ ] `make test` + `make lint` clean.

---

### PR 6: `feature/v031-rfc0026-close` — RFC Close

**Depends on**: PR 5a → 5e all merged.

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
| Predicate allowlist locks the vocabulary at PR 2 author time; a missing predicate produces silent rejection at extraction. | The allowlist is co-located with the extractor prompt — extending it is a one-line PR. PR 2 ships the `agent.facts.extraction_failed` counter (rejection volume) **and** the `persatrix.facts.rejected_predicate` structured-log field (verbatim rejected verb, deduplicated per-process). Together these answer "is rejection happening?" and "which verbs are being rejected?" — the data-driven feeder for allowlist amendments. |
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
| 1 | Facts schema + FactStore + erasure primitive | `feature/v031-rfc0026-facts-schema-store` | ✅ Merged | [#339](https://github.com/mkhomutov/Persatrix/pull/339) | 2026-05-14 |
| 2 | Extractor + predicate allowlist + audit | `feature/v031-rfc0026-extractor` | ✅ Merged | [#340](https://github.com/mkhomutov/Persatrix/pull/340) | 2026-05-14 |
| 3 | Recall + MemoryBudget tier slot + config | `feature/v031-rfc0026-recall-budget` | ✅ Merged | [#341](https://github.com/mkhomutov/Persatrix/pull/341) | 2026-05-14 |
| 4 | Reinforcement + retraction + tier provenance + MT update | `feature/v031-rfc0026-reinforcement-retraction` | ✅ Merged | [#342](https://github.com/mkhomutov/Persatrix/pull/342) | 2026-05-15 |
| 5a | Review follow-ups slice 1 — PR 1 review (symmetric latest-wins + nullability) | `feature/v031-rfc0026-followups-pr1` | 🔀 PR open | — | — |
| 5b | Review follow-ups slice 2 — PR 2 review (envelope parse observability) | `feature/v031-rfc0026-followups-pr2` | ⬜ Not started | — | — |
| 5c | Review follow-ups slice 3 — PR 3 review storage/render defensive fixes | `feature/v031-rfc0026-followups-pr3a` | ⬜ Not started | — | — |
| 5d | Review follow-ups slice 4 — PR 3 review tests + counter polish | `feature/v031-rfc0026-followups-pr3b` | ⬜ Not started | — | — |
| 5e | Review follow-ups slice 5 — PR 4 review (audit, chunking, edge cases) | `feature/v031-rfc0026-followups-pr4` | ⬜ Not started | — | — |
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
