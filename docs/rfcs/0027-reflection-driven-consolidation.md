# RFC 0027 — Reflection-Driven Consolidation

**Type**: feature
**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-05-01
**Target**: v0.4.0
**Depends on**: RFC 0005, RFC 0008, RFC 0009, RFC 0013, RFC 0026

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [G. Subject erasure (RFC 0013 traversal)](#g-subject-erasure-rfc-0013-traversal)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

Give [RFC 0005's `auto_reflect_after`](0005-persona-agent-memory.md) nudge teeth: when it fires, the persona reads its top-N recent episodes via the existing recall path and writes a single higher-level note (e.g., "Bob and I have shifted from formal to friendly over the past two weeks"). Source episodes get a `consolidated_into=<note_id>` pointer and a recall-priority demotion. No tombstoning. No background clustering pipeline. Fully auditable.

This is the §E deliverable from the [Memory Quality Roadmap](../memory-quality-roadmap.md#e-reflection-driven-consolidation-not-llm-clustering) and supersedes the user's draft RFC 0025 (thematic episode clustering) in scope and motivation.

## Motivation

Two failure modes drive this RFC:

1. **Episode-set growth without abstraction.** [RFC 0020](0020-interaction-lifecycle.md) makes interactions the unit of episodic memory, which compresses raw turn count by ~10× — but a persona that talks to one user weekly still accumulates dozens of similar episodes per year. None of them individually is wrong; collectively they're noise that drowns out the higher-level pattern ("our relationship has shifted").
2. **Background clustering rewrites history.** Draft RFC 0025 proposed a thematic-clustering pipeline that merges and tombstones older episodes. That has two problems: it does the consolidation *mechanically* (humans consolidate during reflection), and it puts the audit trail under stress — tombstoning is auditable-state risk for [RFC 0009](0009-security-sandboxing.md).

The roadmap-doc's framing: humans consolidate during *reflection*, and the persona substrate already has a reflection nudge ([`auto_reflect_after`](0005-persona-agent-memory.md)) that fires zero times today because nothing handles it. Wiring reflection to do consolidation produces a more grounded design than an LLM clustering pipeline that rewrites the historical record. Source episodes stay; a higher-level note is written *alongside* them; recall priority is rebalanced.

## Goals

1. When `auto_reflect_after` fires, the persona writes a single consolidation note that synthesizes a coherent pattern across its top-N recent episodes for the active scope.
2. Source episodes are preserved verbatim — no rewrite, no tombstone, full audit trail.
3. Source episodes carry a `consolidated_into` pointer; recall ranking uses the pointer to demote them in favour of the consolidation note when both would otherwise admit.
4. The consolidation note is a regular `recall_notes` row ([RFC 0005](0005-persona-agent-memory.md)) — it composes with the existing budget allocator; no new tier.
5. The dementia test ([MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md)) demonstrably benefits on the relationship-arc leg after a multi-interaction window.

## Non-Goals

- Episode rewriting, merging, or tombstoning. The historical record is immutable.
- Background or scheduled clustering. Consolidation is event-driven (reflection fires) — no separate worker, no corpus-wide pass.
- Cross-agent consolidation. Each agent reflects on its own episodes only.
- Replacing facts ([RFC 0026](0026-declarative-facts-tier.md)). Facts are atomic claims; consolidation notes are narrative-level patterns. The two compose — facts get extracted at interaction close; consolidations happen during reflection.
- LLM-driven theme detection across the whole episode corpus. Reflection scope is the top-N recent episodes for the active scope, bounded.

## Design / Implementation

### A. Trigger

`auto_reflect_after` is the existing [RFC 0005](0005-persona-agent-memory.md) counter. [RFC 0020 PR 4](0020-pr-plan.md#pr-4-featurev030-rfc0020-summarize-on-close--summarization-hook--janitor--record_interaction-move) already moved the increment to the interaction-close path. When the counter trips, the persona enters a brief reflection state instead of being a no-op.

Reflection runs at most once per `auto_reflect_after` firing. A second firing during an active reflection coalesces — no concurrent reflections per agent.

### B. Reflection scope

Reflection scope = the active conversational scope (DM partner, channel, thread). The reflection LLM call receives:

1. The top-N recent closed episodes for this scope (default N=8; configurable).
2. The current relationship summary for the scope's counterparty (if any).
3. The most recent prior consolidation note for this scope (if any) — so consolidations chain rather than restart.
4. The list of [RFC 0026 facts](0026-declarative-facts-tier.md) about the active subject — used for a reflection-time **semantic-contradiction sanity check** that flags conflict pairs across different predicates (e.g., `is_vegetarian=true` + `loves="ribeye"`). When a conflict is detected, the consolidation note records it (`contradictions: [{a: fact_id, b: fact_id, kind: "semantic"}]`) for the persona to reconcile rather than silently retracting either fact. Counter increment: `consolidation.contradictions_detected`.

Token cost is bounded: 8 × ~500 tokens of episode summary + ~200 of relationship + ~300 of prior consolidation + ~400 of subject facts ≈ 4900 tokens of input, single output of ≤ 600 tokens.

### C. Consolidation note shape

The reflection LLM emits a single note via the existing [`store_note`](0005-persona-agent-memory.md) surface, with metadata extensions:

```python
{
    "note_id": "...",                # ULID, as today
    "scope": "...",                  # interaction scope (RFC 0020 §G)
    "consolidates": ["ep_id_1", ...], # source episode IDs
    "kind": "consolidation",         # discriminator (new)
    "horizon": "two_weeks",          # rough timespan label
    "body": "Bob and I have shifted from formal to friendly over the past two weeks. ..."
}
```

`store_note` is already the correct surface — it carries agent intent, has the right TTL story, and feeds the existing recall path. The `kind` discriminator lets the recall ranker distinguish consolidations from agent-authored prose notes.

### D. Source-episode demotion

Each source episode gets a new column `consolidated_into = <note_id>`. The recall ranker — feeding [RFC 0017 `MemoryBudget`](0017-persona-memory-injection-budget.md) — checks this column: when an episode whose `consolidated_into` is non-null *and* the referenced consolidation note is itself a recall candidate for the same query, the episode is demoted (or skipped, configurable).

This is a ranking change, not a deletion. An operator querying with `--include-consolidated` sees the full historical record.

### E. Audit

Every consolidation produces a [RFC 0009](0009-security-sandboxing.md) audit event with:
- `event_type = "memory.consolidation"`
- `note_id`, `consolidates` (list of source episode IDs), `agent_id`, `scope`, `horizon`
- Counter increment: `agent.consolidation.notes_written`

Operators can reconstruct any consolidation chain from audit logs without touching the persona DB.

### F. Composition with RFC 0026 (facts)

[RFC 0026 facts](0026-declarative-facts-tier.md) and consolidation notes occupy different layers:
- **Facts** = atomic, framework-extracted, subject-indexed claims, written at every interaction close.
- **Consolidations** = narrative-level patterns, agent-authored at reflection firing.

The two are independently retrievable. A typical injected context after both ship: working memory + relationship summary + relevant facts (RFC 0026 tier slot) + relevant notes (including consolidations) + episodic recall, all under the [RFC 0017 budget](0017-persona-memory-injection-budget.md).

### G. Subject erasure (RFC 0013 traversal)

[RFC 0013 §SubjectErasure](0013-legal-ethical-compliance.md) must traverse the new `consolidated_into` and `consolidates` graph when erasing a subject's data. Two paths matter:

- **Forward (note → episodes)**: when a consolidation note is erased (its `scope` belonged to the erased subject), null `consolidated_into` on every episode whose row is *not* itself erased — otherwise demoted episodes orphan with a dangling pointer and become unrecallable.
- **Reverse (episode → note)**: when source episodes are erased, rewrite each referencing note's `consolidates` array; if the array empties, delete the note. The audit-logged `records_deleted` map gains `consolidation_notes_deleted` and `episode_pointers_nulled` keys.

Phase 2 must include this traversal alongside the schema migration. Without it, GDPR / CCPA erasure leaves orphan-pointer fragments that violate "no historical record loss" §Security from the operator's side but do leak through "data is here but unreachable" from the data-subject's side.

## Security Considerations

- **Reflection prompt injection.** The reflection LLM call receives episode summary content, which can include user-supplied text. Standard [RFC 0009](0009-security-sandboxing.md) input-sanitization rules apply at the recall side; the consolidation prompt itself is short and templated.
- **Note content as exfil channel.** Consolidation notes are written to the same store as agent-authored notes; the existing `RedactStruct` policy applies. A consolidation that captures secret-shaped content gets redacted at write time, same as today's `store_note` path.
- **Audit-trail integrity.** No tombstoning means no historical record loss. The `consolidated_into` column is append-only; reverting a consolidation is a new row, not a column rewrite.
- **Coalescing race.** Two scopes whose `auto_reflect_after` trips simultaneously must not interfere — per-agent serialization (existing `asyncio.Lock`) handles this. A reflection failure is logged + a counter increment, never a partial write.

## Phased Implementation Plan

### Phase 1: Reflection trigger + consolidation note write

1. Hook `auto_reflect_after` firing to a `ReflectionRunner` in `agents/persona_runtime/`.
2. Reflection LLM call with top-N episode + relationship + prior-consolidation input; output validated and persisted via `store_note`.
3. New `kind = "consolidation"` discriminator on notes; metadata extensions persisted.
4. Audit-log integration (event + counters).

### Phase 2: Source-episode demotion + recall ranking

1. Schema migration adds `consolidated_into` to `episodes`.
2. Recall ranker checks the column; default policy = demote (configurable to skip).
3. `--include-consolidated` query flag for operator visibility.
4. **Atomicity**: the consolidation-note `INSERT` and the `UPDATE episodes SET consolidated_into = ?` writes execute in a single transaction. A crash between the two leaves no partial state.
5. `SubjectErasure` traversal extended per §G — forward and reverse paths covered; audit map gains the two new keys.
6. Integration test asserts a reflection across 8 episodes produces one consolidation note + 8 demoted episode rows; an erasure-after-consolidation test verifies no dangling pointers remain.

### Phase 3: Tuning + dementia-test pass

1. [MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md) extended with a relationship-arc leg covering a multi-week scenario.
2. Calibration of N (top-N) and the demotion vs. skip policy under operator data.
3. Counter-driven SLO for `agent.consolidation.notes_written` per active scope.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents | `agents/persona_runtime/reflection.py` | **New** — `ReflectionRunner`; trigger hook; LLM call; result validation. |
| Python agents | `agents/memory/notes.py` | `kind` discriminator; metadata extensions. |
| Python agents | `agents/memory/episodic.py` | `consolidated_into` column + index. |
| Python agents | `agents/persona_runtime/memory_context.py` | Recall ranker checks `consolidated_into`. |
| Python agents | `agents/persona_runtime/__init__.py` | Wire `auto_reflect_after` firing → `ReflectionRunner`. |
| Python agents | `agents/observability/metrics.py` | `consolidation.notes_written`, `consolidation.failed`, `consolidation.episodes_demoted`. |
| Config / schema | `config/agents.yaml`, `schemas/agent.schema.json` | `memory.consolidation.*` knobs (N, demote-vs-skip, model). |
| Tests | `tests/unit/python/test_reflection_runner.py`, `tests/integration/test_consolidation_recall.py`, `tests/integration/test_consolidation_erasure.py` | Trigger, write, demotion, audit, atomicity, subject erasure. |
| Python agents | `agents/memory/erasure.py` (RFC 0013) | Traverse `consolidated_into` (forward) and `consolidates` arrays (reverse) during subject erasure. |

## Test Strategy

- **Unit tests**: `ReflectionRunner` lifecycle (trigger → LLM call → write); LLM-failure fallback (no partial write; counter increment); coalescing under concurrent triggers.
- **Integration tests**: 8-episode scope reflects into one consolidation note; demoted episodes rank below the consolidation; `--include-consolidated` exposes the full chain.
- **E2E / smoke tests**: a 4-week persona session shows the relationship-arc leg of [MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md) passing.
- **Manual tests**: [MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md) relationship-arc leg.

## Open Questions

1. **N (top-N episodes).** Default 8 is a guess; calibration in Phase 3.
2. **Demote vs. skip.** Demote (always show consolidation; show source if budget allows) is the default. Skip (consolidation only, source hidden unless `--include-consolidated`) is more aggressive. Operator-configurable.
3. **Reflection model.** Inherit summarization model, or pick a stronger model for reflection? Reflection is rarer than summarization, so a stronger model is feasible. Defer to Phase 1.
4. **Chain depth.** A consolidation note that includes prior consolidations as input could chain indefinitely. Cap at one prior consolidation (default) — a longer chain is a v0.5.0 question.
5. **Cross-scope reflection.** Today, reflection is per-scope. A persona that talks to many users might want a meta-reflection ("I'm spending more time on technical questions lately"). Out of scope for this RFC; tracked as a v0.5.0 follow-up.
6. **Reflection-time contradiction reporting.** When the §B sanity-check finds a semantic-conflict pair, what should the persona do — surface it in the next turn ("I think I have conflicting info — which is right?"), record it silently in the consolidation `contradictions` array, or escalate to the operator? Default: silent record + counter increment (`consolidation.contradictions_detected`); user-facing surfacing deferred to v0.5.0 once dogfood data shows whether silent-record creates compounding drift.

## Decision / Next Steps

1. Land the [memory-quality-roadmap.md](../memory-quality-roadmap.md) ratification PR (carries this RFC's motivation).
2. Defer implementation to v0.4.0; depends on [RFC 0026](0026-declarative-facts-tier.md) landing in v0.3.x so the two surfaces compose at integration time.
3. Open `feature/v04x-rfc0027-pr-plan` after v0.3.0 release.

## Related Documentation

- [Memory Quality Roadmap §E](../memory-quality-roadmap.md#e-reflection-driven-consolidation-not-llm-clustering) — design rationale; dementia-test framing.
- [RFC 0005 — Persona Agent & Memory System](0005-persona-agent-memory.md) — `auto_reflect_after`, `store_note`, notes substrate.
- [RFC 0008 — Agent Memory & Context Optimization](0008-agent-memory-context-optimization.md) — context budget, scoring seam.
- [RFC 0009 — Security & Sandboxing](0009-security-sandboxing.md) — audit log + redaction.
- [RFC 0017 — Persona Memory Injection Budget](0017-persona-memory-injection-budget.md) — recall ranker / allocator.
- [RFC 0020 — Interaction Lifecycle](0020-interaction-lifecycle.md) — interaction close + `record_interaction` site for `auto_reflect_after`.
- [RFC 0026 — Declarative Facts Tier](0026-declarative-facts-tier.md) — companion memory-quality RFC; facts and consolidations compose.
- [MT-MEMORY-005 — Dementia Test](../manual-tests/MT-MEMORY-005-dementia-test.md) — qualitative acceptance gate.
