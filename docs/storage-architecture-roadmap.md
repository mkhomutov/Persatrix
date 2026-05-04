# Storage Architecture Roadmap — Discussion Notes

**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-05-03
**Target**: scope-shaping for the v0.4.0 storage layer; sets the personal/society storage boundary and the vectors-as-accelerator-only policy. Spawns one v0.4.0 RFC; folds smaller items into existing RFCs.
**Companion to**: [Memory Quality Roadmap](memory-quality-roadmap.md), [v0.3.0 plan](v0.3.0-plan.md), [ROADMAP.md §v0.4.0](../ROADMAP.md#v040--agent-organizations)

---

## Table of Contents

- [Why this doc exists](#why-this-doc-exists)
- [Scope and non-goals](#scope-and-non-goals)
- [Where Persatrix is now and where it is going](#where-persatrix-is-now-and-where-it-is-going)
- [The dementia bar reframed in storage terms](#the-dementia-bar-reframed-in-storage-terms)
- [Storage choice — SQLite vs Postgres vs MongoDB vs hybrid](#storage-choice--sqlite-vs-postgres-vs-mongodb-vs-hybrid)
- [Memory tiers — keep, drop, or rename](#memory-tiers--keep-drop-or-rename)
- [Semantic memory — when, how, and why not the default](#semantic-memory--when-how-and-why-not-the-default)
- [Target picture](#target-picture)
- [Recommended sequencing](#recommended-sequencing)
- [Risks](#risks)
- [Open questions](#open-questions)
- [Decision / next steps](#decision--next-steps)
- [Related documentation](#related-documentation)

---

## Why this doc exists

Persatrix's storage layer is being asked to do increasingly different things across versions. Today (v0.2.3 shipped, v0.3.0 in implementation), every memory tier is a table in a single per-agent `memory.db` SQLite file. That model is correct for v0.2's per-agent private memory, but the roadmap is asking it to do more:

- **v0.3.0** — multi-agent channels ([RFC 0011](rfcs/0011-channels-bridges.md)) and shared memory pools ([RFC 0008 §H](rfcs/0008-agent-memory-context-optimization.md#h-shared-vs-isolated-memory)): *first time agents share state*.
- **v0.4.0** — organizational topologies (RFC 0012), sub-agent spawning (RFC 0010), the Skill Registry ([RFC 0014](rfcs/0014-agent-skill-registry-lifecycle.md)), and the Decision Policy Engine (RFC 0028): *first time the society itself has structure*.
- **v0.5.0** — external bridges and full compliance & privacy ([RFC 0013](rfcs/0013-legal-ethical-compliance.md)): *first time external user data enters at volume and right-to-erasure must be enforceable*.
- **v0.6.0+** (when the distributed mesh lands; not yet promised on [ROADMAP.md](../ROADMAP.md)): *first time storage is not co-located with the agent*.

Without a policy decision now about *where the personal/society boundary lives*, each of those RFCs will independently pick "another table in `memory.db`," and the cost of carving the society state out grows with every PR that lands.

This doc captures the architectural assessment, recommends a personal/society storage split, and stages the work into one focused v0.4.0 RFC plus addenda to existing RFCs. It is **not an RFC** — it is the planning step between the architectural question and the RFCs that act on it. Same shape and role as [memory-quality-roadmap.md](memory-quality-roadmap.md).

## Scope and non-goals

**In scope.** Storage substrate choice (SQLite / Postgres / Mongo / vectors); the personal/society boundary; tier vocabulary; semantic-memory naming; vector-deployment policy; sequencing across v0.3.x, v0.4.0, and v0.5.0.

**Out of scope.** Editing accepted RFCs (0005, 0008, 0017, 0020, 0026). Authoring the v0.4.0 storage-split RFC itself. Selecting Postgres extensions (`pgvector` vs alternatives) — that lives in the spawned RFC. Mesh / distributed storage (v0.6.0).

---

## Where Persatrix is now and where it is going

Today, grounded in code: one per-agent `memory.db` SQLite file holds episodic + relationship + notes ([docs/diagrams/memory-architecture.md](diagrams/memory-architecture.md)); episodic uses FTS5 lexical recall ([agents/memory/episodic.py](../agents/memory/episodic.py)); working memory is volatile in Python ([agents/memory/working.py](../agents/memory/working.py)); shared-pool scaffolding exists ([agents/memory/shared_pool.py](../agents/memory/shared_pool.py)) but is per-agent in physical layout; migrations are forward-only ([agents/memory/migrations.py](../agents/memory/migrations.py)). [RFC 0017](rfcs/0017-persona-memory-injection-budget.md) caps per-event memory injection; [RFC 0020](rfcs/0020-interaction-lifecycle.md) collapses per-message episodes into per-interaction summaries; [RFC 0026](rfcs/0026-declarative-facts-tier.md) adds a `facts` table; [RFC 0027](rfcs/0027-reflection-driven-consolidation.md) consolidates via reflection. RFC 0028 will add `DecisionRecord`s on every checkpoint — yet another consumer of the same SQLite file.The trajectory of *what storage must do* (v0.2 line is current observed state; v0.3+ are projected):

```
v0.2: per-agent private memory                     → SQLite is perfect           (observed)
v0.3: + agent-to-agent channels + shared pools     → SQLite starts straining     (projected)
v0.4: + org graph + decision audit + skill grants  → SQLite is the wrong shape   (projected)
v0.5: + erasure + consent + compliance audit       → SQLite + erasure is a fight (projected)
v0.6: + multi-node                                 → SQLite is no longer an option (projected)
```

Plan the v0.3/v0.4 storage split now, even if v0.3 still ships entirely against SQLite. The longer the "everything in `memory.db`" assumption persists, the more expensive the carve-out gets.

## The dementia bar reframed in storage terms

The user-facing quality bar (from [Memory Quality Roadmap](memory-quality-roadmap.md#quality-bar--the-dementia-test)) is "natural and better than human." Translated into storage requirements:

| Quality property | Storage implication |
|------------------|---------------------|
| Continuity across interactions | A persistent **scratchpad bridge** (volatile + durable) — pure RAM is wrong; pure SQL row-per-message is wrong |
| Recognition of recurring entities | **Subject-indexed structured store** ([RFC 0026](rfcs/0026-declarative-facts-tier.md) facts) — *the* load-bearing change |
| Reference to established facts | **Declarative tier separated from narrative tier** (RFC 0026). Vectors do not solve this — they retrieve more prose. |
| Salience-weighted recall | **Use-based reinforcement** column (`last_recalled_at`) on every retrievable item, not just episodes |
| Salience that matches *importance*, not *length* | **Outcome tags** at write time ([RFC 0020 OQ #6](rfcs/0020-interaction-lifecycle.md#open-questions)) — free at write, expensive to backfill |
| Self-consistency over time | **`subject = "self"` rows in facts table** ([RFC 0026 §C.4](rfcs/0026-declarative-facts-tier.md)) |
| Time-aware reasoning | **Structured "since we last spoke" header** reading from RFC 0021 timestamps — not prose-rendered recency |

None of those say "vector database." All say "the right *shape* of data, written at the right *time*."

---

## Storage choice — SQLite vs Postgres vs MongoDB vs hybrid

**Default position**: hybrid — SQLite for personal memory, Postgres for society state, S3-class object store for cold artifacts (v0.5+). Mongo doesn't appear.

### SQLite — keep for personal memory

A persona reads its own memory in tight loops on every tick. Embedded SQLite is microseconds; any network DB is milliseconds — and at the tick rates the v0.2.x cost-leak fix (the empty-tick short-circuit) already had to defend against ([RFC 0017 §F](rfcs/0017-persona-memory-injection-budget.md#f-empty-context-tick-short-circuit)), even single-digit-millisecond round trips matter. One file per agent is a natural ownership boundary: backups, snapshots, "delete this agent" become `rm`. FTS5 is good enough for recall over interaction-summary corpora. Operational cost is zero — asking the user to provision Postgres for a single agent is a tax we shouldn't levy.

### Postgres — add for society state

The moment v0.3.0 ships channels, agents share state. The moment v0.4.0 ships orgs, the society is a graph. SQLite handles neither well:

| Society capability | Why SQLite struggles | Why Postgres fits |
|--------------------|---------------------|-------------------|
| Multi-agent channels ([RFC 0011](rfcs/0011-channels-bridges.md)) | WAL still serializes writers; fan-out from N channels contends on the single writer | Concurrent writers, NOTIFY/LISTEN |
| Org topology (v0.4 RFC 0012) | Recursive CTE polish needed | Recursive CTEs first-class |
| Decision audit (RFC 0028) | Single-writer bottleneck on append + replay | Partitioned tables + logical decoding |
| HITL approval queue (RFC 0028) | Cross-agent state with TTLs | Row-level locking, advisory locks |
| Compliance erasure ([RFC 0013](rfcs/0013-legal-ethical-compliance.md)) | Foreach across N agent files | One JOIN |
| Vector recall when/if it lands | `sqlite-vec` works small | `pgvector` is production-hardened |
| Multi-node mesh (v0.6) | SQLite is local-only | Replicas, logical replication |

**The split**: anything with *one writer and one logical owner* lives in SQLite (the agent's brain). Anything requiring *cross-agent consistency* or *external query* lives in Postgres (the society). This is the load-bearing recommendation of this doc.

### MongoDB — explicitly not

Data shape is wrong (fact tuples, graph edges, audit chains — none benefit from documents); joins matter for v0.4 organizational queries; transactional story is weaker than Postgres for the audit chain; the codebase is SQL-idiomatic throughout. Postgres + JSONB covers raw transcript storage at much lower architectural cost.

### Vector DBs (Pinecone / Weaviate / Qdrant) — deferred

Same answer as RFC 0024: deferred behind a measured failure of FTS5/BM25 on real summaries. When it lands, `pgvector` co-located with the society Postgres is the lowest-risk choice — no separate ops surface.

---

## Memory tiers — keep, drop, or rename

Today's tiers (working / episodic / relationship / notes) describe the *physical store*. Re-tier by *cognitive purpose*:

| Tier | Purpose | Lifetime | Backing | Maps to today |
|------|---------|----------|---------|---------------|
| **Scratchpad** | Active conversation context | Volatile + bridge across one prior interaction | RAM + small SQLite snapshot | Working + Memory-Quality-Roadmap [§B](memory-quality-roadmap.md#b-continuity-bridge-across-interaction-close) |
| **Episodes** | Narrative record | Persistent, decay over months | SQLite + FTS5 | Episodic |
| **Facts** | Subject-indexed declarative truth | Persistent, retraction only | SQLite | [RFC 0026](rfcs/0026-declarative-facts-tier.md) |
| **Bonds** | Per-pair relationship state | Persistent, slow decay | SQLite | Relationship |
| **Commitments** | Time-bound promises | Persistent, lifecycle-managed | SQLite | [RFC 0021 P2](rfcs/0021-persona-temporal-awareness.md) |
| **Notes** | Agent-discretion prose | Persistent | SQLite | Notes (unchanged) |
| **Procedural** | Extracted patterns | Persistent | Postgres (society-shared) | [RFC 0015](rfcs/0015-process-automation-pattern-extraction.md) |
| **Shared world** | Cross-agent facts and channels | Persistent, multi-writer | Postgres | [RFC 0008 §H](rfcs/0008-agent-memory-context-optimization.md#h-shared-vs-isolated-memory) + RFC 0011 |

The **Facts** row includes a `subject = "self"` slice that serves as the persona's self-model; whether that slice deserves promotion to a separate **Identity** tier or remains a view-with-write-ACL over Facts is [OQ #4](#open-questions). The doc currently treats it as the latter — one tier, one row class — to avoid pre-empting the OQ.

Why the rename matters: "scratchpad" signals it's allowed to be lossy but should *bridge one interaction-close boundary* (today's "working" is purely volatile — [root cause #2](memory-quality-roadmap.md#root-causes-of-the-dementia-feel) waiting to happen); "bonds" prevents collision with the relational *database*; the `subject="self"` slice of Facts forces the design to think about who can write to it (only the persona, never an extractor reading user input — see [OQ #4](#open-questions)); "procedural" carved out for [RFC 0015](rfcs/0015-process-automation-pattern-extraction.md) lands in the society store from day one. The rename is **vocabulary, not behavior**. Tracked as [SA-2](#recommended-sequencing).

---

## Semantic memory — when, how, and why not the default

"Semantic memory" usually means one of two things: (a) *vector embeddings of text*, or (b) *typed knowledge in subject-predicate-object form* (the cognitive-science meaning).

### The cognitive-science kind — already happening, name it explicitly

[RFC 0026](rfcs/0026-declarative-facts-tier.md)'s `(subject, predicate, object, certainty, asserted_at)` *is* semantic memory in the cognitive sense. The docs should name it that way:

- **Episodic** — autobiographical events → `episodes` table ([RFC 0020](rfcs/0020-interaction-lifecycle.md))
- **Semantic** — context-free knowledge → `facts` table ([RFC 0026](rfcs/0026-declarative-facts-tier.md))
- **Procedural** — how to do things → patterns extracted by [RFC 0015](rfcs/0015-process-automation-pattern-extraction.md)

Adopting that vocabulary exposes a gap: Persatrix has no procedural memory tier today, and RFC 0015 is v0.5.0. A v0.4 placeholder — even just "extracted recipes can be hand-authored as YAML" — closes the loop early.

### The vector-embedding kind — accelerator, not tier

Deploy embeddings *only when measurement shows BM25 missing relevant episodes the persona then visibly forgets*. The Memory Quality Roadmap deferral of RFC 0024 behind [MT-MEMORY-005](manual-tests/MT-MEMORY-005-dementia-test.md) is exactly this discipline; this doc hardens it as a policy:

> **Vectors-as-accelerator-only**: vector indexes never own facts. They are recall accelerators on top of stores that already hold the truth in structured form. If a fact only exists as a high-similarity hit, it does not exist.

This matters because: (1) [RFC 0013](rfcs/0013-legal-ethical-compliance.md) erasure must guarantee deletion, and selectively deleting from vector indexes is hard — if facts live in `facts` and the vector is just an index, deletion is unambiguous; (2) RFC 0028's `DecisionRecord` replay needs determinism, and embeddings drift across model upgrades — keep them off the load-bearing path; (3) embedding every event in a busy channel is not cheap. Tracked as [SA-3](#recommended-sequencing).

One place vectors *would* be load-bearing earlier: **subject canonicalization for facts**. [RFC 0026 §C](rfcs/0026-declarative-facts-tier.md) punts on entity resolution. For non-trivial cases ("Bob" / "Robert" / "rob@example.com"), case-and-whitespace normalization will fail. A small embedding-based subject-resolution pass at fact-write time — with a confidence threshold and an "ask for confirmation" fallback — addresses that without putting vectors on the read path. Worth raising as an OQ on RFC 0026.

---

## Target picture

```
┌──────────────────────────────────────────────────────────────────┐
│ Per-agent (one process, one persona)                             │
│                                                                  │
│   Scratchpad (RAM)  ──── snapshot ───►  agent-{id}/scratch.db   │
│                                                                  │
│   agent-{id}/episodes.db    (FTS5;  decay)                      │
│   agent-{id}/facts.db       (subject-indexed; retraction chain; │
│                              includes subject="self" slice)     │
│   agent-{id}/bonds.db       (per-pair trust + texture)          │
│   agent-{id}/commitments.db (time-bound; lifecycle)             │
│   agent-{id}/notes.db       (agent-authored prose)              │
│   agent-{id}/action-log.jsonl  (per-agent action chain;         │
│                                 backend pinned by OQ #3)        │
└──────────────────────────────────────────────────────────────────┘
                               │
                               │  publish via narrowed capability tokens
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│ Society (Postgres, single instance per deployment)               │
│                                                                  │
│   channels(...)              messages(...)                       │
│   org_nodes(...)             org_edges(...)        skills(...)   │
│   shared_facts(...)          decision_records(...) approvals(...)│
│   procedural(...)            audit_chain(...)      vectors(...)  │
│                                                                  │
│   pgvector co-located when MT-MEMORY-005 says FTS5 has missed    │
│   enough recall to justify the index.                            │
└──────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│ Cold artifacts (S3-class object store; v0.5+)                    │
│   raw transcripts, large attachments, exported audit bundles     │
└──────────────────────────────────────────────────────────────────┘
```

Key invariants: per-agent files are an **ownership boundary** (`rm -rf agent-{id}/`); the society Postgres is the **only place agents talk to each other through** (no agent reads another's SQLite directly); vectors live in **exactly one place** (society Postgres + `pgvector`) when they exist at all; the personal/society boundary is the **same** boundary that capability tokens ([RFC 0009 Phase 4](rfcs/0009-security-sandboxing.md#phase-4-agent-identity-tokens--hitl-gates)) and HITL gates ([RFC 0028 §H](rfcs/0028-agent-decision-policy-engine.md#h-mandatory-human-in-the-loop-decision-classes)) enforce. There are deliberately **two audit streams**: a per-agent action chain (the `action-log.jsonl` per agent) for that agent's own outbound actions, and a society-wide `audit_chain` table in Postgres for cross-agent events; [OQ #3](#open-questions) governs only the per-agent stream's backend (JSONL vs SQLite append-only). This diagram is the **target** for the v0.4.0 storage-split RFC — not a v0.3.0 commitment.

---

## Recommended sequencing

SA = Storage Architecture.

| # | Item | Vehicle | Target | Status |
|---|------|---------|--------|--------|
| SA-1 | Personal/society storage split (Postgres for society state) | new RFC; cross-cuts RFC 0008 §H, RFC 0009, RFC 0011, RFC 0028 | v0.4.0 | 📋 Pending RFC |
| SA-2 | Tier rename (scratchpad / bonds / identity / procedural) | docs PR (vocabulary only) | v0.3.x | ⬜ |
| SA-3 | "Vectors-as-accelerator-only" policy line | addendum to [memory-quality-roadmap.md](memory-quality-roadmap.md) + OQ on RFC 0024 when it un-defers | v0.3.x | ⬜ |
| SA-4 | Forgetting floor/ceiling (salience clamps) | addendum to [RFC 0008 calibration review](rfcs/0008-calibration-review.md) | v0.3.x | ⬜ |
| SA-5 | Per-tier SQLite file split | implementation PR if SQLite contention measured | v0.4.x | 🔮 Conditional on SA-1 benchmark |
| SA-6 | Channel-granularity episodes | verify after [RFC 0020 P3](rfcs/0020-interaction-lifecycle.md) lands; fold into SA-1 if needed | v0.3.0 | 🔮 Tracked |
| SA-7 | Unified provenance log (CQRS / event sourcing) | new RFC if pursued; cross-cuts RFC 0009 audit + RFC 0028 records | v0.5.0 | 🔮 Deferred |
| SA-8 | Memory-as-API for sub-agents (read-side capability tokens) | folds into RFC 0010 design | v0.4.0 | 🔮 Tracked |
| SA-9 | Forgetting as first-class (generalization, trauma-locking, embarrassment-decay) | speculative; needs forcing function | v0.5+ | 🔮 Deferred |
| SA-10 | Personality as memory-shaping function | speculative; needs forcing function | v0.5+ | 🔮 Deferred |

**Ordering**: SA-1 is load-bearing; it should land *before* RFC 0028 implementation begins or RFC 0010 schema is settled, otherwise both will bake in "another table in `memory.db`" assumptions SA-1 has to undo. SA-2/SA-3/SA-4 are independent and can ship as small PRs in v0.3.x. SA-5 is gated on a real benchmark. SA-6 may already be in [RFC 0020 P3](rfcs/0020-interaction-lifecycle.md) scope. SA-8 should be raised as an OQ on RFC 0010 when authored.

The throughline: **draw the personal/society storage boundary now, on paper, before v0.4.0 implementation forces it on us in code**.

---

## Risks

1. **Adds Postgres as a hard dependency for v0.4.0+.** Today's "run it from a terminal" experience would degrade. Mitigation: ship a `--single-agent` mode that skips Postgres entirely; enable society features only when Postgres is configured. Single-agent must remain a first-class experience.
2. **One-file-per-tier multiplies SQLite connection management.** Mitigation: a thin `MemoryStore` facade holds the per-tier connections; callers don't see the file split. SA-5 is gated on contention measurement.
3. **Migration cost from current `memory.db` is real.** Mitigation: a one-shot `persatrix memory migrate` command; old `memory.db` keeps working in read-only fallback for a deprecation window.
4. **"Vectors-as-accelerator-only" is a discipline that can erode.** Mitigation: encode the rule in the `MemoryStore` API surface — `recall_by_similarity()` returns row IDs, never content. (Risks #2 and #4 share one facade: `MemoryStore` is a single class that owns both per-tier connection management and the no-content-from-vectors invariant.)
5. **Counterfactual reasoning here has no real cost data.** Specifically, "SQLite starts straining at v0.3 channel volumes" is intuition, not benchmark. Mitigation: SA-1's PR plan opens with a benchmark step; if SQLite holds at realistic society scale, Postgres can defer to v0.5.0.

---

## Open questions

1. **At what scale does SQLite actually break for channels?** Worth benchmarking before committing to Postgres for v0.4.0. Specifically: 10 agents writing to one channel at 1 msg/sec each — does WAL mode hold? If yes, SA-1 narrows to "design the split, defer the migration."
2. **Per-tier file split (SA-5) vs one-file-per-agent with separate tables.** Both work; the choice is locking-granularity gains vs connection-management cost. Decide inside SA-1's RFC.
3. **Action log: JSONL (cheap, replayable, slow to query) or SQLite append-only table (queryable, harder to tail)?** [RFC 0009](rfcs/0009-security-sandboxing.md) has likely thought about this; resolve as part of SA-1 / SA-7 prep. Governs the per-agent stream only; the society `audit_chain` table is fixed at Postgres.
4. **Does "identity" deserve a separate tier, or is it a `subject="self"` view over `facts.db` with a write ACL?** Lean toward view-with-ACL; the question is whether the ACL is enforceable inside SQLite or needs Python-layer mediation. Resolve in SA-2.
5. **Procedural memory — really part of the memory system, or the skill registry ([RFC 0014](rfcs/0014-agent-skill-registry-lifecycle.md)) by another name?** Probably the latter. If so, the tier collapses into RFC 0014's catalogue; flag during SA-2.
6. **Where do raw LLM transcripts live?** Today they're transient. For RFC 0028 replay and RFC 0013 right-to-erasure they probably need to land somewhere — Postgres? S3? Surface during SA-1.

---

## Decision / next steps

**Status**: 📋 Proposed (this PR). Awaiting maintainer ratification.

On ratification, the maintainer is expected to:

1. Flip status to `📋 Proposed — ratified <date>` and add a "Tracks: SA-1 (RFC ...), SA-2 (PR ...), ..." line — same shape as [memory-quality-roadmap.md](memory-quality-roadmap.md).
2. Open SA-1 (the v0.4.0 storage-split RFC) before RFC 0028 implementation begins, so `DecisionRecord` schema lands in the right store.
3. Add cross-reference rows to the [v0.3.0 plan §Memory Quality Follow-Ups](v0.3.0-plan.md#memory-quality-follow-ups-v03x-and-beyond) for SA-2/SA-3/SA-4.
4. Update [ROADMAP.md §v0.4.0](../ROADMAP.md#v040--agent-organizations) to list SA-1 as a v0.4.0 dependency for RFC 0010 / RFC 0028 / RFC 0012.
5. Surface SA-6 as an Open Question on the [RFC 0020](rfcs/0020-interaction-lifecycle.md) Phase 3 PR plan, and SA-8 as an Open Question on the RFC 0010 design when it is authored — both are tracked here but need an owner in their respective RFCs.

If the maintainer wants to push back on any of SA-1 through SA-10, the discussion lives here. RFC reviewers can cite this doc by section.

---

## Related documentation

- [Memory Quality Roadmap](memory-quality-roadmap.md) — companion discussion doc; precedent for this one's shape.
- [RFC 0005](rfcs/0005-persona-agent-memory.md), [RFC 0008](rfcs/0008-agent-memory-context-optimization.md), [RFC 0008 Calibration Review](rfcs/0008-calibration-review.md) (landing point for SA-4), [RFC 0009](rfcs/0009-security-sandboxing.md), [RFC 0011](rfcs/0011-channels-bridges.md) (primary v0.3.0 motivator for SA-1), [RFC 0013](rfcs/0013-legal-ethical-compliance.md), [RFC 0017](rfcs/0017-persona-memory-injection-budget.md), [RFC 0020](rfcs/0020-interaction-lifecycle.md), [RFC 0026](rfcs/0026-declarative-facts-tier.md), [RFC 0027](rfcs/0027-reflection-driven-consolidation.md).
- RFC 0028 (Agent Decision Policy Engine — on `feature/v04-rfc0028-agent-decision-policy-engine`; `DecisionRecord` is the third major event-stream consumer; SA-1 should land before its implementation).
- [v0.3.0 plan](v0.3.0-plan.md) — SA-2/SA-3/SA-4 fold here.
- [ROADMAP.md §v0.4.0](../ROADMAP.md#v040--agent-organizations) — version target for SA-1.
- [docs/rfcs/README.md](rfcs/README.md) — RFC process and lifecycle.
