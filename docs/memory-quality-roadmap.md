# Memory Quality Roadmap — Discussion Notes

**Status**: 🔨 Draft (discussion doc, not an RFC)
**Author**: Maksim Khomutov
**Date**: 2026-05-01
**Target**: scope-shaping for draft RFCs 0023/0024/0025; seeds for v0.3.x and v0.4.0
**Companion to**: [v0.3.0 plan](v0.3.0-plan.md), [ROADMAP.md §v0.3.0](../ROADMAP.md#v030--agent-conversations)

---

## Table of Contents

- [Why this doc exists](#why-this-doc-exists)
- [Scope and non-goals](#scope-and-non-goals)
- [Quality bar — the dementia test](#quality-bar--the-dementia-test)
- [Assessment of draft RFCs 0023 / 0024 / 0025](#assessment-of-draft-rfcs-0023--0024--0025)
- [Root causes of the dementia feel](#root-causes-of-the-dementia-feel)
- [Proposed alternatives](#proposed-alternatives)
  - [A. Promote `key_facts` to a declarative-fact tier](#a-promote-key_facts-to-a-declarative-fact-tier)
  - [B. Continuity bridge across interaction close](#b-continuity-bridge-across-interaction-close)
  - [C. Salience score with use-based reinforcement](#c-salience-score-with-use-based-reinforcement)
  - [D. Outcome-tagged importance, not turn-count importance](#d-outcome-tagged-importance-not-turn-count-importance)
  - [E. Reflection-driven consolidation, not LLM clustering](#e-reflection-driven-consolidation-not-llm-clustering)
  - [F. Structured "since we last spoke" prompt header](#f-structured-since-we-last-spoke-prompt-header)
  - [G. Memory-of-context dogfood test](#g-memory-of-context-dogfood-test)
- [Recommended sequencing](#recommended-sequencing)
- [Where each idea will eventually land](#where-each-idea-will-eventually-land)
- [Open questions](#open-questions)
- [Decision / next steps](#decision--next-steps)
- [Related documentation](#related-documentation)

---

## Why this doc exists

Three RFCs are on deck for memory-quality work: **0023** (structured episode summaries + auto-extracted notes + recency boost calibration), **0024** (sqlite-vec embeddings + hybrid scorer + backfill), and **0025** (thematic clustering of older episodes into super-episodes). All three were proposed in response to the qualitative complaint that the persona "feels like it has dementia" across conversations — recent details fail to surface, established facts re-introduce themselves, and the relationship arc resets at every interaction boundary.

Three separate RFCs is the right shape for *implementation tracking*, but adopting them as written risks spending v0.3.x infrastructure budget on the wrong layer. The premise of this doc is that the dementia experience is primarily a **representation** problem (prose summaries hide facts; relevance scoring ignores salience; interaction boundaries are sharp) and only secondarily a **retrieval** problem (BM25 vs vectors). Vectors and clustering address retrieval; they do not fix the representation gap.

This doc captures the assessment, proposes alternatives, and recommends a different sequencing. It is not an RFC — it is the planning step that sits between the dementia complaint and any of the RFCs that act on it.

## Scope and non-goals

**In scope.** Assessment of drafts 0023 / 0024 / 0025 against the [quality bar](#quality-bar--the-dementia-test); alternative ideas; staging recommendation across v0.3.x and v0.4.0.

**Out of scope.** Editing accepted RFCs ([0008](rfcs/0008-agent-memory-context-optimization.md), [0017](rfcs/0017-persona-memory-injection-budget.md), [0020](rfcs/0020-interaction-lifecycle.md), [0021](rfcs/0021-persona-temporal-awareness.md)) — scope changes to them would be separate RFC amendments. Authoring the drafts 0023/0024/0025 themselves. Multi-agent / shared-memory work ([RFC 0008 §H](rfcs/0008-agent-memory-context-optimization.md#h-shared-vs-isolated-memory)). Vector-DB dependencies beyond `sqlite-vec`.

---

## Quality bar — the dementia test

The acceptance bar is **qualitative**, not just metric-driven. A persona that scores well on `recall@k` can still feel like a person with dementia if its retrieved fragments are prose and the model has to re-derive facts from them on every turn. Concretely, the test is:

> Across a five-interaction scenario over 30 minutes covering one named entity, one stated preference, and one explicit commitment, does the persona reference each of those when an appropriate trigger appears later, **without keyword overlap to seed the retrieval**?

If the answer is "only sometimes" — even with high `recall@k` numbers — the memory layer has failed the dementia test. This bar is the v0.3.0 user-facing promise ([ROADMAP.md L420](../ROADMAP.md#v030--agent-conversations) — "form opinions about each other over time") translated into something a reviewer can fail an RFC against.

---

## Assessment of draft RFCs 0023 / 0024 / 0025

### RFC 0023 — Episodic Memory Quality (structured summary + auto-notes + recency boost)

**Verdict**: Strongest of the three; mis-scoped.

The instinct to replace prose summaries with `(narrative, key_facts[], commitments[], timestamps[])` is correct. The real prize is `key_facts[]`: a structured, retrievable layer of *declarative* memory, distinct from narrative episodes. Treating it as a schema field on `episodes` buries the lede — see [§A](#a-promote-key_facts-to-a-declarative-fact-tier).

The framework-initiated note write at close inherits [RFC 0005](rfcs/0005-persona-agent-memory.md)'s note model, which was designed for agent intent (the persona decides what's worth noting). Framework-initiated extraction is a different permission and TTL story; conflating the two complicates `recall_notes` semantics for both callers.

The recency-boost calibration is a one-line scoring change and correctly belongs as an addendum to [`0008-calibration-review.md`](rfcs/0008-calibration-review.md), where the eviction/scoring formula already lives.

**Recommendation**: Split RFC 0023 into (a) a JSON-summary schema RFC that pins `outcome` tags ([RFC 0020 OQ #6](rfcs/0020-interaction-lifecycle.md#open-questions)) and the 2000-char field vocabulary, and (b) a separate "declarative-fact tier" RFC. Move the recency boost to the calibration review.

### RFC 0024 — Episodic Vector Recall (sqlite-vec + hybrid scorer + backfill)

**Verdict**: Sound infra, oversold as a fix for the dementia feel.

BM25 isn't the bottleneck while [RFC 0020](rfcs/0020-interaction-lifecycle.md) is still landing. With one summary per interaction (instead of one per message), corpus density inverts: there are fewer rows, each carrying a coherent multi-turn narrative. BM25 over those summaries works *better* than today's BM25 over per-message episodes. Vectors mainly help **paraphrase** queries — "did we ever discuss pricing?" against a stored summary that says "we negotiated rate cards." That's real value, but smaller than the dementia framing implies.

The `RelevanceScorer` protocol seam is the right hook ([RFC 0008 OQ #1](rfcs/0008-agent-memory-context-optimization.md#1-relevance-scoring-approach--heuristic-only-in-phase-1-pluggable-scoring-interface)) — RFC 0024 should be a backend swap, not an architectural change.

**Recommendation**: Defer to v0.3.x or v0.4.0, gated on dogfood data showing BM25 misses on multi-turn summaries. Keep the `RelevanceScorer` seam clean in the meantime so the swap stays cheap.

### RFC 0025 — Thematic Episode Clustering (cluster + merge + tombstone)

**Verdict**: Premature. v0.4.0 at earliest.

Re-merging episodes that RFC 0020 just stopped over-shredding has a strong "two wrongs make a right" smell. Cluster-then-tombstone is **mechanical** consolidation; humans consolidate during *reflection*, which [RFC 0005's `auto_reflect_after`](rfcs/0005-persona-agent-memory.md) nudge already exists for. Wiring reflection to do consolidation produces a more grounded design than an LLM clustering pipeline that rewrites the historical record — see [§E](#e-reflection-driven-consolidation-not-llm-clustering).

Tombstoning is also auditable-state risk for [RFC 0009](rfcs/0009-security-sandboxing.md): a "memory was here" audit trail must survive consolidation, which means tombstoning needs its own retention story.

**Recommendation**: Replace with [§E](#e-reflection-driven-consolidation-not-llm-clustering) as a v0.4.0 RFC. Drop RFC 0025 from the active queue.

---

## Root causes of the dementia feel

The three drafts all add retrieval/storage infrastructure. The qualitative failure is upstream of that:

1. **Wall-of-prose summaries hide specific facts.** A 2000-char narrative about "negotiating with Bob" doesn't surface "Bob's daughter is named Mira" three weeks later — BM25 won't connect them, and even cosine similarity is noisy on a single proper noun in a long summary.
2. **Sharp interaction boundaries.** [RFC 0020 §C](rfcs/0020-interaction-lifecycle.md#c-interaction-lifecycle-states) commits to "do not reopen during closing" — correct for concurrency but produces visible artifacts. A user replies 11 minutes after going quiet, the prior interaction is now `closed/summarized`, and the next turn starts with empty working memory plus whatever recall surfaces. The persona reads as forgetful even though the data is there.
3. **Recall is relevance-only, not salience-weighted.** The current scorer ranks "what matches the query." Human memory ranks "what matched + what mattered + what's recurrent." Importance is currently a `0.3 + 0.05 * turn_count` placeholder ([RFC 0020 §I](rfcs/0020-interaction-lifecycle.md#i-backfill-and-migration)) — a 10-turn boring exchange beats a 2-turn "I'm pregnant."
4. **No declarative tier.** Everything is episodic. `recall_notes` is the closest thing, but notes are agent-discretion ([RFC 0005](rfcs/0005-persona-agent-memory.md)) and rarely written at framework write time — so the things humans actually remember about each other (names, preferences, commitments) live encoded in prose.
5. **Temporal scaffolding is at the wrong layer.** [RFC 0021 P1](rfcs/0021-persona-temporal-awareness.md) renders recency in prose. Better: a *structured* "since we last spoke" header so the LLM doesn't have to parse "3 days ago" out of the summary blob.

These five are independent; addressing any one improves the dementia experience, but addressing all five is what gets to "better than human."

---

## Proposed alternatives

Each idea below names a concrete deliverable, the layer it lives in, and an order-of-magnitude cost estimate. Costs are token cost per event unless noted.

### A. Promote `key_facts` to a declarative-fact tier

**Idea.** Stop hiding facts inside narrative episodes. At interaction close, run a one-shot extractor that emits structured tuples — `(subject, predicate, object, certainty, source_interaction_id, asserted_at)` — and store them in a dedicated `facts` table indexed by `subject`. On message arrival, look up `facts WHERE subject IN (sender, mentioned_entities)` and inject as a small high-signal section above episodic recall.

**Layer.** New tier in `agents/memory/`. Sits between [`RelationshipMemory`](rfcs/0005-persona-agent-memory.md) (structured but per-sender only) and [`EpisodicMemory`](rfcs/0005-persona-agent-memory.md) (prose, multi-subject). Composes with [RFC 0017 `MemoryBudget`](rfcs/0017-persona-memory-injection-budget.md) — facts get a tier slot in the greedy fill.

**Cost.** Combine the extraction LLM call with the existing summarization call at interaction close (one prompt, two structured outputs). No new per-event cost. Storage is a small table — facts are short.

**Why this is the highest-leverage fix.** Subsumes most of RFC 0023's value, addresses [root cause #1](#root-causes-of-the-dementia-feel) directly, and is the single change most likely to move the dementia bar.

**Eventual home.** A new RFC (post-0025 in numbering, since the user has the 0023–0025 reservations) — call it tentatively "Declarative Facts Tier."

### B. Continuity bridge across interaction close

**Idea.** Keep the most recent closed interaction's summary in working memory for the same scope until the *next* interaction *also* closes. Two summaries max, scoped to the active conversational partner. This means a user replying 11 minutes after going quiet sees the persona reference the just-closed interaction without paying for a recall round-trip.

**Layer.** Working-memory change only. New section in [`agents/persona_runtime/memory_context.py`](../agents/persona_runtime/memory_context.py). No schema, no proto, no LLM cost.

**Cost.** ~200 tokens of working-memory budget for the carry-forward summary.

**Why this matters.** Addresses [root cause #2](#root-causes-of-the-dementia-feel). Without it, [RFC 0020](rfcs/0020-interaction-lifecycle.md)'s sharp "do not reopen" rule produces the most visible dementia artifact.

**Eventual home.** Single PR, no RFC needed. Sized for an [RFC 0020 P2 follow-up](rfcs/0020-pr-plan.md) or v0.3.x patch.

### C. Salience score with use-based reinforcement

**Idea.** Replace [RFC 0008 §G](rfcs/0008-agent-memory-context-optimization.md#g-memory-eviction-decay-and-validation)'s static `importance × 0.6 + recency × 0.3 + access_freq × 0.1` with a salience term that decays exponentially but **resets on successful recall** — where "successful" means the entry was injected into a prompt and admitted by the [`MemoryBudget` allocator](rfcs/0017-persona-memory-injection-budget.md#b-memory-budget-allocator), not dropped under budget pressure. Memories that get used stay sharp; memories that fade really fade.

**Layer.** Scoring formula change in `agents/memory/episodic.py` and the `RelevanceScorer` protocol. Composes with [RFC 0008 §G](rfcs/0008-agent-memory-context-optimization.md#g-memory-eviction-decay-and-validation)'s decay model — the existing $c_t = c_0 \cdot e^{-\lambda t}$ becomes the salience update rule.

**Cost.** Free at query time. Adds one column (`last_recalled_at`) and one update path on inject.

**Why this matters.** Addresses [root cause #3](#root-causes-of-the-dementia-feel). Replaces the +10% multi-turn boost hack ([RFC 0020 §I](rfcs/0020-interaction-lifecycle.md#i-backfill-and-migration)) with a principled signal. Pairs naturally with [§D](#d-outcome-tagged-importance-not-turn-count-importance) — outcome tags seed the initial salience, use reinforces it.

**Eventual home.** Folds into the [RFC 0008 calibration review](rfcs/0008-calibration-review.md), exactly the seam that doc owns. No new RFC.

### D. Outcome-tagged importance, not turn-count importance

**Idea.** Pin [RFC 0020 OQ #6](rfcs/0020-interaction-lifecycle.md#open-questions) before any RFC 0023-style schema work lands. The summarizer already returns structured-ish output; have it emit `outcome: {neutral, agreement, conflict, disclosure, commitment}` and an emotional-weight float in `[0, 1]`. Use those for importance; keep `turn_count` only as a tiebreaker.

**Layer.** Summarization prompt change in [RFC 0020 P2](rfcs/0020-pr-plan.md) + `_compute_interaction_importance` formula change in [RFC 0020 §I](rfcs/0020-interaction-lifecycle.md#i-backfill-and-migration). Backward compatible — legacy rows keep `0.5`.

**Cost.** Marginal — the summarizer is already running. Adds ~30 tokens to the prompt and ~20 tokens to the structured output.

**Why this matters.** A 10-turn boring exchange should not outrank a 2-turn disclosure. Without it, [§C](#c-salience-score-with-use-based-reinforcement)'s reinforcement loop bootstraps from a noisy initial signal.

**Eventual home.** Resolves [RFC 0020 OQ #6](rfcs/0020-interaction-lifecycle.md#open-questions) inside RFC 0020 P2's PR plan. No new RFC.

### E. Reflection-driven consolidation, not LLM clustering

**Idea.** Instead of [draft RFC 0025](#rfc-0025--thematic-episode-clustering-cluster--merge--tombstone)'s background clustering pipeline, give [RFC 0005's `auto_reflect_after`](rfcs/0005-persona-agent-memory.md) nudge teeth. When it fires, the persona reads its top-N recent episodes via the existing recall path and writes a single higher-level note ("Bob and I have shifted from formal to friendly over the past two weeks"). Source episodes get a `consolidated_into=<note_id>` pointer and a recall-priority demotion. No tombstoning, no rewriting history, fully auditable.

**Layer.** Persona-runtime + episodic-memory change. Adds one column (`consolidated_into`) to `episodes`. Reuses [`store_note`](rfcs/0005-persona-agent-memory.md) — which is the *correct* surface for agent-authored consolidation. Composes with [RFC 0009](rfcs/0009-security-sandboxing.md) audit log: the consolidation note carries provenance pointers to its source episodes.

**Cost.** One LLM call per `auto_reflect_after` firing (already gated by the counter — typically minutes-to-hours apart). Cheaper than RFC 0025's background pipeline because it's event-driven, not corpus-wide.

**Why this matters.** Addresses [root cause #1](#root-causes-of-the-dementia-feel) at a different layer than [§A](#a-promote-key_facts-to-a-declarative-fact-tier) — facts are extracted at close, consolidations happen during reflection. The two compose. Also resolves the audit-trail concern under [RFC 0025's tombstoning](#rfc-0025--thematic-episode-clustering-cluster--merge--tombstone).

**Eventual home.** v0.4.0 RFC. Replaces RFC 0025 in scope and motivation.

### F. Structured "since we last spoke" prompt header

**Idea.** A small fixed-shape block injected on every event in a known-participant scope:

```
since-we-last-spoke:
  last_seen_ago: 3 days
  interactions_total: 14
  trust_trajectory: rising
  open_commitments_count: 2
  last_topic: "rate card negotiation"
```

[RFC 0021 P1](rfcs/0021-persona-temporal-awareness.md) already exposes the timestamps and trust signals; this is purely a rendering change in [`memory_context.py`](../agents/persona_runtime/memory_context.py).

**Layer.** Prompt assembly only. No schema, no scoring, no LLM cost.

**Cost.** ~50 tokens per event.

**Why this matters.** Addresses [root cause #5](#root-causes-of-the-dementia-feel). Eliminates a whole class of "stranger every conversation" failures by giving the LLM a fixed-shape anchor instead of asking it to parse temporal context out of prose summaries.

**Eventual home.** Single PR; sized for [RFC 0021 P1](rfcs/0021-pr-plan.md) or a follow-on patch. No new RFC.

### G. Memory-of-context dogfood test

**Idea.** A new manual-test slot under [`docs/manual-tests/`](manual-tests/) that scripts a five-interaction scenario over 30 minutes covering one named entity, one stated preference, and one explicit commitment. Pass criterion: the persona references each of the three when an appropriate trigger appears later, *without keyword overlap*. Fail criterion: the persona re-introduces the fact, asks for it again, or contradicts it.

**Layer.** Manual-test artifact + a small driver script. No code change to the persona runtime. Should be re-run before each release that touches memory.

**Cost.** Author once; ~20 minutes per release to execute manually.

**Why this matters.** `recall@k` is the wrong gate for the dementia failure mode. Without an explicit qualitative test, every memory RFC will pass its own metric and the user will still report dementia. This is the gate.

**Eventual home.** Manual-test doc, authored alongside any of A–F that ships. Naming: `MT-MEMORY-005-dementia-test.md`.

---

## Recommended sequencing

| Order | Action | Vehicle | Target |
|------:|--------|---------|--------|
| 1 | Land RFC 0020 P3 (channel-scoped interactions) | already on critical path | v0.3.0 |
| 2 | Pin [§D](#d-outcome-tagged-importance-not-turn-count-importance) (outcome tags) into RFC 0020 P2 PR plan | RFC 0020 OQ #6 resolution | v0.3.0 |
| 3 | Ship [§B](#b-continuity-bridge-across-interaction-close) and [§F](#f-structured-since-we-last-spoke-prompt-header) as small follow-on PRs | no RFC; ~1 PR each | v0.3.0 or v0.3.x |
| 4 | Author [§G](#g-memory-of-context-dogfood-test) as a manual-test artifact | docs PR | v0.3.0 release-prep (Phase 4) |
| 5 | Pull `key_facts` out of draft RFC 0023 into a "declarative-fact tier" RFC ([§A](#a-promote-key_facts-to-a-declarative-fact-tier)) | new RFC | v0.3.x |
| 6 | Trim draft RFC 0023 to JSON-summary schema only; move recency boost to [`0008-calibration-review.md`](rfcs/0008-calibration-review.md) | scope amendment | v0.3.x |
| 7 | Fold [§C](#c-salience-score-with-use-based-reinforcement) into the [RFC 0008 calibration review](rfcs/0008-calibration-review.md) | review-time formula change | v0.3.x (calibration window close) |
| 8 | Defer draft RFC 0024 (vector recall) until [§G](#g-memory-of-context-dogfood-test) data shows BM25 misses on multi-turn summaries | gate, not a deliverable | v0.3.x or v0.4.0 |
| 9 | Replace draft RFC 0025 with [§E](#e-reflection-driven-consolidation-not-llm-clustering) | new RFC; supersede 0025 | v0.4.0 |

The throughline: stop treating memory as a retrieval problem and start treating it as a representation problem. Vectors don't fix prose; structure does.

---

## Open questions

1. **Does [§A](#a-promote-key_facts-to-a-declarative-fact-tier) need a separate `facts` table, or is it a typed view over `notes`?** Notes are agent-authored prose; facts are framework-extracted tuples with a different access pattern (subject-indexed, not query-matched). Lean toward separate table.

2. **Entity recognition for [§A](#a-promote-key_facts-to-a-declarative-fact-tier).** Naive: ask the summarizer LLM to emit subjects in the structured close output. Alternative: a deterministic NER pass first. Naive is cheaper; deterministic is more auditable. Defer to the RFC.

3. **Fact retraction.** A user might say "her name is Lila, not Mira" — the prior fact is wrong. Options: (a) latest-asserted-wins, (b) retraction tuple, (c) confidence-weighted union. Lean toward (a) with [§C](#c-salience-score-with-use-based-reinforcement) reinforcement.

4. **Does [§B](#b-continuity-bridge-across-interaction-close) blow the memory budget on busy channels?** Two summaries × ~500 tokens = 1000 tokens against [RFC 0017's 1500-token budget](rfcs/0017-persona-memory-injection-budget.md#open-questions). Mitigation: one summary, or a char cap. Pin in the PR.

5. **Is [§F](#f-structured-since-we-last-spoke-prompt-header) duplicative of [RFC 0021 P1](rfcs/0021-persona-temporal-awareness.md)'s recency rendering?** Depends on the shape P1 lands. Verify after P1 merges; collapse to a P1 follow-up if redundant.

---

## Decision / next steps

1. **Review and ratify (or reject) the assessment in [§Assessment](#assessment-of-draft-rfcs-0023--0024--0025).** This doc is not load-bearing until that happens.
2. **If ratified:** open the [§D](#d-outcome-tagged-importance-not-turn-count-importance) PR against RFC 0020 P2's plan first — it's the smallest, unblocks [§C](#c-salience-score-with-use-based-reinforcement), and lands in v0.3.0 with no scope change.
3. **Author [§G](#g-memory-of-context-dogfood-test) before any of A–F merges.** Without the test, "did dementia get better?" is not answerable.
4. **Communicate the scope change to draft-RFC-0023/0024/0025 authoring** — the user holds those reservations; this doc proposes that 0023 narrows, 0024 defers, 0025 is replaced.

This doc stays at `🔨 Draft` until step 1 lands; on ratification it flips to `📋 Proposed` and a follow-up PR seeds the new "Declarative Facts Tier" RFC plus the v0.4.0 reflection-consolidation RFC.

---

## Related documentation

- [RFC 0005 — Persona Agent & Memory System](rfcs/0005-persona-agent-memory.md) — the substrate (working / episodic / relationship; `auto_reflect_after`; `store_note`).
- [RFC 0008 — Agent Memory & Context Optimization](rfcs/0008-agent-memory-context-optimization.md) — context budget, eviction/decay, scoring seam.
- [RFC 0008 Calibration Review](rfcs/0008-calibration-review.md) — landing point for [§C](#c-salience-score-with-use-based-reinforcement).
- [RFC 0017 — Persona Memory Injection Token Budget](rfcs/0017-persona-memory-injection-budget.md) — `MemoryBudget`; tier composition rule.
- [RFC 0020 — Interaction Lifecycle](rfcs/0020-interaction-lifecycle.md) — interaction-bounded episodes; OQ #6 (outcome tags) resolved by [§D](#d-outcome-tagged-importance-not-turn-count-importance).
- [RFC 0021 — Persona Temporal Awareness](rfcs/0021-persona-temporal-awareness.md) — temporal data feeding [§F](#f-structured-since-we-last-spoke-prompt-header).
- [v0.3.0 plan](v0.3.0-plan.md) — the milestone this doc shapes.
- [ROADMAP.md §v0.3.0](../ROADMAP.md#v030--agent-conversations) and [§v0.4.0](../ROADMAP.md) — version targets.
- [docs/rfcs/README.md](rfcs/README.md) — RFC process and lifecycle.
