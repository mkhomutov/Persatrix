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
- [Open Questions (Pending)](#open-questions-pending)
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

**Phase 1 budget derivation strategy**: Current workflow YAML steps have no `criticality` or `complexity` fields (the schema defines `id`, `agent`, `input`, `depends_on`, `condition`, `output_key`, `approval_required`). Phase 1 uses a simple heuristic: equal split of the workflow context budget across steps, with an optional per-step `context_budget` override in workflow YAML for callers that know certain steps need more headroom. Complexity-based derivation (inferring budget from agent type, dependency fan-in, or input length) can be added in a later phase once production allocation patterns are observed. See Pending Open Question 2.

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

The eviction scoring formula replaces the current notes-only `ORDER BY access_count ASC, created_at ASC` approach with a weighted hybrid that considers importance — the existing purely LRU+age ordering does not account for entry importance. These defaults are starting points; see Pending Open Question 6 for further calibration needs.

### H. Shared vs Isolated Memory

Support both models with explicit policy:

1. **Isolated (default)**: per-agent namespace, no cross-agent reads/writes.
2. **Shared pool (opt-in)**: designated group namespace with ACL and provenance tags.
3. **Hybrid**: isolated writes with curated publish to shared pool.

Safety constraints:

1. Shared memory writes require schema and provenance fields (`source_agent`, `created_at`, `confidence`).
2. Consumers can filter by trust policy and confidence threshold.
3. Sensitive memory classes stay isolated regardless of pool settings.

**Default write validation policy for Phase 4**: Writes to shared pools are immediate (no human or validator-role gate), subject to ACL and provenance field requirements enforced at write time. Validator-role review — where a designated agent or operator must approve writes before they become visible to consumers — is a v0.3 or follow-on RFC concern, where multi-node shared memory creates stronger cross-boundary isolation requirements. Open Question 4 tracks whether an earlier opt-in review gate is needed.

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
| Protos | `proto/task.proto` | No changes in Phase 1–2; Phase 3 adds typed fields for context package and delegation envelopes once the schema is stable (see Open Question 2). Proto changes require RFC review per project policy. |
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

## Open Questions (Pending)

*Added during post-acceptance review. These are specification gaps that must be resolved before or during their respective implementation phases. None invalidate the accepted design.*

### 7. MemoryFacade lifecycle for task agents — Resolve before Phase 2

**Problem**: Task agents are stateless per-request. Section B proposes `EpisodicMemory` + optional `WorkingSet` per task execution, but does not specify the instantiation lifecycle:
- **Per-process** (shared across tasks): Efficient, but requires concurrent access management — `EpisodicMemory` uses aiosqlite with a single connection.
- **Per-task** (instantiated and torn down per gRPC call): Simple, but adds SQLite open/close latency per call and risks file descriptor exhaustion under load.

**Recommendation**: Per-process with connection pooling, consistent with how persona agents already manage memory. Document the concurrency model and add a `MemoryFacade.close()` cleanup hook.

### 8. Context budget derivation algorithm — Resolve before Phase 1

**Problem**: Section C states "Scheduler derives per-step budget from step criticality and estimated complexity" but current workflow YAML steps have no `criticality` or `complexity` fields (see `schemas/workflow.schema.json`). Without a concrete derivation algorithm, Phase 1 implementers must invent one.

**Recommendation**: Phase 1 uses equal-split of workflow budget across steps, with optional per-step `context_budget` override in workflow YAML. See Phase 1 budget derivation note in Section C. Complexity-based derivation should be deferred until production data shows which steps systematically need more context.

### 9. Compression pipeline latency and availability — Resolve before Phase 1

**Problem**: The abstractive compression step (pipeline step 3b) is an LLM call on the dispatch critical path. The RFC specifies fallback when the overhead budget is exhausted, but does not address:
1. **Timeout**: What if the compression LLM call takes 30+ seconds?
2. **Model unavailability**: Budget exhaustion and model unavailability are different failure modes.
3. **Sync vs async**: Current `WorkingMemory.compress_if_needed()` is async fire-and-forget; the orchestrator-level pipeline appears synchronous (blocks dispatch).

**Recommendation**: Define a compression timeout (e.g., 10 seconds). On timeout or model unavailability, fall back to extractive-only compression with a `compression_fallback` metric. Consider making abstractive compression async for non-critical steps.

### 10. Orchestrator vs agent context assembly boundary — Resolve before Phase 1

**Problem**: The `ContextPackage` pipeline (Section D) runs in the Go orchestrator, but agent memory (episodic, notes, working) lives in Python agent processes. The `budget_memory_tokens` field (Section C) implies the orchestrator allocates a token budget for memory, but the orchestrator cannot enforce it because memory retrieval happens inside the Python agent. The RFC describes both orchestrator-level and agent-level context assembly without clearly delineating the boundary.

Three options:
1. **Soft enforcement**: Orchestrator sends a budget hint; trusts the agent to comply.
2. **Hard enforcement**: New gRPC "memory query" call from orchestrator to agent retrieves pre-scored candidates. Requires proto changes.
3. **Split ownership**: Orchestrator budgets what it controls (step outputs); agent manages its own memory budget using `budget_memory_tokens` as a hint.

**Recommendation**: Option 3 for Phase 1 — the orchestrator budgets step outputs (which it already holds in the `outputs` map), and the agent manages its own memory budget using `budget_memory_tokens` as an advisory limit. This avoids proto changes and new gRPC calls. Phase 3's delegation contract can formalize enforcement if needed.

### 11. Merge strategy `patch` semantics — Resolve before Phase 3

**Problem**: Section E lists four merge strategies (`replace`, `append`, `patch`, `reject_on_conflict`). The semantics of `patch` are undefined for the memory write schema. For JSON-structured fields (`context`, `tags`), JSON Merge Patch (RFC 7396) is a reasonable interpretation. For string fields (`content`, `summary`), "patch" has no obvious meaning.

**Recommendation**: Define `patch` as JSON Merge Patch for structured fields and `replace` for string fields. If more granular string patching is needed later, introduce it as a separate strategy.

### 12. Memory eviction parameter calibration — Resolve before Phase 4

**Problem**: Section G now includes default parameter values (see table), but these are informed guesses. Production workloads may reveal that the defaults are too aggressive or too lenient for specific agent types or workflow patterns.

**Recommendation**: The defaults in Section G are starting points. Phase 4 implementation should include a calibration period where eviction metrics (`evictions_per_cleanup`, `average_confidence_at_eviction`, `memory_utilization_ratio`) are collected and analyzed before adjusting defaults. Agent-level overrides in `config/agents.yaml` allow per-agent tuning without a global parameter change.

### 13. Shared memory ACL without RFC 0009 — Resolve before Phase 4

**Problem**: Phase 4 shared memory depends on ACL enforcement. The RFC references RFC 0009 for capability tokens, but RFC 0009 is at `📋 Proposed` status and may not land before Phase 4 implementation.

**Impact**: If RFC 0009 slips, shared memory would either launch without security primitives (unacceptable per deny-by-default policy) or be blocked waiting for RFC 0009.

**Recommendation**: Define a minimal ACL mechanism in Phase 4 that works without RFC 0009:
- Config-based ACL: `config/agents.yaml` gets a `shared_memory_pools` section with explicit read/write agent lists.
- Enforcement in the Python memory layer (not dependent on Go orchestrator security).
- RFC 0009 can later upgrade this to token-based ACL without breaking changes.

This ensures deny-by-default is preserved regardless of RFC 0009 timeline.

## Decision / Next Steps

Decision: Propose this RFC for review as the v0.2 architecture bridge between RFC 0006 (limits/budgets) and broader delegation/sub-agent scale.

Next steps:

1. Accept sequencing: 0006 → 0008 → 0007 implementation order (RFC 0007 `Depends on` has been updated to reflect this).
2. Create a PR plan file for RFC 0008 after acceptance.
3. Implement Phase 1 first to establish measurable context-budget outcomes.
4. Resolve Pending Open Questions 8, 9, and 10 before Phase 1 implementation begins.

## Related Documentation

- [RFC 0005](0005-persona-agent-memory.md)
- [RFC 0006](0006-efficiency-execution-limits.md)
- [RFC 0007](0007-conditional-looped-workflow-control-flow.md)
- [Roadmap](../../ROADMAP.md)
- [Extension Spec](../persatrix-extension-spec.md)
