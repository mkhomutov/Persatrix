---
id: RFC-0026
title: Declarative Facts Tier
summary: New persona-memory tier for canonical, dated, source-attributed facts — complements episodic recall and feeds RFC 0027 consolidation.
type: feature
status: proposed
author: Maksim Khomutov
created: 2026-05-01
target: v0.3.x
depends_on:
  - RFC-0005
  - RFC-0008
  - RFC-0009
  - RFC-0013
  - RFC-0017
  - RFC-0020
---

# RFC 0026 — Declarative Facts Tier

**Type**: feature
**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-05-01
**Target**: v0.3.x
**Depends on**: RFC 0005, RFC 0008, RFC 0009, RFC 0013, RFC 0017, RFC 0020

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [H. Subject erasure (RFC 0013 traversal)](#h-subject-erasure-rfc-0013-traversal)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

Promote `key_facts` out of episode summaries into a first-class **declarative-fact tier**. At interaction close, an LLM extractor emits structured tuples — `(subject, predicate, object, certainty, source_interaction_id, asserted_at)` — that are persisted to a dedicated `facts` table indexed by `subject`. On message arrival, the persona runtime looks up `facts WHERE subject IN (sender, mentioned_entities)` and injects them as a small high-signal section above episodic recall through the [RFC 0017 `MemoryBudget`](0017-persona-memory-injection-budget.md) allocator.

This is the §A deliverable from the [Memory Quality Roadmap](../memory-quality-roadmap.md#a-promote-key_facts-to-a-declarative-fact-tier) and the highest-leverage fix for the [dementia test](../memory-quality-roadmap.md#quality-bar--the-dementia-test).

## Motivation

The persona-memory subsystem stores everything as either prose (episodic) or sender-keyed structured state (relationship). The things humans actually remember about each other — names, preferences, commitments — currently live encoded inside 2000-character interaction summaries. BM25 cannot connect "Bob's daughter is named Mira" three weeks later to a query about Mira; even cosine similarity is noisy on a single proper noun in a long summary.

Three signals drive this RFC:

1. **The dementia complaint** ([memory-quality-roadmap.md](../memory-quality-roadmap.md)): persona references to earlier-stated facts feel "only sometimes," even at high `recall@k`.
2. **Root cause #1** in the roadmap: wall-of-prose summaries hide specific facts.
3. **The user-facing v0.3.0 promise** ([ROADMAP.md §v0.3.0](../../ROADMAP.md#v030--agent-conversations)): "form opinions about each other over time." Opinions need stable, retrievable facts to anchor them.

Three RFCs were drafted in response — 0023 (structured summary + auto-notes), 0024 (vector recall), 0025 (thematic clustering). The roadmap-doc assessment narrows the prize: structured **facts**, not structured summaries, are the load-bearing change. This RFC is that carve-out.

## Goals

1. Every closed interaction extracts zero or more facts as structured tuples without an additional LLM round-trip (combined with the existing summarization call).
2. Subject-indexed lookup at message arrival is O(log N) on the number of facts about that subject; injection respects the [RFC 0017 budget allocator](0017-persona-memory-injection-budget.md#b-memory-budget-allocator).
3. Facts compose with [RFC 0008 §G](0008-agent-memory-context-optimization.md#g-memory-eviction-decay-and-validation) decay/eviction via the same scoring seam — a fact is just another retrievable item to the budget allocator.
4. Fact retraction is supported (§Open Questions resolves the policy).
5. The dementia test ([MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md)) measurably improves on the named-entity and stated-preference legs after this RFC ships.

## Non-Goals

- Replacing episodic memory. Episodes remain the substrate for narrative recall; facts are an additional tier, not a replacement.
- Replacing `recall_notes` ([RFC 0005](0005-persona-agent-memory.md)). Notes are agent-discretion prose; facts are framework-extracted tuples. The two have different access patterns and different TTLs.
- LLM clustering or super-episode rewriting. That is the [§E reflection-driven consolidation surface](../memory-quality-roadmap.md#e-reflection-driven-consolidation-not-llm-clustering) (separate v0.4.0 RFC).
- Vector / embedding retrieval. Subject-indexed lookup is deterministic; cross-subject paraphrase recall is the [§24 (vectors) deferred surface](../memory-quality-roadmap.md#rfc-0024--episodic-vector-recall-sqlite-vec--hybrid-scorer--backfill).
- Multi-agent fact sharing. v0.3.x scope is per-agent facts only; cross-agent fact propagation is a v0.4.0 question paired with [RFC 0008 §H shared-pool ACL](0008-agent-memory-context-optimization.md).

## Design / Implementation

### A. Data shape

```python
@dataclass(frozen=True)
class Fact:
    fact_id: str                  # ULID
    agent_id: str                 # owner (per-agent isolation; matches RFC 0008 ACL)
    subject: str                  # canonical entity key (sender_id, mentioned_entity_id, or normalized name)
    predicate: str                # short verb phrase, e.g. "has_child_named", "prefers", "committed_to"
    object: str                   # short value, ≤ 200 chars
    certainty: float              # [0.0, 1.0]; seeded by extractor, updated by reinforcement (§F)
    source_interaction_id: str | None  # FK to interactions (RFC 0020 §B); NULL permitted — see below
    asserted_at: datetime         # timestamp of assertion (typically interaction.closed_at)
    last_recalled_at: datetime | None  # set by §F use-based reinforcement
    superseded_by: str | None     # fact_id of replacing fact (retraction policy — see §F)
```

Schema is additive — new `facts` table; no changes to `episodes` or `notes`.

**`source_interaction_id` nullability** (PR 5a amendment, 2026-05-15): the field is typed `str | None` and the DDL column is nullable. The production extractor (RFC 0026 PR 2) always populates it, but three legitimate callers commit rows without one and tightening the column would force them to fabricate a synthetic id:

1. Test fixtures that exercise the storage primitive in isolation.
2. The future RFC 0013 erasure backfill, which may re-assert subjects from a snapshot without a source-interaction context.
3. The OQ #9 operator-seeded facts path — config-driven cold-start seeds have no originating interaction.

The audit-log surface still records `source_interaction_id` (as `NULL` when absent) so provenance traceability is preserved on the production write path. Decision was deferred from the PR 1 review for explicit lock in PR 5a per [`docs/rfcs/0026-pr-plan.md` PR 5a §From PR 1 review](0026-pr-plan.md#from-pr-1-review).

### B. Extraction at interaction close

The summarization call introduced by [RFC 0020 PR 4](0020-pr-plan.md#pr-4-featurev030-rfc0020-summarize-on-close--summarization-hook--janitor--record_interaction-move) becomes a **two-output** prompt: (a) the existing prose summary, (b) a JSON list of fact tuples. One LLM call, two structured outputs. No new per-event cost.

The extractor prompt enumerates a small predicate vocabulary (~25 verbs covering attribute / preference / commitment / relationship classes) and instructs the model to return zero tuples when no extractable facts are present (the common short-interaction case).

**Relationship-predicate granularity.** Relationship verbs are intentionally gender-neutral (`has_child_named`, not `has_son_named` / `has_daughter_named`). The flat `(subject, predicate, object)` schema cannot carry the gender axis as a structured field; encoding it in the verb spawns one predicate per (relation × gender × generation) and pushes the schema gap into the vocabulary. The salient fact for memory is the relationship + the named entity; when the gender of the relationship is the load-bearing detail, it surfaces in the prose summary that ships in the same close-path round-trip.

**Vocabulary discovery from rejected predicates.** The allowlist is the storage-boundary cap on prompt-injection blast radius (§Security Considerations), but it is also the bound on what the LLM can record — a near-miss verb the model emits (e.g. `has_kid_named` vs the allowlisted `has_child_named`) is a quality signal that the vocabulary needs an amendment. The extractor records each distinct rejected verb verbatim, once per process, into the structured-log surface (`persatrix.facts.rejected_predicate` field). This is the operator discovery surface for growing the allowlist from observed workload rather than guessing. Per-process dedup keeps log volume bounded; an in-process cap prevents pathological growth from an adversarial LLM emitting unique-per-call garbage.

### C. Subject canonicalization

`subject` is a canonical entity key, not a free-form string. The runtime resolves it in this order:
1. If the entity is the conversational counterparty, `subject = sender_id`.
2. If the entity has an existing fact row, reuse the canonical form there (case- and whitespace-normalized).
3. Otherwise, normalize and store as a new canonical key.
4. The persona itself is a valid subject (`subject = "self"`) — used by the [MT-MEMORY-005 Leg 5 self-consistency](../manual-tests/MT-MEMORY-005-dementia-test.md) gate. The predicate vocabulary covers self-attribute / self-preference / self-value classes alongside the user-facing predicates.

This keeps subject-indexed lookup correct without an embeddings step — the canonical form lives in the row itself.

### D. Retrieval

`FactStore.recall(subject, limit) -> list[Fact]` returns facts about a subject ordered by a salience score (see §F). The persona runtime calls this for each `(sender, *mentioned_entities)` and feeds the union into the [RFC 0017 budget allocator](0017-persona-memory-injection-budget.md) as a new tier slot, ranked above episodic recall.

The allocator's tier-priority list grows by one entry: facts come *after* working memory + relationship summaries and *before* notes + episodic recall. The five enumerated tiers post-RFC 0026 are: working → relationship → **facts** → notes → episodic — matching the post-[RFC 0027](0027-reflection-driven-consolidation.md) end-state described in [RFC 0027 §F](0027-reflection-driven-consolidation.md#f-composition-with-rfc-0026-facts). The notes-vs-episodic relative ordering is owned by [RFC 0017 OQ #1](0017-persona-memory-injection-budget.md#open-questions); the current allocator places notes *after* episodic (priority 6 < 7), and that ordering shifts to notes-before-episodic when consolidation notes ship. The [RFC 0017 OQ #1](0017-persona-memory-injection-budget.md#open-questions) tier-budget split must be revisited — facts are short and high-signal, so a small dedicated slice (e.g. 200 of the 1500 token budget) is reasonable.

### E. Composition with `recall_notes`

Facts and notes co-exist. The persona runtime continues to call `recall_notes` for agent-authored prose; facts are an additional injection. Operators can enable/disable the facts tier via `config/agents.yaml`:

```yaml
memory:
  facts:
    enabled: true
    budget_tokens: 200
```

Fact extraction has no model knob of its own. It rides the interaction-close LLM call that already produces the conversation summary — a single combined `summarize + extract` call — so the extractor always runs on whatever `context_management.summarization.model` selects. An earlier draft of this RFC proposed a `memory.facts.extraction_model` override; it was dropped during the PR 5c review follow-ups because a knob distinct from the summariser model is a category error when the two share one call. See the [`extraction_model` decision in `0026-pr-plan.md`](0026-pr-plan.md) for the full rationale and the `additionalProperties: false` migration note.

### F. Salience and reinforcement

Each fact's `certainty` evolves under the [§C use-based reinforcement rule](../memory-quality-roadmap.md#c-salience-score-with-use-based-reinforcement) — a fact admitted into a prompt by `MemoryBudget` resets its decay timer. The full reinforcement formula lands in the [RFC 0008 calibration review](0008-calibration-review.md); this RFC consumes that contract.

Retraction policy: **symmetric latest-asserted-wins**. Within a single `(agent_id, subject, predicate)` key, only one row stays live and the row with the greatest `asserted_at` wins. On every write:

* Existing live rows with `asserted_at <= new.asserted_at` are marked superseded by the new row.
* If a strictly-newer live row already exists, the new row is itself marked superseded by it — an out-of-order older write does not leave two live rows.
* Equal-timestamp ties break in favour of the later arrival (the row being inserted). The production extractor uses monotonic `interaction.closed_at` so ties are unreachable in the hot path; the rule exists for fixtures, the OQ #9 operator-seeded path, and the future RFC 0013 erasure backfill.

Recall filters out superseded rows by default. This composes with reinforcement — a fact contradicted at the next interaction loses its salience boost. The symmetric shape was settled by PR 5a per [`docs/rfcs/0026-pr-plan.md` PR 5a §From PR 1 review](0026-pr-plan.md#from-pr-1-review); the PR 1 implementation initially shipped a strict-less-than SELECT that left two live rows on out-of-order or equal-timestamp writes.

### G. Audit and provenance

Every fact carries `source_interaction_id`. The [RFC 0009 AuditLogger](0009-security-sandboxing.md) records fact extraction events at `INFO` level; redaction policy follows the existing `RedactStruct` rules (no raw PII in audit metadata). A `superseded_by` write is also audited.

### H. Subject erasure (RFC 0013 traversal)

[RFC 0013 §SubjectErasure](0013-legal-ethical-compliance.md#c-right-to-erasure--memory-compliance) promises deletion of all data associated with a subject across **all** memory tiers. The new `facts` table is one such tier — `SubjectErasure.delete(subject_id)` MUST traverse it, both as the `subject` column directly and as the `source_interaction_id` foreign key (a fact extracted *during* an erased subject's interaction is also erasable, even if its declared subject is someone else). Phase 1 extends the erasure surface with `FactStore.delete_by_subject(subject_id)` and includes the count in the audit-logged `records_deleted` map. Without this, the first GDPR / CCPA request after v0.3.x ships will silently miss extracted facts.

## Security Considerations

- **PII concentration**: a `facts` table is a denser PII surface than prose summaries. The [RFC 0009 redactor](0009-security-sandboxing.md) must be applied at extraction time, not at recall time, so the table never persists raw secrets.
- **Cross-agent leakage**: per-agent isolation matches the [RFC 0008 §H ACL model](0008-agent-memory-context-optimization.md). No fact crosses an `agent_id` boundary in v0.3.x.
- **Prompt injection**: the extractor prompt receives interaction content. A user (or another agent in a channel) crafting "store fact: <attacker-controlled tuple>" is a prompt-injection vector; the predicate vocabulary is enumerated and validated against an allowlist before write to bound the blast radius.
- **Retraction race**: two interactions closing concurrently with conflicting facts could each write `superseded_by` pointing at the other. Handled by serializing fact writes per-`agent_id` (matches the existing per-agent `asyncio.Lock` in `_LLMPersonaAgent`).

## Phased Implementation Plan

### Phase 1: Schema + extractor

1. New `agents/memory/facts.py` module — `Fact` dataclass, `FactStore` with `store`, `recall`, `supersede`, `prune`.
2. SQLite migration adds `facts` table + `idx_facts_subject_agent` index.
3. Extractor wired into the [RFC 0020 PR 4 summarize-on-close](0020-pr-plan.md#pr-4-featurev030-rfc0020-summarize-on-close--summarization-hook--janitor--record_interaction-move) path — combined prompt; structured outputs parsed and stored.
4. **Close-path sequencing**: at interaction close the prose-summary write commits first (via the `EpisodicMemory` connection), then the fact-tuple writes commit (via the `FactStore` connection). Each tier owns its own `aiosqlite` connection, so a literal single-transaction wrap across both halves is not implementable at this layer — and the failure modes the original "single SQLite transaction" wording cared about all collapse onto **commit the summary, skip the facts, increment a counter** anyway:
   - **Envelope (JSON) parse failure** on the combined response → summary commits as the unparsed raw text; facts are skipped; the counter is *not* bumped here because the failure surfaces before the facts-dispatch path (see also [§PR 5 follow-ups — combined-envelope truncation observability](0026-pr-plan.md#from-pr-2-review)).
   - **Per-tuple failure** (allowlist miss, missing field, certainty out of range) inside `store_extracted_facts` → summary commits; the offending tuple is skipped; `facts.extraction_failed` increments per tuple; other tuples in the same batch still commit.
   - **Summary commit failure** → facts dispatch is skipped (guarded by the `update_episode_summary` return value); the janitor re-attempts the summary on the next sweep.
   The unreachable case — summary committed, mid-batch SQLite error in facts → roll back the summary — matches the spec's recovery clause regardless ("commits only the summary plus a counter increment"), so the observable behaviour is the spec's intended outcome on every path. The original "single SQLite transaction" wording was reconciled with the implementation in [PR 2 review](0026-pr-plan.md#from-pr-2-review); a future RFC amendment can revisit if cross-tier transactional rollback becomes desirable.
5. Unit tests for the extractor's empty-list path, the predicate-allowlist rejection, the schema migration, and the partial-failure rollback.

### Phase 2: Recall + budget integration

1. `FactStore.recall(subject, limit)` wired into `agents/persona_runtime/memory_context.py` as a new tier feeding `MemoryBudget.try_add`.
2. Tier priority order updated: working → relationship → **facts** → notes → episodic. Cross-link to [RFC 0017 OQ #1](0017-persona-memory-injection-budget.md#open-questions) for the budget split; notes-before-episodic ordering aligns with the post-[RFC 0027](0027-reflection-driven-consolidation.md) end state in [§F](0027-reflection-driven-consolidation.md#f-composition-with-rfc-0026-facts).
3. `config/agents.yaml` + `schemas/agent.schema.json` additions for `memory.facts.*` knobs.
4. Integration test: a fact stored at interaction N is injected at interaction N+1 when the subject reappears, *without* the subject string appearing in the query.

### Phase 3: Reinforcement + retraction

1. `last_recalled_at` updated on `MemoryBudget`-admitted recall (composes with [RFC 0008 calibration §C](0008-calibration-review.md)).
2. Latest-asserted-wins retraction with `superseded_by` writes; audit-log entry per supersede.
3. Per-turn tier-provenance instrumentation surfaced through the [`MemoryBudget` allocator](0017-persona-memory-injection-budget.md) — gates [MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md) Telemetry section so leg-fail diagnoses can disambiguate recall miss from reasoning miss.
4. [MT-MEMORY-005 dementia test](../manual-tests/MT-MEMORY-005-dementia-test.md) re-run; named-entity, preference, and self-consistency (Leg 5) legs must pass.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents | `agents/memory/facts.py` | **New** — `Fact`, `FactStore`, predicate allowlist. |
| Python agents | `agents/memory/episodic.py` | Schema migration: add `facts` table + index. |
| Python agents | `agents/memory/interactions.py`, `agents/persona_runtime/summarize_close.py` | Combined summary + facts extraction at interaction close. |
| Python agents | `agents/persona_runtime/memory_context.py` | New tier slot in the budget allocator. |
| Python agents | `agents/observability/metrics.py` | `facts.extracted`, `facts.injected`, `facts.superseded`, `facts.extraction_failed`. |
| Config / schema | `config/agents.yaml`, `schemas/agent.schema.json` | `memory.facts.*` keys. |
| Tests | `tests/unit/python/test_fact_store.py`, `tests/integration/test_facts_recall.py` | Storage, recall, retraction, allocator integration, partial-failure rollback. |
| Python agents | `agents/memory/erasure.py` (RFC 0013) | Extend `SubjectErasure.delete` to traverse the `facts` table (both `subject` and `source_interaction_id`); include `facts_deleted` in the audit map. |
| Docs | `docs/rfcs/0017-persona-memory-injection-budget.md` | OQ #1 tier-budget split addendum (facts slot). |

## Test Strategy

- **Unit tests**: `FactStore` CRUD; predicate-allowlist rejection; canonicalization; supersede chain; schema migration idempotence.
- **Integration tests**: combined-prompt extraction at interaction close; subject-indexed recall on a fresh subject; dementia-test scenario in the integration harness (named entity → trigger → reference without keyword overlap).
- **E2E / smoke tests**: a multi-interaction persona session asserts a stated preference is honored without re-asking.
- **Manual tests**: [MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md) — three-leg pass (named entity, preference, commitment).

## Open Questions

1. **Predicate vocabulary scope.** Start small (~30 verbs across attribute / preference / commitment / relationship)? Or open-ended with allowlist enforcement at write time? Lean toward small + extensible.
2. **Tier-budget slice.** [RFC 0017 OQ #1](0017-persona-memory-injection-budget.md#open-questions) currently does not allocate a facts slice. 200 tokens (≈13% of 1500) is a starting point — calibration belongs in the [RFC 0008 calibration review](0008-calibration-review.md).
3. **Should `subject` accept multiple forms?** A single canonical form is simpler; a subject-alias table is more robust to name variants ("Bob" vs "Robert"). Defer to Phase 2.
4. **Cross-agent fact sharing.** v0.3.x ships per-agent only. The cross-agent surface pairs with [RFC 0008 §H](0008-agent-memory-context-optimization.md) and is a v0.4.0 question.
5. **Extraction model selection.** Inherit `optimization.yaml → context_management.summarization.model`, or pin a smaller/faster model for facts? Defer to Phase 1 implementation.
6. **Negative facts and state-history retention.** `superseded_by` keeps the latest assertion but loses prior states ("user used to live in Boston, now Seattle"). Should superseded rows remain queryable for "where did you used to live?" via an explicit history flag on `recall`? Lean toward yes; defer policy to Phase 3 once dogfood data shows whether the persona attempts state-history queries.
7. **Cross-fact semantic contradiction.** Latest-asserted-wins handles `(subject, predicate)` collisions but not semantic conflicts across different predicates (`is_vegetarian=true` + `loves=ribeye`). Detection is owned by [RFC 0027 §B reflection-time sanity sweep](0027-reflection-driven-consolidation.md), not this RFC.
8. **Inferred facts.** Extraction is literal-only by design (predicate allowlist). Multi-utterance inferences ("she handles school pickups") live between facts and consolidations and are out of scope here. Track as a v0.4.0 surface alongside RFC 0027.
9. **Operator-seeded facts (cold start).** A new persona has zero facts; an operator may want to pre-seed brand voice / company info / persona self-claims. Defer to a `seed_facts:` block in `config/agents.yaml` — Phase 2 follow-up, not load-bearing for v0.3.x.
10. **Self-as-subject coverage.** §C.4 admits `subject = "self"`. The Phase-1 predicate vocabulary needs to include self-attribute predicates (e.g., `self.has_preference`, `self.holds_value`) alongside user-facing ones; finalize the list before extractor prompt freezes.

## Decision / Next Steps

1. Land the [memory-quality-roadmap.md](../memory-quality-roadmap.md) ratification PR (carries this RFC's motivation and links).
2. After RFC 0020 closes (PRs 5–7), open `feature/v03x-rfc0026-pr-plan` with the per-PR scaffold.
3. Phase 1 implementation PR opens after the RFC 0026 PR plan merges.
4. RFC 0017 OQ #1 (tier-budget split) gets its facts-slice addendum at Phase 2 land time.

## Related Documentation

- [Memory Quality Roadmap §A](../memory-quality-roadmap.md#a-promote-key_facts-to-a-declarative-fact-tier) — design rationale and dementia-test framing.
- [RFC 0005 — Persona Agent & Memory System](0005-persona-agent-memory.md) — substrate (notes, episodes, relationships, `auto_reflect_after`).
- [RFC 0008 — Agent Memory & Context Optimization](0008-agent-memory-context-optimization.md) — context budget, eviction/decay, scoring seam, ACL model.
- [RFC 0008 Calibration Review](0008-calibration-review.md) — landing point for the §C reinforcement formula consumed by §F here.
- [RFC 0009 — Security & Sandboxing](0009-security-sandboxing.md) — audit log + secret redactor used at extraction time.
- [RFC 0017 — Persona Memory Injection Budget](0017-persona-memory-injection-budget.md) — `MemoryBudget` allocator + OQ #1 tier split.
- [RFC 0020 — Interaction Lifecycle](0020-interaction-lifecycle.md) — interaction close hook for combined summary + extraction.
- [MT-MEMORY-005 — Dementia Test](../manual-tests/MT-MEMORY-005-dementia-test.md) — qualitative acceptance gate.
