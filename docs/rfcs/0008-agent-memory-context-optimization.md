# RFC 0008 — Agent Memory and Context Optimization

**Type**: architecture  
**Status**: � Accepted  
**Author**: Engineering Team  
**Date**: 2026-04-15  
**Target**: v0.2  
**Depends on**: RFC 0005, RFC 0006

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Current State and Gaps](#a-current-state-and-gaps)
  - [B. Memory for All Agent Types](#b-memory-for-all-agent-types)
  - [C. Context Budget as a Scheduler Primitive](#c-context-budget-as-a-scheduler-primitive)
  - [D. Context Packaging and Compression Pipeline](#d-context-packaging-and-compression-pipeline)
  - [E. Delegation Contract and Merge Semantics](#e-delegation-contract-and-merge-semantics)
  - [F. Persona Context Sanity and Helper Agents](#f-persona-context-sanity-and-helper-agents)
  - [G. Memory Eviction, Decay, and Validation](#g-memory-eviction-decay-and-validation)
  - [H. Shared vs Isolated Memory](#h-shared-vs-isolated-memory)
  - [I. Rollout Timing Recommendation](#i-rollout-timing-recommendation)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions-resolved)
- [Open Questions (Post-Acceptance)](#open-questions-resolved-post-acceptance)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

This RFC extends memory and context-optimization from persona agents to all agent types (task, persona, and future sub-agents), and makes context budget allocation explicit in orchestration. The core design is: the caller prepares a minimal context package, delegates to a subtask with an allocated token budget, and receives a structured result envelope that can be merged deterministically.

The objective is to reduce hallucinations and runaway context growth by default: smaller, scoped windows for delegated tasks; aggressive relevance filtering; and compression before prompt injection. This RFC also defines memory decay and eviction rules, plus shared-vs-isolated memory boundaries for swarm safety.

## Motivation

Persatrix already has strong memory building blocks, but they are persona-centric and not yet integrated with orchestrator-level context budgeting.

What already exists:

1. Persona memory stack (working, episodic, relationship) is implemented and used in runtime (`agents/persona_runtime.py`, `agents/memory/*`).
2. Episodic note tools exist (`store_note`, `recall_notes`, `update_note`, `delete_note`) and are agent-scoped.
3. Working memory already supports section-level token estimation and compression.
4. RFC 0006 identifies budget and execution-limit enforcement as a prerequisite to safe scale.

What is missing for this use case:

1. Task agents do not have framework-level memory injection/retrieval strategy; they are largely stateless per request.
2. Scheduler currently sends broad prior outputs (`context map`) rather than relevance-filtered packages.
3. No first-class context budget allocator in scheduler/executor for prompt-construction tokens.
4. No caller-subagent return contract for merge-safe delegated execution.
5. No unified stale-memory policy (decay/eviction/validation) across memory classes.
6. No clear shared-memory pools with policy boundaries; current model is mostly isolated per agent ID.

If unchanged, context windows continue to grow organically and delegation quality degrades with scale. This increases hallucination risk and token waste.

## Goals

1. Enable memory usage patterns for non-persona agents without forcing full persona runtime semantics.
2. Make context budget a first-class orchestration resource, allocated per step/subtask.
3. Add a caller-owned context packaging pipeline: retrieve, score relevance, compress, and inject only necessary state.
4. Define a strict delegation return contract with deterministic merge behavior.
5. Keep persona runtime contexts compact; shift long-lived state to stored memory with quick retrieval.
6. Implement memory eviction, freshness decay, and optional revalidation for procedural memory.
7. Support both isolated memory and opt-in shared memory pools with policy guardrails.
8. Provide observability for context assembly quality, compression ratio, and merge outcomes.

## Non-Goals

- Replacing the existing three-tier memory architecture from RFC 0005.
- Perfect semantic relevance selection in v1; heuristic scoring is acceptable initially.
- Vector database dependency in the first implementation slice.
- Cross-repository or internet-wide shared memory.
- Full autonomous policy redesign for persona behavior.

---

## Design / Implementation

### A. Current State and Gaps

| Area | Current behavior | Gap |
|------|------------------|-----|
| Task agent memory | Stateless `_run_llm_loop` + task payload/context | No memory retrieval/injection policy |
| Persona memory | Implemented and injected in runtime | Not reusable by task agents via common orchestration path |
| Scheduler context | Sends accumulated prior outputs | Includes irrelevant history; no budget-aware pruning |
| Compression | WorkingMemory section compression exists | No orchestrator-level pre-delegation compression step |
| Delegation | Action type exists; spawner is TODO | No context contract, output envelope, or merge policy |
| Memory sharing | Agent-scoped storage enforced | No explicit shared pool model for coordinated swarms |

### B. Memory for All Agent Types

Introduce a lightweight `MemoryFacade` abstraction usable by all agents without flattening tier-specific APIs.

Design rules:

1. Keep RFC 0005 tier interfaces (`WorkingMemory`, `EpisodicMemory`, `RelationshipMemory`) intact.
2. Add orchestration-facing facade methods for common operations:
   - `retrieve_relevant(query, limit, scope)`
   - `store_observation(entry, scope, ttl)`
   - `store_procedure(key, content, confidence, expires_at)`
   - `list_candidates(task_context)`
3. For task agents, instantiate `EpisodicMemory` + optional lightweight `WorkingSet` per task execution, without persona state machine.
4. Relationship memory remains optional and mainly persona-oriented.

This provides memory capability to non-persona agents while preserving existing domain-specific memory classes.

### C. Context Budget as a Scheduler Primitive

Define explicit context budget allocation for each dispatch:

- `budget_total_tokens`
- `budget_input_tokens`
- `budget_output_reserve_tokens`
- `budget_memory_tokens`
- `budget_tool_round_tokens`

Allocator policy:

1. Workflow budget from RFC 0006 is the hard cap.
2. Scheduler derives per-step budget from step criticality and estimated complexity.
3. Delegated subtasks receive independent sub-budgets to prevent parent-context bloat.
4. Retry attempts consume the same budget pool (no fresh context-budget reset).

This treats token context the same way an OS treats CPU/memory quotas.

**Phase 1 budget derivation strategy**: Current workflow YAML steps have no `criticality` or `complexity` fields (the schema defines `id`, `agent`, `input`, `depends_on`, `condition`, `output_key`, `approval_required`). Phase 1 uses a simple heuristic: equal split of the workflow context budget across steps, with an optional per-step `context_budget` override in workflow YAML for callers that know certain steps need more headroom. Complexity-based derivation (inferring budget from agent type, dependency fan-in, or input length) can be added in a later phase once production allocation patterns are observed. See Open Question 8 (resolved).

**Retry budget persistence**: Because retry attempts consume the same budget pool (rule 4), the orchestrator must persist the remaining context budget in step state between retry attempts. This differs from the current executor behavior where each retry gets a fresh execution timeout (`context.WithTimeout` per dispatch). The step-state entry for context budget must be updated after each attempt to reflect tokens consumed, so the next attempt sees the true remaining budget.

### D. Context Packaging and Compression Pipeline

Before dispatching to any agent (especially delegated sub-agents), caller builds a `ContextPackage`.

Pipeline:

1. Candidate collection: depends-on outputs, memory recalls, pinned constraints, and task-local state.
2. Relevance scoring: dependency proximity + lexical overlap + recency + importance.
3. Compression:
   - Extractive pruning first.
   - Abstractive summarization second if still over budget.
4. Budget fit check with deterministic truncation order.
5. Injection into dispatch payload as explicit sections.

Compression objective:

$$
\text{compression\_ratio} = \frac{\text{tokens\_before}}{\text{tokens\_after}}
$$

Selection objective:

$$
\max \sum_i \text{relevance}_i \cdot x_i \quad \text{s.t.} \quad \sum_i \text{tokens}_i \cdot x_i \leq B
$$

where $B$ is the step memory/context budget.

**Phase 1 algorithm**: This is a 0/1 knapsack problem (NP-hard in the general case). Phase 1 uses greedy selection by descending relevance-per-token density (`relevance_i / tokens_i`), which is O(n log n) and gives a well-understood approximation. The exact optimum is not required at this stage; heuristic scoring quality (Open Question 1) dominates solution quality far more than the gap between greedy and optimal selection.

**Compression LLM cost accounting**: The abstractive summarization step (pipeline step 3b) requires an LLM call. This call is charged to a separate orchestrator overhead budget — it does not consume from the dispatching task's `budget_input_tokens` or `budget_tool_round_tokens`. Compression calls are recorded in step metadata (`context_compression_tokens`, `context_compression_model`) for observability and cost attribution. If the overhead budget is exhausted, the pipeline falls back to extractive-only compression and emits a warning metric.

### E. Delegation Contract and Merge Semantics

Define explicit caller-subagent contracts.

`DelegationRequest` (from caller to sub-agent):

1. Objective and acceptance criteria.
2. Context package (already filtered/compressed by caller).
3. Resource budget (tokens, timeout, LLM calls).
4. Allowed tools and permission scope.
5. Required output schema.

`DelegationResult` (sub-agent to caller):

1. `summary`: `str`
2. `artifacts`: `dict[str, Any]` (structured outputs keyed by artifact name)
3. `decisions`: `list[str]` (assumptions and rationale)
4. `memory_writes`: `list[MemoryWriteEntry]` (suggested durable memory updates — see schema below)
5. `risks`: `list[str]`
6. `status`: `str` (e.g. `"completed"`, `"partial"`, `"failed"`)

`memory_writes` entry schema (each element in the list):

```python
{
    "tier": "episodic" | "notes",   # which memory tier to write to
    "key": str | None,              # stable key (required for notes; optional for episodic)
    "content": str,                 # text content to store
    "importance": float,            # 0.0–1.0; caller caps unverified sub-agent writes at 0.8
    "ttl_seconds": int | None,      # None = persist until evicted by size policy
    "tags": list[str],              # used for retrieval filtering
    # "source_agent" is injected by the framework; sub-agents must not set it
}
```

Caller validation rules for `memory_writes`:
- Reject entries missing required fields or with `tier` outside the allowed set.
- Downscale `importance` if it exceeds the caller's configured trust ceiling for this sub-agent.
- Apply the declared merge strategy (`replace`, `append`, `patch`, `reject_on_conflict`) when an entry with the same `key` already exists.

Merge behavior:

1. Schema validation is mandatory before merge.
2. Merge strategy is declared by caller (`replace`, `append`, `patch`, `reject_on_conflict`).
3. Conflict events are logged and visible in run metadata.

This avoids implicit, lossy merge behavior and prevents caller context corruption.

### F. Persona Context Sanity and Helper Agents

Persona agents should not keep large active context windows continuously.

Principles:

1. Persona active working context stays compact and task-focused.
2. Most history and procedural state remain in durable memory.
3. Retrieval is just-in-time per event/tick, not persistent long transcript carryover.

Note: `agents/memory/working.py` already implements section-level pinning via `ContextSection.compressible = False`. The "pinned, non-compressible section" referenced in Security Considerations item 3 maps directly to this existing mechanism on the Python side. The orchestrator-level `ContextPackage` needs a parallel `pinned_sections` field that the compression pipeline passes through untouched — this is new work, but the design pattern is already established.

Optional helper pattern (recommended, not mandatory):

- Introduce dedicated helper task-agent roles for personas, such as:
  - `memory-curator` (summarize and deduplicate)
  - `procedure-validator` (revalidate stale procedural memory)
  - `context-packer` (prepare delegation packages)

This aligns with the manager-specialist delegation model and keeps persona decision loops lean.

### G. Memory Eviction, Decay, and Validation

Add standardized memory lifecycle policy:

1. **Eviction**:
   - Hard TTL for low-importance episodic entries.
   - Size cap with LRU-importance hybrid pruning.
2. **Decay**:
   - Procedural memory confidence decays over time.
   - Confidence refresh on successful reuse.
3. **Revalidation**:
   - If confidence falls below threshold, mark memory stale and require validation before injection.

Example decay:

$$
c_t = c_0 \cdot e^{-\lambda t}
$$

Inject only if $c_t \ge c_{min}$ or recently validated.

**Default parameter values** (configurable per agent via `config/agents.yaml`):

| Parameter | Default | Rationale |
|-----------|---------|----------|
| $\lambda$ (decay rate) | 0.01 per day (half-life ≈ 69 days) | Slow enough that regularly-used memories stay relevant; fast enough that unused memories fade within a quarter |
| $c_{min}$ (eviction threshold) | 0.1 | Below this, entries are evicted on the next cleanup pass rather than just flagged stale |
| Hard TTL (importance < 0.3) | 30 days | Low-importance entries that haven't been accessed are cleaned up after one month |
| Episodic memory cap | 1000 per agent | Notes already cap at `max_notes=500`; episodes need a comparable bound to prevent unbounded growth |
| Eviction scoring formula | `importance × 0.6 + recency_norm × 0.3 + access_freq_norm × 0.1` | Weights importance highest (core signal), recency second (temporal locality), access frequency lowest (avoids over-weighting repeated but low-value lookups) |

The eviction scoring formula replaces the current notes-only `ORDER BY access_count ASC, created_at ASC` approach with a weighted hybrid that considers importance — the existing purely LRU+age ordering does not account for entry importance. These defaults are starting points; see Open Question 12 (resolved) for calibration approach.

### H. Shared vs Isolated Memory

Support both models with explicit policy:

1. **Isolated (default)**: per-agent namespace, no cross-agent reads/writes.
2. **Shared pool (opt-in)**: designated group namespace with ACL and provenance tags.
3. **Hybrid**: isolated writes with curated publish to shared pool.

Safety constraints:

1. Shared memory writes require schema and provenance fields (`source_agent`, `created_at`, `confidence`).
2. Consumers can filter by trust policy and confidence threshold.
3. Sensitive memory classes stay isolated regardless of pool settings.

**Default write validation policy for Phase 4**: Writes to shared pools are immediate (no human or validator-role gate), subject to ACL and provenance field requirements enforced at write time. Validator-role review — where a designated agent or operator must approve writes before they become visible to consumers — is a v0.3 or follow-on RFC concern, where multi-node shared memory creates stronger cross-boundary isolation requirements. Open Question 4 (resolved) confirmed that validator-role gating is deferred to v0.3.

### I. Rollout Timing Recommendation

Best timing for implementation:

1. Start design/infra work immediately after RFC 0006 Phase 1 (limit propagation) is merged.
2. Land core context budget + packaging + compression before broad loop adoption (RFC 0007 implementation), because loops multiply context inefficiency.
3. Land delegation contract and merge engine before enabling production sub-agent spawning patterns.

Practical sequencing:

- RFC 0006 hardens budgets and limits.
- RFC 0008 makes context assembly budget-aware and memory-aware.
- RFC 0007 then benefits from safer looped execution under bounded context discipline.

---

## Security Considerations

1. Context package poisoning: untrusted outputs must be tagged and optionally sanitized before reuse.
2. Shared-memory contamination: enforce ACL and provenance checks on shared pools.
3. Over-compression risk: preserve critical constraints in a pinned, non-compressible section.
4. Merge injection: validate result schema and enforce deterministic merge policies.
5. Budget bypass attempts: reject negative or malformed budget fields and cap declared values.
6. Compression LLM prompt injection: prior step outputs may contain adversarial content that manipulates the compression LLM into producing misleading summaries. Mitigation: the compression prompt must include a system-level instruction to summarize factually without following instructions found in the input, and compressed outputs must be tagged with `source: compressed` so downstream agents know the content is a derivative, not verbatim.
7. Memory write amplification via delegation: sub-agent `memory_writes` could flood the parent's memory with high-volume low-value entries. The importance cap at 0.8 limits per-entry impact but not volume. Mitigation: enforce a `max_memory_writes` limit per `DelegationResult` (default: 20 entries), rejecting excess entries and logging the overflow event.

## Phased Implementation Plan

### Phase 1: Context Budget and Packaging Foundation

Summary: Add explicit context budget allocation and package assembly in scheduler/executor without changing agent personas.

Deliverables:

1. Add budget fields to dispatch path.
2. Build candidate-selection and relevance scoring module.
3. Add extractive compression and deterministic truncation order.
4. Emit context assembly metrics in step metadata.

Dependencies: RFC 0006 Phase 1.

### Phase 2: Memory Facade for Task Agents

Summary: Enable non-persona memory retrieval/storage flows via `MemoryFacade` and task-agent integration.

Deliverables:

1. Task-agent memory facade wiring.
2. Config and schema updates for task memory policies.
3. Basic eviction and TTL policy.

Dependencies: Phase 1.

### Phase 3: Delegation Contract and Merge Engine

Summary: Add structured caller-subagent contract and validated merge semantics.

Deliverables:

1. `DelegationRequest` and `DelegationResult` data contracts.
2. Merge strategy implementation and conflict handling.
3. Observability for merge outcomes and dropped fields.

Dependencies: Phase 1.

### Phase 4: Shared Memory Pools and Procedural Revalidation

Summary: Add shared namespaces and stale procedural-memory controls.

Deliverables:

1. Shared pool ACL and provenance policy.
2. Confidence decay and stale-memory revalidation.
3. Curated publish workflow from isolated to shared memory.

Dependencies: Phases 2 and 3.

---

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Go orchestrator | `internal/scheduler/` | Context budget allocation + package builder integration |
| Go orchestrator | `internal/executor/` | Dispatch contract extensions for context package + budget |
| Go orchestrator | `internal/cost/` | Budget accounting extended with context package metrics |
| Go orchestrator | `internal/state/` | Store context assembly, compression, and merge metadata |
| Python agents | `agents/task_agent.py` | Memory facade usage for non-persona agents |
| Python agents | `agents/memory/` | Shared policies, decay/eviction, facade module |
| Python agents | `agents/sub_agents/` | Contract-aware delegation and merge support |
| Protos | `proto/task.proto` | No changes in Phase 1–2; Phase 3 adds typed fields for context package and delegation envelopes once the schema is stable (see Open Question 2, resolved). Proto changes require RFC review per project policy. |
| Config | `config/agents.yaml`, `schemas/agent.schema.json` | Task-agent memory policy and shared pool configuration |
| Tests | `tests/unit/`, `tests/integration/` | Context budget, compression, delegation merge, shared memory policy tests |

## Test Strategy

- **Unit tests**: relevance scoring, compression ordering, decay math, merge conflict policy.
- **Integration tests**: scheduler -> executor -> agent dispatch with budgeted context package.
- **Delegation tests**: caller package construction, sub-agent output schema validation, deterministic merge outcomes.
- **Memory safety tests**: shared pool ACL, provenance filtering, isolation guarantees.
- **Regression tests**: ensure persona behavior remains stable while task memory support is added.

## Open Questions (Resolved)

### 1. Relevance scoring approach — Heuristic-only in Phase 1; pluggable scoring interface

**Decision**: Context relevance scoring is heuristic-only in Phase 1. The scoring module exposes a `RelevanceScorer` protocol so that embedding-based scoring can be swapped in as an alternative backend without changing the selection pipeline.

**Rationale**: The RFC's Non-Goals already state "Perfect semantic relevance selection in v1; heuristic scoring is acceptable initially" and "Vector database dependency in the first implementation slice." The existing episodic memory (`agents/memory/episodic.py`) already demonstrates that FTS5 + BM25 combined with importance/recency/access-count weighting produces useful relevance ranking without embeddings. Section D's knapsack analysis confirms that "heuristic scoring quality dominates solution quality far more than the gap between greedy and optimal selection" — investing in embedding infrastructure before the heuristic is proven in production would be premature optimization.

The Phase 1 heuristic combines four signals: (a) dependency proximity (steps directly upstream score highest), (b) lexical overlap via FTS5 match scoring, (c) recency (more recent outputs score higher), and (d) importance tags from memory entries. This covers the dominant retrieval patterns for task delegation contexts. A `RelevanceScorer` protocol with a `score(candidate, query_context) -> float` signature allows a future embedding backend (RFC 0005 already identifies "Semantic Search via Vector Embeddings" as a future episodic memory enhancement) to be plugged in without modifying the selection or knapsack logic.

### 2. Proto context fields — Defer typed fields to Phase 3; use existing `context` map in Phases 1–2

**Decision**: `TaskRequest.context` remains a `map<string, string>` through Phases 1–2. Phase 1 passes the `ContextPackage` as a serialized JSON string under a reserved key (`_context_package`) in the existing map. Typed proto fields for `ContextPackage` and `DelegationEnvelope` are added in Phase 3 after the schema shape is proven.

**Rationale**: Proto changes are cross-language (Go + Python), require RFC review per project policy, and create a versioning obligation. The `ContextPackage` schema will evolve during Phase 1–2 implementation as real compression ratios, relevance scoring behavior, and budget allocation patterns are observed. Changing the proto prematurely risks multiple revision cycles. The existing `context` map already carries arbitrary string key-value pairs between orchestrator and agents — adding a `_context_package` key with a JSON-serialized payload is backward-compatible and requires zero proto changes. Agents that do not understand the key ignore it; agents that do parse it gain structured context. The underscore prefix signals a framework-managed key that agents should not set directly.

Phase 3 introduces `DelegationRequest` and `DelegationResult` contracts (Section E) which define the most demanding proto requirements. By that point, the `ContextPackage` shape will have been validated through two implementation phases, and a single proto change can introduce both typed context and delegation envelope fields together — reducing total proto revision count.

### 3. Compression warning threshold — Warn at 4:1, hard-cap at 10:1

**Decision**: Emit a `high_compression_ratio` warning metric when `compression_ratio ≥ 4.0` (75% token reduction). Hard-cap compression at `10:1` (90% reduction) — if extractive + abstractive compression exceeds this ratio, the pipeline preserves the 10:1 output and logs an `extreme_compression_capped` event. Pinned sections (Section F, `compressible = False`) are excluded from ratio calculation.

**Rationale**: The existing working memory compression (`agents/memory/working.py`) already guards against summaries that are larger than the original, but has no upper-bound quality check. A 4:1 ratio means 75% of the source tokens were removed — at this level, abstractive summarization is likely discarding substantive content rather than just removing redundancy. The warning threshold creates operator visibility without blocking execution.

The 10:1 hard-cap prevents pathological compression where a multi-page context is reduced to a single sentence. At 90% reduction, the compression output is essentially a headline — insufficient for reliable downstream agent reasoning. When the cap triggers, the pipeline stops compressing further and emits the capped output, giving the agent a degraded but bounded context rather than a useless one. The cap is conservative; operators can adjust it via configuration (`optimization.yaml`) once production compression patterns are observed.

Pinned sections are excluded from the ratio because they are non-compressible by design (Section F maps to `ContextSection.compressible = False` in working memory). Including them would artificially lower the observed ratio and mask aggressive compression of the compressible sections.

### 4. Shared pool write policy — Immediate writes with ACL enforcement; validator-role gating deferred

**Decision**: Writes to shared memory pools in Phase 4 are immediate, subject to ACL checks and mandatory provenance fields (`source_agent`, `created_at`, `confidence`) enforced at write time. Validator-role review gating is deferred to v0.3 or a follow-on RFC.

**Rationale**: Section H already proposes this default and the reasoning is sound: Phase 4 shared memory operates within a single orchestrator node where all agents are locally managed and their permissions are deny-by-default. ACL enforcement ensures that only agents with explicit shared-pool write permission can publish, and provenance fields enable consumer-side trust filtering (e.g., "only read entries with `confidence ≥ 0.7` from agents in my organization"). This provides sufficient safety for single-node v0.2 deployment.

Validator-role review — where a designated agent or human operator must approve writes before they become visible — introduces async approval workflows, queue semantics, and potential deadlocks (what if the validator agent is also waiting on shared memory?). This complexity is justified in v0.3's multi-node mesh where shared memory crosses trust boundaries between separately administered nodes, but is premature for v0.2's single-node model. RFC 0009 (Security & Sandboxing) Phase 3 will introduce capability tokens that can further restrict shared-pool access — landing those controls before validator gating provides a stronger foundation for the review mechanism.

### 5. Stale procedural memory — Downgrade confidence and continue; do not block

**Decision**: Stale procedural memory is injected at its decayed confidence value with a `stale_memory_injection` warning log and metric. Execution is not blocked. Operators can configure a `stale_confidence_alert_threshold` (default `0.3`) that triggers an alert when injected memory confidence falls below the threshold.

**Rationale**: The decay formula in Section G ($c_t = c_0 \cdot e^{-\lambda t}$) already provides a continuous confidence signal — blocking at an arbitrary threshold would turn a gradient into a cliff edge, creating unpredictable execution failures when memory ages past the cutoff. Downgrade-and-continue preserves the agent's ability to use partially-relevant context while making the degradation visible through metrics.

The `stale_memory_injection` metric carries the measured confidence value, the memory key, and the agent ID. Operators can set alerting rules on this metric (e.g., "alert if any injection has `confidence < 0.2`") and decide whether to trigger manual revalidation or eviction. This is consistent with RFC 0006's cost observability approach: make resource usage visible and actionable, don't block execution on heuristic thresholds.

Blocking execution would also create a cascading failure risk: if a workflow step depends on procedural memory that has decayed past the threshold, the step fails, which may fail the entire workflow — even if the stale memory was only supplementary context and the agent could have completed the task without it. The confidence value in the injection metadata lets the agent itself decide how much weight to give the memory in its reasoning.

### 6. RFC 0006 dependency — Satisfied; field alignment verified

**Decision**: This dependency is satisfied. RFC 0006 has reached `👍 Accepted` status with all open questions resolved. The `budget_*` field names in Section C are compatible with RFC 0006's `TaskConfig` fields and budget enforcement model. No revision needed.

**Rationale**: RFC 0006's resolved open questions confirm: (a) budget enforcement uses actual post-dispatch token counts with a pre-dispatch heuristic guard based on `max_tokens`, (b) cost tracking operates at the orchestrator level via `BudgetEnforcer` and `TokenCounter`, and (c) per-step execution metadata records token usage and cost. RFC 0008's `budget_total_tokens`, `budget_input_tokens`, and `budget_output_reserve_tokens` extend this model to context assembly — they are *additional* budget dimensions that the scheduler allocates alongside RFC 0006's execution limits, not replacements for them.

The "overhead budget" model in Section D (compression LLM calls charged to a separate orchestrator budget) is orthogonal to RFC 0006's per-task budget — RFC 0006 budgets govern agent execution cost, while the compression overhead budget governs orchestrator-side context preparation cost. Both feed into the same `CostReporter` for aggregate tracking, but their enforcement paths are independent.

At Phase 1 implementation start, verify that RFC 0006's `TaskConfig` field names (`max_llm_calls`, `max_tokens`, `timeout_seconds`) have not been renamed during implementation. If they have, update Section C's field names to match.

## Open Questions (Resolved, Post-Acceptance)

*Added during post-acceptance review. These specification gaps have been resolved. None invalidate the accepted design.*

### 7. MemoryFacade lifecycle for task agents — Per-process with serialized access

**Decision**: `MemoryFacade` for task agents uses a per-process lifecycle, consistent with how persona agents already manage memory. A single `EpisodicMemory` instance is created per task-agent process, initialized in `server.py` `start()`, and closed in `stop()`. Concurrent gRPC calls share the instance. No connection pooling is needed in Phase 2; the existing aiosqlite WAL-mode connection with Python's GIL and asyncio's cooperative scheduling provides sufficient serialization.

**Rationale**: The persona agent pattern in `persona_runtime.py` already proves per-process memory works: `_LLMPersonaAgent` holds a single `EpisodicMemory` instance across all events, serialized by an `asyncio.Lock`. Task agents can adopt the same model. The key observations are:

1. **Per-task instantiation is unacceptable.** `EpisodicMemory.initialize()` runs SQLite `PRAGMA journal_mode=WAL`, FTS5 availability checks, and migration verification on every call. Under load (e.g., 50 concurrent workflow steps dispatching to the same agent), this would mean 50 SQLite open/close cycles, each with schema validation overhead, plus file descriptor churn.

2. **Connection pooling is unnecessary for v0.2.** aiosqlite serializes all operations through a single background thread. Adding a connection pool would require switching to a multi-connection SQLite setup with careful WAL checkpoint coordination — complexity that is not justified when the current single-connection model handles persona agent workloads. If profiling later shows the single connection is a bottleneck, pooling can be added behind the `MemoryFacade` interface without changing callers.

3. **Concurrency safety is already handled.** Task agents' `_run_llm_loop()` is async but sequential per call (no parallel tool execution yet — marked TODO v0.2). Multiple concurrent gRPC calls to the same task agent will interleave at `await` points, but aiosqlite's internal serialization prevents data races. If parallel tool execution is added later, a `MemoryFacade`-level `asyncio.Lock` (mirroring persona's `_lock`) can be added.

The `MemoryFacade` exposes `initialize()` and `close()` matching the existing `MemoryLifecycle` protocol. `server.py`'s `start()`/`stop()` loops already iterate over agents and call lifecycle methods for persona agents — extending this to memory-enabled task agents is a minimal change.

### 8. Context budget derivation algorithm — Equal-split with per-step override

**Decision**: Phase 1 uses equal-split of the workflow context budget across steps, with an optional per-step `context_budget` field in workflow YAML for explicit overrides. Complexity-based derivation is deferred until production allocation data is available.

**Rationale**: The current scheduler (`internal/scheduler/scheduler.go`) has zero budget derivation logic — `executeStep()` passes the full `outputsCopy` map (all accumulated prior step outputs) as context without any pruning or allocation. The `planner.Step` struct and `schemas/workflow.schema.json` have no budget-related fields. Introducing a sophisticated derivation algorithm at this stage would be speculative — there is no production data on which steps systematically need more context.

Equal-split is the simplest correct strategy:

$$
B_{step} = \frac{B_{workflow\_context}}{N_{steps}}
$$

where $B_{workflow\_context}$ is the total context budget from workflow config and $N_{steps}$ is the step count. This gives every step an equal share and is trivially implementable in the scheduler.

The per-step `context_budget` override allows workflow authors to manually allocate more budget to steps they know are context-heavy (e.g., a code review step that needs full prior implementation output). This override takes priority over equal-split and is subtracted from the pool before the remaining budget is divided among non-overridden steps:

$$
B_{remaining} = B_{workflow\_context} - \sum_{i \in \text{overridden}} B_{i,\text{override}}
$$

$$
B_{step,\text{non-overridden}} = \frac{B_{remaining}}{N_{steps} - |\text{overridden}|}
$$

This coordinates with RFC 0006's planned schema additions (`timeout_seconds`, `max_llm_calls`, `max_tokens` per step). Both RFCs need `Step` struct and workflow schema changes — the `context_budget` field should be added alongside RFC 0006's fields to minimize schema revision count.

### 9. Compression pipeline latency and availability — 10s timeout with extractive fallback

**Decision**: Abstractive compression has a 10-second timeout. On timeout, model unavailability, or any LLM call failure, the pipeline falls back to extractive-only compression and emits a `compression_fallback` metric with the failure reason. Abstractive compression runs synchronously on the dispatch critical path for all steps in Phase 1.

**Rationale**: Three distinct failure modes require explicit handling:

1. **Timeout.** The existing agent-side `WorkingMemory.compress_if_needed()` is async fire-and-forget — it can take arbitrarily long because the persona runtime is not on a dispatch critical path. The orchestrator-level compression pipeline is different: it blocks dispatch. A 10-second timeout balances compression quality against dispatch latency. The `compression_model` is expected to be a fast, cheap model (existing working memory uses `claude-haiku-4`), where 10 seconds covers >99th percentile latency for single-call summarization. If the model is consistently slow, operators should switch to a faster model in `optimization.yaml` rather than raising the timeout.

2. **Model unavailability.** Budget exhaustion (overhead budget depleted) and model unavailability (API errors, rate limits) are different failure modes that produce the same outcome: no abstractive compression. The fallback is identical — use extractive-only output. The `compression_fallback` metric distinguishes between `reason: timeout`, `reason: budget_exhausted`, and `reason: model_error` for operational debugging.

3. **Sync vs async.** Making abstractive compression async (non-blocking dispatch) would require the orchestrator to either (a) dispatch the task with uncompressed context and hope the agent handles it, or (b) implement a two-phase dispatch where compression completes later and the agent receives an updated context mid-execution. Both approaches add significant complexity. Since the compression call targets a fast model and has a bounded timeout, the worst-case dispatch delay is 10 seconds — acceptable for Phase 1. If production data shows compression latency is problematic, Phase 2 can introduce async compression for non-critical steps (steps without `approval_required` or high criticality) while keeping synchronous compression for critical-path steps.

Extractive-only compression (pipeline step 3a — pruning low-relevance candidates by the truncation order defined in Section D) is deterministic and sub-millisecond. It always produces a valid, budget-fitting context package, making it a safe fallback.

### 10. Orchestrator vs agent context assembly boundary — Split ownership (Option 3)

**Decision**: Split ownership in Phase 1. The Go orchestrator budgets and assembles context from data it holds (prior step outputs from the `outputs` map, workflow metadata). The Python agent manages its own memory retrieval using `budget_memory_tokens` as an advisory limit passed in the `_context_package` payload. No new gRPC calls or proto changes.

**Rationale**: The architecture boundary is clear — the orchestrator holds step outputs (`scheduler.go`'s `outputsCopy`), while agent memory (episodic, notes, working) lives exclusively in Python agent processes (`agents/memory/`). There is no existing mechanism for the orchestrator to query agent memory, and adding one would require proto changes that Open Question 2 explicitly defers to Phase 3.

Option 1 (soft enforcement with trust) is insufficient because it provides no mechanism for the agent to even know what the budget is — `budget_memory_tokens` would be advisory but invisible to the agent's memory retrieval code. Option 2 (hard enforcement via gRPC memory query) requires proto changes and introduces a new cross-boundary call pattern that contradicts the project's component boundary principle: "Python agents own LLM interaction, tool execution, persona behavior, memory."

Option 3 respects the existing boundaries:

- **Orchestrator responsibility**: Apply relevance scoring and compression to step outputs (which it already holds in the `outputs` map). Enforce `budget_input_tokens` on the step-output portion of the context package. Pass `budget_memory_tokens` as part of the `_context_package` JSON payload.
- **Agent responsibility**: Use `budget_memory_tokens` to limit memory retrieval. The `MemoryFacade.retrieve_relevant()` method (Section B) accepts a `limit` parameter — Phase 2 implementation translates the token budget into a retrieval limit using `estimate_tokens()` (already available in `agents/memory/working.py`).

This means memory budget enforcement is advisory in Phase 1, with the agent as a trusted participant. Phase 3's delegation contract (Section E) can formalize enforcement by including memory token usage in the `DelegationResult` envelope, allowing the caller to verify compliance after the fact. If an agent consistently exceeds its memory budget, the orchestrator can flag it in run metadata — consistent with RFC 0006's approach of making resource usage visible rather than blocking execution on heuristic thresholds.

### 11. Merge strategy `patch` semantics — JSON Merge Patch for structured fields; replace for strings

**Decision**: The `patch` merge strategy applies JSON Merge Patch (RFC 7396) to structured fields (`artifacts`, `tags`, and any JSON-typed values in `memory_writes`). For string fields (`content`, `summary`), `patch` behaves identically to `replace`. A `patch` operation on a `memory_writes` entry with the same `key` merges the `tags` list (union) and applies JSON Merge Patch to any JSON-parseable `content`, falling back to `replace` for plain-text content.

**Rationale**: The four merge strategies in Section E serve different use cases: `replace` (overwrite), `append` (accumulate), `reject_on_conflict` (fail-safe), and `patch` (partial update). The first three have unambiguous semantics. `patch` needs a concrete definition because the `DelegationResult` schema mixes structured and unstructured fields.

JSON Merge Patch (RFC 7396) is the right choice for structured fields because: (a) it is a widely-adopted standard with clear semantics (null values delete keys, present values overwrite), (b) Go has `encoding/json` and Python has `json` — no new dependencies, and (c) it handles the primary `patch` use case: a sub-agent returning partial updates to a shared artifact (e.g., updating two fields of a code review result without replacing the entire object).

For string fields, there is no meaningful "partial update" operation that doesn't require diff/patch infrastructure (which would be a disproportionate dependency for this use case). Treating `patch` as `replace` for strings is explicit and unsurprising. If a future use case requires string-level patching (e.g., line-level code edits), it should be introduced as a dedicated `line_patch` strategy with its own diff format, rather than overloading the `patch` strategy.

The `tags` list uses union semantics under `patch` (add new tags, don't remove existing ones) because tags are additive metadata — a sub-agent's `patch` should enrich the tag set, not silently drop tags the caller added.

### 12. Memory eviction parameter calibration — Ship defaults with mandatory metrics collection

**Decision**: Ship the defaults specified in Section G's table ($\lambda = 0.01$/day, $c_{min} = 0.1$, TTL 30 days for importance < 0.3, episodic cap 1000, eviction scoring `importance × 0.6 + recency × 0.3 + access_freq × 0.1`). Phase 4 implementation must emit calibration metrics on every eviction pass. Agent-level overrides in `config/agents.yaml` are available from day one. A calibration review is scheduled after 30 days of production data.

**Rationale**: The defaults are informed by reasonable heuristics but are inherently speculative — no production workload exists yet to validate them. Delaying Phase 4 to find "perfect" defaults would be wasteful because the optimal parameters depend on actual agent behavior patterns (conversation frequency, memory write volume, task diversity) that can only be observed in production.

The mitigation is to make calibration a first-class deliverable of Phase 4, not an afterthought:

1. **Mandatory metrics per eviction pass**: `evictions_count`, `average_confidence_at_eviction`, `average_importance_at_eviction`, `memory_utilization_ratio` (current entries / cap), `oldest_surviving_entry_age_days`, `entries_below_stale_threshold`. These are emitted as structured log entries and exposed via the telemetry pipeline.

2. **Agent-level overrides** are already planned in Section G ("configurable per agent via `config/agents.yaml`"). This means operators can tune parameters per agent type without a code change — a persona agent with frequent interactions may need a higher episodic cap and slower decay, while a task agent doing one-off code reviews may need aggressive TTL and a low cap.

3. **30-day calibration review**: After Phase 4 lands and runs with real workloads for 30 days, the team reviews eviction metrics and adjusts defaults. This is a documentation commitment, not a code gate — it goes in the Phase 4 PR plan as a follow-up task.

The risk of shipping with imperfect defaults is low because all eviction is soft (entries are removed from the store, not from any external system) and reversible (entries could be reconstructed from episodic logs if needed). The risk of *not* shipping eviction is high — unbounded memory growth degrades retrieval quality and increases token waste, which is the core problem this RFC addresses.

### 13. Shared memory ACL without RFC 0009 — Config-based ACL with Python-layer enforcement

**Decision**: Phase 4 implements a minimal config-based ACL mechanism independent of RFC 0009. `config/agents.yaml` gains a `shared_memory_pools` section that defines named pools with explicit read/write agent lists. Enforcement lives in the Python memory layer. RFC 0009 can later upgrade this to capability-token-based ACL without breaking changes.

**Rationale**: RFC 0009 is at `📋 Proposed` status — it has not been accepted, and its implementation timeline is uncertain. Phase 4 of this RFC cannot depend on an unaccepted RFC without risking indefinite blocking. At the same time, launching shared memory without ACL is unacceptable: the project's deny-by-default security policy (`.github/copilot-instructions.md`: "Permissions are deny-by-default") requires explicit authorization for all cross-agent data access.

The config-based ACL provides deny-by-default without RFC 0009:

```yaml
# config/agents.yaml (example structure)
shared_memory_pools:
  team-knowledge:
    readers: ["code-writer", "code-reviewer", "planner"]
    writers: ["code-reviewer", "planner"]
    max_entries: 2000
    required_confidence: 0.5
```

Implementation details:

1. **Enforcement point**: The `MemoryFacade` (or a `SharedMemoryPool` class) checks the calling agent's ID against the pool's ACL on every read/write. This is Python-layer enforcement in `agents/memory/`, consistent with how `agents/tools/permissions.py` already enforces tool permissions.

2. **No orchestrator dependency**: The ACL check happens in the agent process, not in the Go orchestrator. This is correct because memory operations are agent-side (per the component boundary established in Open Question 10). The orchestrator does not need to know about shared memory pools.

3. **Provenance fields remain mandatory**: Every shared-pool write must include `source_agent`, `created_at`, and `confidence` per Section H. The `source_agent` field is framework-injected (not caller-settable) to prevent spoofing.

4. **Upgrade path to RFC 0009**: When RFC 0009 lands capability tokens, the ACL check can be extended to verify a token instead of (or in addition to) the config list. The `SharedMemoryPool` interface stays the same — only the authorization backend changes. The config-based ACL becomes the fallback for environments that haven't adopted capability tokens.

This approach mirrors how the project already handles tool permissions: `config/agents.yaml` defines `permissions.allowed_tools` per agent, enforced in Python by `PermissionGate`. Shared memory ACL follows the same pattern — config-declared, Python-enforced, deny-by-default.

## Decision / Next Steps

Decision: Propose this RFC for review as the v0.2 architecture bridge between RFC 0006 (limits/budgets) and broader delegation/sub-agent scale.

Next steps:

1. Accept sequencing: 0006 → 0008 → 0007 implementation order (RFC 0007 `Depends on` has been updated to reflect this).
2. Create a PR plan file for RFC 0008 after acceptance.
3. Implement Phase 1 first to establish measurable context-budget outcomes.
4. ~~Resolve Pending Open Questions 8, 9, and 10 before Phase 1 implementation begins.~~ All open questions resolved (2026-04-16).

## Related Documentation

- [RFC 0005](0005-persona-agent-memory.md)
- [RFC 0006](0006-efficiency-execution-limits.md)
- [RFC 0007](0007-conditional-looped-workflow-control-flow.md)
- [Roadmap](../../ROADMAP.md)
- [Extension Spec](../persatrix-extension-spec.md)
