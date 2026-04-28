# RFC 0008 — PR Implementation Plan

**RFC**: [0008-agent-memory-context-optimization.md](0008-agent-memory-context-optimization.md)
**Created**: 2026-04-25
**Fleshed out**: 2026-04-27
**Branch prefix**: `feature/v030-rfc0008-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.0-plan.md Phase 1 (combined plans PR)](../v0.3.0-plan.md#phase-1--author-the-six-rfc-pr-plans)

> **Status**: ✅ Ready — every PR row has a fleshed-out scope table, key-implementation-detail block, tests block, and PR checklist. Cross-RFC sequencing pins (PR 1 → RFC 0007 PR 3; PR 2 → RFC 0011 PR 5 + RFC 0020 PR 4) are unchanged and the 6-PR count is preserved so downstream PR plans stay valid.

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
**Estimated size**: ~400–500 lines (calibrated; close to the [BRANCHING.md](../BRANCHING.md) 500-line soft cap — see *Sizing risk* below).

#### Scope

| File | Change |
|------|--------|
| `schemas/workflow.schema.json` | Add optional per-step `context_budget` (integer, ≥ 0) and optional workflow-level `context_budget_total` (integer, ≥ 0). Both default to unset; unset workflow-level total disables packaging (legacy passthrough preserved). |
| `internal/planner/` | Parse + validate the new fields onto `planner.Step` and the workflow root. Reject negative values at parse time. |
| `internal/scheduler/scheduler.go` | Equal-split allocator per [RFC §C](0008-agent-memory-context-optimization.md#c-context-budget-as-a-scheduler-primitive) and [Open Question 8](0008-agent-memory-context-optimization.md#8-context-budget-derivation-algorithm--equal-split-with-per-step-override): subtract per-step overrides from `context_budget_total`, divide remainder among non-overridden steps. Persist remaining budget in step state across retries (rule 4). |
| `internal/executor/dispatch.go` | New `ContextPackage` struct (Go) with `pinned_sections`, `step_outputs`, `metrics` fields. Serialize as JSON under reserved key `_context_package` in the existing `TaskRequest.context` map per [Open Question 2](0008-agent-memory-context-optimization.md#2-proto-context-fields--defer-typed-fields-to-phase-3-use-existing-context-map-in-phases-12). No proto changes. |
| `internal/executor/packaging/` | **New package**. Candidate-selection (depends-on outputs + workflow constants), `RelevanceScorer` interface with default heuristic backend (dependency proximity + lexical overlap + recency + importance — [Open Question 1](0008-agent-memory-context-optimization.md#1-relevance-scoring-approach--heuristic-only-in-phase-1-pluggable-scoring-interface)), greedy knapsack by `relevance / tokens` density per [RFC §D](0008-agent-memory-context-optimization.md#d-context-packaging-and-compression-pipeline), extractive truncation (deterministic order: lowest density first), pinned-section passthrough. **Phase 1 is extractive-only.** Abstractive compression is deferred to Phase 1b (see *Sizing risk*). |
| `internal/cost/` | Add `ContextPackageMetrics` (`tokens_before`, `tokens_after`, `compression_ratio`, `candidates_admitted`, `candidates_dropped`) to per-step cost record. Emit `high_compression_ratio` warning at ≥ 4.0 and `extreme_compression_capped` event at the 10:1 hard cap per [Open Question 3](0008-agent-memory-context-optimization.md#3-compression-warning-threshold--warn-at-41-hard-cap-at-101). |
| `internal/state/` | Persist `remaining_context_budget` and `ContextPackageMetrics` in step state. |
| `tests/unit/go/scheduler/` | Equal-split allocator: N steps with no overrides; mixed override + non-override; override sum ≥ total → reject at validate time; retry preserves remaining budget. |
| `tests/unit/go/executor/packaging/` | Greedy knapsack picks highest-density candidates; pinned sections always admitted (even if over budget); deterministic truncation order under tied densities; warning fires at 4:1; cap at 10:1. |
| `tests/integration/go/` | End-to-end: 3-step workflow with `context_budget_total: 6000` → step 2 receives a `_context_package` payload whose `step_outputs` honor its allocated budget; step metadata records compression ratio. |

#### Key implementation details

- **Equal-split formulas** (verbatim from [Open Question 8](0008-agent-memory-context-optimization.md#8-context-budget-derivation-algorithm--equal-split-with-per-step-override)): $B_{remaining} = B_{total} - \sum_{i \in \text{overridden}} B_{i,\text{override}}$ then $B_{step,\text{non-overridden}} = B_{remaining} / (N_{steps} - |\text{overridden}|)$. If `B_remaining < 0` the planner rejects the workflow with a precise error citing the offending overrides.
- **Retry budget persistence**: after each dispatch attempt the executor writes `remaining_context_budget = remaining_before - actual_consumed_input_tokens` back to step state. The next retry's packaging pass reads this value, not the original allocation. This is the contract RFC 0006's `BudgetEnforcer` already established for execution budgets — the context budget piggybacks on the same step-state row.
- **`_context_package` JSON shape** (pinned to enable downstream consumers to parse without proto changes):
  ```json
  {
    "version": 1,
    "pinned_sections": [{"name": "...", "content": "...", "tokens": 0}],
    "step_outputs": [{"step_id": "...", "content": "...", "tokens": 0, "relevance": 0.0, "compressed": false}],
    "metrics": {"tokens_before": 0, "tokens_after": 0, "compression_ratio": 0.0, "candidates_dropped": 0},
    "budget_memory_tokens": 0
  }
  ```
  `version: 1` is the wire contract for v0.3.0. The optional `budget_memory_tokens` field is admitted into v1 up-front (zero-cost — orchestrator emits 0 in PR 1, PR 2 gives it a non-zero meaning) so PR 2 does not need to evolve the shape mid-RFC. Agents that don't recognise a higher version downgrade gracefully (ignore unknown fields). Any *new* top-level field added after PR 1 merges requires a version bump and a separate RFC amendment. **`candidates_admitted` is intentionally cost-record-only** — the wire `metrics` block exposes only `candidates_dropped` because consumers can compute the admitted count from `step_outputs` length (admitted == `len(step_outputs)`). Keeping the wire shape minimal is preferred over duplicating derivable values.
- **`RelevanceScorer` protocol** (Go): `type RelevanceScorer interface { Score(candidate Candidate, query QueryContext) float64 }`. The default heuristic implementation lives in `executor/packaging/scorer_heuristic.go`. RFC 0008's [Open Question 1](0008-agent-memory-context-optimization.md#1-relevance-scoring-approach--heuristic-only-in-phase-1-pluggable-scoring-interface) commits this surface so a future embedding backend (RFC 0005 follow-on) can swap in.
- **Pinned-section contract**: orchestrator-side parallel of `ContextSection.compressible = False` from `agents/memory/working.py` (see [RFC §F](0008-agent-memory-context-optimization.md#f-persona-context-sanity-and-helper-agents)). Pinned sections are excluded from compression-ratio denominator (per [Open Question 3](0008-agent-memory-context-optimization.md#3-compression-warning-threshold--warn-at-41-hard-cap-at-101)) and always admitted; if their token sum alone exceeds `B_step`, packaging logs `pinned_overflow` and admits anyway (correctness over budget — operator alert via metric).
- **Compression overhead budget**: not implemented in Phase 1 (no abstractive calls yet). The struct + metrics are wired so Phase 1b can plug in the abstractive call without schema churn.
- **Sizing risk**: the calibrated upper bound (~500 lines) brushes the [BRANCHING.md](../BRANCHING.md) cap. If implementation exceeds 450 lines pre-tests, split off the cost-metrics + state-persistence rows (`internal/cost/` + `internal/state/`) into a follow-on PR `feature/v030-rfc0008-context-metrics` between PR 1 and PR 2. The packaging pipeline can ship with structured-log-only observability and metrics back-fill in the follow-on without changing the `_context_package` v1 shape. Update PR 2's `Depends on` to reference both. This split is contingent and does not change the canonical 6-PR count unless triggered. (The previously-named `feature/v030-rfc0008-context-abstractive` split is not viable because abstractive compression is already deferred to Phase 1b — there is no Phase-1 abstractive code to split out.)
- **Phase 1b prep note**: when the deferred abstractive-compression PR opens, it must implement [RFC §Security item #6](0008-agent-memory-context-optimization.md#security-considerations) (compression-LLM prompt-injection mitigation): a system instruction that directs the compression model to summarize untrusted input rather than execute it, plus `source: "compressed"` tagging on every emitted section so downstream consumers know to apply reduced trust.

#### Tests

Unit (Go):
- Allocator: equal-split with N ∈ {1, 3, 10}; no overrides; mixed overrides; all overrides; override sum equals total (zero remainder); override sum exceeds total (planner rejects).
- Allocator: retry consumes from persisted remaining budget, not original allocation.
- Packaging: greedy knapsack admits highest density first; ties broken by candidate ID (deterministic); pinned section always present.
- Packaging: extractive truncation drops lowest-density candidates first; resulting `tokens_after ≤ B_step` (or pinned-overflow path triggered).
- Metrics: `compression_ratio = tokens_before / tokens_after`; ratio is 1.0 when nothing dropped; ratio cap at 10:1 emits `extreme_compression_capped`; ratio ≥ 4.0 emits `high_compression_ratio`.
- Schema: workflow with negative `context_budget` rejected at parse time.
- `_context_package` JSON round-trip: serialize → deserialize → equal struct.

Integration (Go + Python):
- 3-step workflow, `context_budget_total: 6000`, no overrides → each step's `_context_package.metrics.tokens_after ≤ 2000`.
- Same workflow with `step[1].context_budget: 4000` → step 1 gets 4000, step 0 + step 2 split remaining 2000 → 1000 each.
- Step retry path: a step that fails after consuming 800 of 2000 budgeted input tokens has `remaining_context_budget = 1200` on its second attempt's package.
- Agent-side: a Python agent receiving `_context_package` parses it without errors and surfaces `version` in its trace metadata. Agents without packaging-awareness ignore the key (regression test).

#### PR checklist

- [x] `make test` passes (Go + Python + integration)
- [x] `make lint` clean
- [x] `make validate` passes (workflow schema additions)
- [x] `_context_package` JSON shape (`version`, `pinned_sections`, `step_outputs`, `metrics`, `budget_memory_tokens`) frozen — any new top-level field after merge requires a version bump and a separate RFC amendment
- [x] `RelevanceScorer` protocol exported with a default heuristic backend; embedding-backend extension point documented in package GoDoc
- [x] Pinned-section overflow path emits `pinned_overflow` metric and proceeds (correctness over budget)
- [x] ROADMAP.md row for RFC 0008 → `🚧 Implementing`
- [x] Master Progress Overview row 4 → 🔄 In progress
- [ ] [RFC 0007 PR plan](0007-pr-plan.md) PR 3 reviewer pinged: `repeat_until` loop budget integration is now unblocked
- [x] **Sizing-risk split triggered**: cost-metrics + state-persistence deferred to follow-on PR `feature/v030-rfc0008-context-metrics` (PR 1b). Phase 1 ships with structured-log-only observability via `Metrics.Warnings`; full metrics + persistence land in PR 1b without changing the `_context_package` v1 shape.

> **Sizing-risk follow-up (PR 1b)** — `feature/v030-rfc0008-context-metrics`: wires `cost.ContextPackageMetrics` to per-step cost records and adds `remaining_context_budget` persistence to `internal/state/` step rows. PR 2 `Depends on` references PR 1 + PR 1b. Bookkeeping under PR 1 row — canonical 6-PR count unchanged. **✅ Merged as PR #219 (2026-04-27).**

---

### PR 2: `feature/v030-rfc0008-memory-facade` — Phase 2: MemoryFacade for Task Agents

**Depends on**: PR 1 and PR 1b (`feature/v030-rfc0008-context-metrics` — cost-metrics + state-persistence follow-on, sizing-risk split triggered at PR 1 merge).
**Estimated size**: ~350–500 lines (calibrated; near the cap — see *Sizing risk*).

> **Sizing-risk split triggered (PR 2 → PR 2a)** — eviction (`agents/memory/eviction.py` + tests) deferred to `feature/v030-rfc0008-eviction`. PR 2 ships the facade surface so the cross-RFC pins ([RFC 0011 PR 5](0011-pr-plan.md), [RFC 0020 PR 4](0020-pr-plan.md)) unblock immediately. PR 2a is bookkeeping under PR 2 row — canonical 6-PR count unchanged.

#### Scope

| File | Change |
|------|--------|
| `agents/memory/facade.py` | **New** — `MemoryFacade` class per [RFC §B](0008-agent-memory-context-optimization.md#b-memory-for-all-agent-types). Methods: `retrieve_relevant(query, *, limit, scope=None, tags=None, min_score=None) -> list[MemoryEntry]`; `store_observation(entry, *, scope, ttl_seconds=None, tags=()) -> str`; `store_procedure(key, content, *, confidence, expires_at=None) -> None`; `list_candidates(task_context) -> list[Candidate]`; `compress(entries, *, target_tokens) -> CompressedView`. Per-process lifecycle with `initialize()` / `close()` matching the existing `MemoryLifecycle` protocol per [Open Question 7](0008-agent-memory-context-optimization.md#7-memoryfacade-lifecycle-for-task-agents--per-process-with-serialized-access). |
| `agents/memory/facade.py` | `tags` filter parameter on `retrieve_relevant` (required by [RFC 0011 PR plan](0011-pr-plan.md) PR 5). `compress` hook (required by [RFC 0020 PR plan](0020-pr-plan.md) PR 4). Both are pinned-API surfaces — additive evolution only. |
| `agents/task_agent.py` | Wire `MemoryFacade` into `_run_llm_loop`. Read advisory `budget_memory_tokens` from `_context_package` payload (per [Open Question 10](0008-agent-memory-context-optimization.md#10-orchestrator-vs-agent-context-assembly-boundary--split-ownership-option-3)) and translate into `retrieve_relevant(limit=...)` via `estimate_tokens()` from `agents/memory/working.py`. Memory injection is gated on `agent.memory.enabled` config flag (default `false` to preserve existing stateless behaviour). |
| `agents/server.py` | Extend `start()` / `stop()` to instantiate / close `MemoryFacade` for memory-enabled task agents (mirrors the existing persona-agent lifecycle path). |
| `config/agents.yaml` | New optional `memory` block per task agent: `enabled` (bool, default `false`), `episodic_cap` (int, default `1000`), `ttl_low_importance_days` (int, default `30`), `min_score` (float \| null, default `null`), `eviction_cadence_seconds` (int, default `3600`). |
| `schemas/agent.schema.json` | Schema for the new `memory` block; `make validate` enforces. |
| `agents/memory/eviction.py` | **New** — basic eviction policy: hard TTL for entries with `importance < 0.3` after `ttl_low_importance_days`; size-cap pruning at `episodic_cap` using the [RFC §G](0008-agent-memory-context-optimization.md#g-memory-eviction-decay-and-validation) hybrid score `importance × 0.6 + recency_norm × 0.3 + access_freq_norm × 0.1`. **Confidence decay is deferred to PR 5** (Phase 4b). |
| `agents/memory/eviction.py` | Single `EvictionPass.run()` entry point invoked on a periodic background task scheduled by `MemoryFacade.initialize()`. Default cadence: every 1 hour, configurable via `memory.eviction_cadence_seconds` in `config/agents.yaml` (default `3600`). |
| `tests/unit/python/test_memory_facade.py` | **New** — facade contract tests, lifecycle tests, advisory-budget translation tests. |
| `tests/unit/python/test_memory_eviction.py` | **New** — TTL eviction; size-cap eviction by hybrid score; deterministic ordering under tied scores. |
| `tests/integration/python/test_task_agent_memory.py` | **New** — task agent with `memory.enabled: true` stores an observation, retrieves it on a subsequent call, respects `min_score` filter. |

#### Key implementation details

- **Per-process lifecycle** ([Open Question 7](0008-agent-memory-context-optimization.md#7-memoryfacade-lifecycle-for-task-agents--per-process-with-serialized-access)): a single `EpisodicMemory` instance per task-agent process, shared across concurrent gRPC calls. Serialization relies on aiosqlite's WAL-mode single-connection internal queue. No new `asyncio.Lock` is introduced unless parallel tool execution lands later (it doesn't in v0.3.0). Per-task instantiation is rejected because `EpisodicMemory.initialize()` runs `PRAGMA journal_mode=WAL` + FTS5 + migration check on every call.
- **Advisory budget translation**: the agent reads `_context_package.metrics.tokens_after` and the top-level `budget_memory_tokens` field (admitted into the v1 shape in PR 1; PR 1 emits 0, PR 2 starts emitting non-zero values from the orchestrator-side budget allocator). Translation: `limit = max(1, int(budget_memory_tokens / avg_entry_tokens))` where `avg_entry_tokens = 100` is a Phase 2 constant (calibrated against existing episodic data in PR 5's metrics rollout). Enforcement is advisory — the agent is trusted; future PR 3 of this plan can audit usage in `DelegationResult`.
- **`MemoryFacade.compress(entries, *, target_tokens) -> CompressedView`** — the API hook required by [RFC 0020 PR plan](0020-pr-plan.md) PR 4's summarize-on-close path. Phase 2 implementation is extractive-only (highest-importance entries first up to `target_tokens`). The abstractive path delegates to `WorkingMemory.compress_if_needed()` from `agents/memory/working.py`; since that method takes no `target_tokens` argument and triggers off its own internal threshold, the facade adapter (a) sets the working-memory token ceiling to `target_tokens` before the call and (b) reads the post-compression sections back out — i.e. `target_tokens` is enforced facade-side, not pushed into `WorkingMemory`'s signature. `CompressedView` is a frozen dataclass with `summary: str`, `entries_dropped: int`, `tokens_before: int`, `tokens_after: int`.
- **`tags` filter semantics**: `retrieve_relevant(tags=("channel:slack-#dev",))` returns entries whose `tags` set is a superset of the requested tags (AND, not OR). [RFC 0011 PR plan](0011-pr-plan.md) PR 5's channel-scoped recall depends on this AND semantics.
- **Eviction scheduling**: `MemoryFacade.initialize()` starts `asyncio.create_task(_eviction_loop())`; `close()` cancels it. Eviction is best-effort — failures log a warning and the loop continues (mirrors RFC 0005 working-memory async-flush pattern).
- **Sizing risk**: facade + eviction + tests + integration is wide. If implementation pushes over the cap during PR review, split eviction (`agents/memory/eviction.py` + its tests) into a follow-on `feature/v030-rfc0008-eviction` PR. Cross-RFC pins ([RFC 0011 PR 5](0011-pr-plan.md), [RFC 0020 PR 4](0020-pr-plan.md)) only require the facade surface, not eviction, so the split is safe.

#### Tests

Unit (Python):
- `MemoryFacade.retrieve_relevant`: `limit` honored; `tags=()` is no-op; `test_facade_tags_intersection` — `tags=("a", "b")` returns only entries whose tag set is a superset of `{a, b}` (AND semantics, the contract [RFC 0011 PR plan](0011-pr-plan.md) PR 5 pins); `min_score` filters per `EpisodicMemory.recall` contract.
- `MemoryFacade.store_observation`: returns a stable key; persists across `close()` + new instance with same DB path.
- `MemoryFacade.compress`: reduces token count to `≤ target_tokens`; preserves highest-importance entries; idempotent on already-compressed view.
- Lifecycle: `initialize()` is safe to call twice (second call is no-op + warning); `close()` after `initialize()` cancels the eviction loop within 1s.
- Eviction TTL: entry with `importance=0.2`, `created_at` 31 days ago → evicted on next pass; same entry with `importance=0.5` → retained.
- Eviction size cap: 1500 entries with `episodic_cap=1000` → 500 lowest-scoring entries evicted; deterministic tie-break by `created_at ASC`.
- Eviction failure: simulated DB error in one pass logs a warning, the loop survives, the next pass succeeds.

Integration (Python):
- Task agent with `memory.enabled: true` calls `store_observation` in tool A, then `retrieve_relevant` in tool B → returns the entry.
- Same agent with `memory.enabled: false` (default) → `MemoryFacade` is `None`; tool calls that would write memory raise `MemoryDisabledError` (no silent no-op).
- Concurrent gRPC calls (10 parallel) on a memory-enabled task agent → no FTS5 corruption; all stores readable post-flush.
- `_context_package.budget_memory_tokens=500` → `retrieve_relevant` is called with `limit=5` (using `avg_entry_tokens=100`).

#### PR checklist

- [x] `make test` passes
- [x] `make lint` clean
- [x] `make validate` passes (`schemas/agent.schema.json` additions)
- [x] `MemoryFacade.retrieve_relevant` exposes the `tags` filter required by [RFC 0011 PR plan](0011-pr-plan.md) PR 5 (AND semantics confirmed in test `test_facade_tags_intersection`)
- [x] `MemoryFacade.compress` exposes the `(entries, target_tokens) -> CompressedView` hook required by [RFC 0020 PR plan](0020-pr-plan.md) PR 4
- [x] Per-process lifecycle: a single `EpisodicMemory` instance per agent process; no per-task instantiation
- [x] `memory.enabled: false` is the default in the schema (deny-by-default; preserves existing stateless task-agent behaviour)
- [ ] [RFC 0011 PR plan](0011-pr-plan.md) PR 5 reviewer pinged: `MemoryFacade` is now available
- [ ] [RFC 0020 PR plan](0020-pr-plan.md) PR 4 reviewer pinged: `compress` hook is now available

#### Follow-up findings (from PR #220 deep review)

All items resolved in PR #221.

| ID | Sev | Finding (summary) | Status |
|----|-----|-------------------|--------|
| M1 | Med | `retrieve_relevant` scope filter missed column-level `scope`; non-facade writers invisible. | ✅ PR #221 |
| M2 | Med | `memory.min_score` defaulted to `null` — low-score entries reached system prompt (OWASP LLM01). | ✅ PR #221 |
| L1 | Low | `MemoryFacade.episodic` raised bare `RuntimeError` instead of `MemoryDisabledError`. | ✅ PR #221 |
| L2 | Low | Tests pinned `RuntimeError` not `MemoryDisabledError`. | ✅ PR #221 |
| L3 | Low | `agents/server.py` `stop()` duplicated `close_memory()` call. | ✅ PR #221 |
| L4 | Low | `MemoryFacade.compress()` silent skip undocumented. | ✅ PR #221 |
| L5 | Low | `store_observation` docstring missing `outcome` param. | ✅ PR #221 |
| Info-1 | Info | Glossary missing `MemoryFacade`/`MemoryEntry`/`CompressedView`/`Candidate`/`MemoryDisabledError`. | ✅ PR #221 |
| Info-2 | Info | ROADMAP flip needed on PR #220 merge. | ✅ Done |

> **PR 2a scope expansion**: PR 2a (`feature/v030-rfc0008-eviction`) absorbs M1/M2/L1–L5 above so PR 2 stays facade-only. Bookkeeping under PR 2 row — canonical 6-PR count unchanged.

#### Follow-up findings (from PR #221 deep review)

All blockers resolved in PR #221; deferrals routed below.

| ID | Sev | Finding | Target |
|----|-----|---------|--------|
| M1 | Med | `cadence > 0` / `cap >= 1` / `ttl >= 1` validated in async task — silent loss on first tick. Move to `MemoryFacade.__init__`. | ✅ PR #221 |
| M2 | Med | First eviction deferred a full cadence — over-cap agents stay bloated. Run startup pass after `min(60, cadence/10)` s. | ✅ PR #221 |
| M3 | Med | `MemoryFacade.initialize` reaches into `EpisodicMemory._ensure_db()`. Promote a `connection` property. | PR 5 (with L7) |
| L1 | Low | `__init__` eviction params lack validation; subsumed by M1. | ✅ PR #221 |
| L2 | Low | `test_eviction_loop_survives_pass_failure` never injects a failure. Patch `EvictionPass.run` to raise then succeed. | ✅ PR #221 |
| L3 | Low | `_score_rows` returns `list[dict]` — opaque to mypy. Promote to `TypedDict`. | PR 5 |
| L4–L6 | Low | Narrow `except`; drop dead `base` local; strengthen SQLi comment in `_evict_size_cap`. | PR 2a — nice to have |
| L7 | Low | Missing e2e (gRPC + eviction), concurrent-write, FTS5-sync, `initialize_memory` wiring tests. | PR 5 |

> **Routing**: M1/M2/L2 in PR #221. M3/L3/L7 deferred to PR 5 (Phase 4b) so the SLF001-leak refactor lands with wiring tests, avoiding a re-triggered sizing-risk split. Canonical 6-PR count unchanged.

> **✅ Merged as PR #221 (2026-04-28)** — episodic eviction, M1/M2 fixes, procedure-row exclusion, all PR #220 follow-ups resolved.

---

### PR 3: `feature/v030-rfc0008-delegation-merge` — Phase 3: Delegation Contract + Merge Engine

**Depends on**: PR 1 (uses the `_context_package` JSON shape) and PR 2 (`MemoryFacade.store_observation` is the merge sink for `memory_writes`).
**Estimated size**: ~400–500 lines (calibrated; near the cap — see *Sizing risk*).

#### Scope

| File | Change |
|------|--------|
| `agents/sub_agents/delegation.py` | **New** — `DelegationRequest` and `DelegationResult` dataclasses per [RFC §E](0008-agent-memory-context-optimization.md#e-delegation-contract-and-merge-semantics). `MemoryWriteEntry` schema with `tier`, `key`, `content`, `importance`, `ttl_seconds`, `tags`. `source_agent` is framework-injected (rejected if caller-set). |
| `agents/sub_agents/merge.py` | **New** — `MergeEngine` with strategies `replace`, `append`, `patch`, `reject_on_conflict` per [RFC §E](0008-agent-memory-context-optimization.md#e-delegation-contract-and-merge-semantics). `patch` uses JSON Merge Patch (RFC 7396) for structured fields and acts as `replace` for strings per [Open Question 11](0008-agent-memory-context-optimization.md#11-merge-strategy-patch-semantics--json-merge-patch-for-structured-fields-replace-for-strings). `tags` lists merge as union under `patch`. |
| `agents/sub_agents/merge.py` | Schema validation (mandatory before merge); reject entries missing required fields, with `tier` outside `{episodic, notes}`, or with caller-set `source_agent`. Importance downscaled to caller-configured trust ceiling (default `0.8` for unverified sub-agents). `max_memory_writes` cap at 20 per result (security item #7). |
| `agents/sub_agents/spawner.py` | Replace existing TODO stub with a contract-aware spawner that builds `DelegationRequest` (objective + context package + sub-budget + allowed tools + output schema) and invokes the sub-agent through the existing dispatch path. |
| `agents/task_agent.py` | When acting as a sub-agent, validate output conforms to `DelegationResult` schema before returning. |
| `internal/observability/` (Go) | New metrics: `delegation_merge_outcome{strategy, status}`, `delegation_dropped_fields_count`, `delegation_memory_writes_admitted`, `delegation_memory_writes_rejected{reason}`. Reasons: `schema_invalid`, `trust_ceiling`, `cap_exceeded`, `source_agent_set`. |
| `tests/unit/python/test_delegation_contract.py` | **New** — request/result schema validation, framework-injected `source_agent`, importance downscaling. |
| `tests/unit/python/test_merge_engine.py` | **New** — all four strategies + JSON Merge Patch corner cases. |
| `tests/integration/python/test_delegation_end_to_end.py` | **New** — caller dispatches `DelegationRequest`, sub-agent returns `DelegationResult`, merge applies, `memory_writes` land in caller memory under the trust ceiling. |

#### Key implementation details

- **`DelegationRequest` shape** (frozen dataclass):
  ```python
  @dataclass(frozen=True)
  class DelegationRequest:
      objective: str
      acceptance_criteria: list[str]
      context_package: dict          # the same v1 shape PR 1 froze
      budget: BudgetEnvelope         # tokens, timeout, max_llm_calls
      allowed_tools: frozenset[str]
      output_schema: dict            # JSON Schema fragment
      trust_ceiling: float = 0.8     # default per [RFC §E](#e-delegation-contract-and-merge-semantics)
      max_memory_writes: int = 20    # default per security item #7
  ```
- **`DelegationResult` shape** (matches [RFC §E](0008-agent-memory-context-optimization.md#e-delegation-contract-and-merge-semantics) verbatim): `summary`, `artifacts`, `decisions`, `memory_writes`, `risks`, `status`. `status ∈ {"completed", "partial", "failed"}`.
- **Merge order** (deterministic): (1) schema validation; (2) framework-inject `source_agent`; (3) cap `memory_writes` at `max_memory_writes` (extras → `cap_exceeded` reason); (4) downscale `importance` to `trust_ceiling`; (5) apply per-entry merge strategy against existing memory; (6) emit metrics. Failure at step (1) rejects the whole result with `status=failed` recorded in metrics; later steps reject only the offending entries and continue.
- **JSON Merge Patch on `artifacts`**: `null` values delete keys, present values overwrite. Implementation uses Python's `json` module; no new dependency. The Go side does not need to implement Merge Patch in this PR — merge happens in the Python caller process.
- **`tags` list under `patch`**: union semantics (additive). Removing a tag requires `replace` strategy on the whole entry.
- **`source_agent` injection**: the spawner records the originating agent ID; the merge engine rejects any `MemoryWriteEntry` whose `source_agent` field is non-`None` on receipt (it must be `None` from the wire and is set by the framework). Test `test_source_agent_spoof_rejected` covers this.
- **Procedural-tier exclusion is intentional**: `MemoryWriteEntry.tier` is restricted to `{episodic, notes}`; sub-agent delegation cannot write the procedural tier even though PR 2 introduces `MemoryFacade.store_procedure` and PR 5 introduces `recall_procedures`. Procedural memory carries the `confidence` decay contract (PR 5) and the trust ceiling for unverified sub-agents (default `0.8`) is below the procedural `c_min` operating range — admitting sub-agent procedures would either bypass decay (bad) or auto-evict on the next pass (pointless). Procedure storage stays task-agent-only via the direct `MemoryFacade.store_procedure` path. Schema validation surface `procedural_tier_rejected` is logged when an entry attempts it (separate from `schema_invalid` so operators can spot trust-model probing).
- **Sizing risk**: contract + merge engine + spawner + observability is wide. If the implementation pushes over the cap, split observability metrics (`internal/observability/`) into a follow-on `feature/v030-rfc0008-delegation-metrics` PR; the merge engine can ship with structured-log-only observability and metrics back-fill in the follow-on without changing any agent-facing API.

#### Tests

Unit (Python):
- `DelegationRequest`: required fields enforced; default `trust_ceiling=0.8`; default `max_memory_writes=20`; budgets non-negative.
- `DelegationResult`: `status` validates against the closed set; `memory_writes` schema validates per entry.
- `MergeEngine.replace`: existing entry overwritten.
- `MergeEngine.append`: list-typed artifacts concatenated; non-list artifact under `append` → schema_invalid.
- `MergeEngine.patch`: JSON Merge Patch applied to `artifacts.code_review` (object); `null` deletes nested key; `tags` list unioned; string `content` field replaced.
- `MergeEngine.reject_on_conflict`: existing key + incoming entry → entry rejected with reason logged.
- Trust ceiling: incoming `importance=0.95` with `trust_ceiling=0.8` → stored at `0.8`; metric `trust_ceiling` increments.
- Cap: 25 incoming `memory_writes` with `max_memory_writes=20` → first 20 admitted (deterministic order: input order), 5 rejected with `cap_exceeded`.
- Spoofing: `MemoryWriteEntry(source_agent="impostor")` → rejected with `source_agent_set`.

Integration:
- Caller dispatches sub-agent with `DelegationRequest`; sub-agent returns `DelegationResult` with 3 memory writes; caller's `MemoryFacade.retrieve_relevant` finds them post-merge.
- Sub-agent returns malformed JSON → caller logs `schema_invalid` metric and surfaces a `DelegationFailure` to the workflow step (no partial merge).

#### PR checklist

- [x] `make test` passes
- [x] `make lint` clean
- [x] `make validate` passes (no schema additions in this PR; validates the existing `_context_package` shape from PR 1 round-trips)
- [x] `DelegationRequest` / `DelegationResult` dataclasses are frozen and validated against [RFC §E](0008-agent-memory-context-optimization.md#e-delegation-contract-and-merge-semantics) verbatim
- [x] `MergeEngine` rejects entries with `tier` outside `{episodic, notes}` and emits `procedural_tier_rejected` metric (procedural exclusion is intentional — see Key implementation details)
- [x] `source_agent` is framework-injected; caller-set values are rejected with `source_agent_set` reason
- [x] Importance downscaling to `trust_ceiling` (default `0.8`) is enforced on every admitted `MemoryWriteEntry`
- [x] `max_memory_writes` cap (default `20`, security item #7) is enforced
- [ ] [RFC 0008 PR 4](#pr-4-feature-v030-rfc0008-shared-pools-acl---phase-4a-shared-pool-acl--provenance) reviewer pinged: `MemoryWriteEntry` schema is now stable; shared-pool ACL can rely on the same provenance shape

#### Follow-up findings (from PR #222 deep review)

Pass-2 review: S2–S5 + N1–N4 resolved in-PR. Remaining items below.

| ID | Sev | Finding (summary) | Target |
|----|-----|-------------------|--------|
| S1 | Should | `output_schema` not enforced against `DelegationResult.artifacts` (explicit TODO). OWASP A04. | PR 3a (gate before PR 4) |
| S6 | Should | `DelegationResult.from_metadata_value` missing `.validate()` — asymmetric with S4 fix on `from_context_value`; bypassed by replay/audit paths. | PR 3a (gate before PR 4) |
| N5 | Nice | `FacadeBoundSpawner._persist_admitted` lacks rollback + test on partial-batch `store_observation` failure. | PR 3a — nice to have |
| N6 | Nice | `TaskAgent._parse_or_synthesise` brittle `{...}` heuristic → wasted `json.loads` + noisy logs. | PR 3a — nice to have |
| N7 | Nice | `output_schema` double-serialised at dispatch. Perf nit. | PR 3a — nice to have |
| N8 | Nice | Test path drift: `tests/integration/python/...` vs `tests/integration/...` (same as PR 2). | PR 6 (normalise plan) |
| Info-1 | Info | PR 3 checklist: 8 items still `[ ]` — flip on merge. | ✅ Done |
| Info-2 | Info | ROADMAP PR-count → "3 of 6"; row stays `🚧`. | ✅ Done |
| Info-3 | Info | No "PR 3a" bookkeeping row (parity with PR 1b/PR 2a). | See PR 3a row below |

> **Sizing-risk follow-up (PR 3a)** — `feature/v030-rfc0008-delegation-metrics`: back-fills Go-side delegation counters from PR 3. Absorbs S1/S6 + N5–N7. PR 4 `Depends on` adds PR 3a. 6-PR count unchanged.

> **✅ Merged as PR #222 (2026-04-28).**

---

### PR 4: `feature/v030-rfc0008-shared-pools-acl` — Phase 4a: Shared Pool ACL + Provenance

**Depends on**: PR 2 + PR 3 + PR 3a.
**Estimated size**: ~300–450 lines (calibrated).

#### Scope

| File | Change |
|------|--------|
| `agents/memory/shared_pool.py` | **New** — `SharedMemoryPool` class wrapping `EpisodicMemory` with config-based ACL enforcement per [Open Question 13](0008-agent-memory-context-optimization.md#13-shared-memory-acl-without-rfc-0009--config-based-acl-with-python-layer-enforcement). Methods: `read(agent_id, query, ...)`, `write(agent_id, entry)`. Deny-by-default — agents not in the pool's `readers`/`writers` list raise `SharedMemoryPermissionError`. |
| `agents/memory/shared_pool.py` | Provenance enforcement: every write requires `source_agent` (framework-injected from `agent_id`), `created_at` (set by pool), `confidence` (caller-supplied, validated `0.0 ≤ c ≤ 1.0`). Reads support `min_confidence` filter. |
| `agents/memory/facade.py` | Add `MemoryFacade.publish_to_pool(pool_name, entry)` — the curated publish path from isolated memory to shared pool ([RFC §H](0008-agent-memory-context-optimization.md#h-shared-vs-isolated-memory) hybrid model). Validates the agent has writer permission on the named pool. |
| `agents/memory/facade.py` | Add `MemoryFacade.read_from_pool(pool_name, query, *, min_confidence=None, limit, tags=None)`. |
| `config/agents.yaml` | New `shared_memory_pools` top-level section: named pools with `readers`, `writers`, `max_entries`, `required_confidence`, `sensitive` (bool — when `true`, isolated memory classes can never publish to this pool, enforcing [RFC §H](0008-agent-memory-context-optimization.md#h-shared-vs-isolated-memory) safety constraint #3). |
| `schemas/agent.schema.json` | Schema for `shared_memory_pools` including ACL list validation (no duplicates, agent IDs match the canonical pattern). |
| `internal/observability/` (Go) | Metrics `shared_pool_reads{pool,agent}`, `shared_pool_writes{pool,agent}`, `shared_pool_denied{pool,agent,operation}`. |
| `tests/unit/python/test_shared_memory_pool.py` | **New** — ACL enforcement, provenance fields, `min_confidence` filter, sensitive-pool isolation. |
| `tests/integration/python/test_shared_pool_publish.py` | **New** — agent A publishes to `team-knowledge`; agent B (reader) retrieves; agent C (not in ACL) is denied. |

#### Key implementation details

- **Config example** (Phase 4 default shape, copied verbatim from [Open Question 13](0008-agent-memory-context-optimization.md#13-shared-memory-acl-without-rfc-0009--config-based-acl-with-python-layer-enforcement)):
  ```yaml
  shared_memory_pools:
    team-knowledge:
      readers: ["code-writer", "code-reviewer", "planner"]
      writers: ["code-reviewer", "planner"]
      max_entries: 2000
      required_confidence: 0.5
      sensitive: false
  ```
- **Enforcement point**: ACL check happens in the Python `SharedMemoryPool` layer, not in the orchestrator. This matches the existing `agents/tools/permissions.py` pattern (Python-layer, deny-by-default). The orchestrator does not need to know about pools.
- **Provenance injection**: `source_agent` is set from the `agent_id` parameter passed by `MemoryFacade.publish_to_pool` — the caller cannot spoof it (any `entry.source_agent` value on input is rejected with `provenance_set` log).
- **Sensitive-pool isolation**: when `sensitive: true`, `MemoryFacade.publish_to_pool` rejects the call regardless of writer ACL, with reason `sensitive_pool_isolation`. This implements [RFC §H](0008-agent-memory-context-optimization.md#h-shared-vs-isolated-memory) safety constraint #3 ("Sensitive memory classes stay isolated regardless of pool settings").
- **`min_confidence` filter** (consumer-side trust): default `None` admits all entries; explicit `0.0` is identical (semantically explicit); `0.7` filters to high-confidence entries only.
- **Upgrade path to RFC 0009**: documented in code comments — when capability tokens land, the ACL check extends to verify a token in addition to (not instead of) the config list. The `SharedMemoryPool` interface stays stable.
- **Shared-pool eviction is FIFO, not §G hybrid score**: shared pools use `created_at` ascending eviction rather than the [RFC §G](0008-agent-memory-context-optimization.md#g-memory-eviction-decay-and-validation) hybrid formula `importance × 0.6 + recency_norm × 0.3 + access_freq_norm × 0.1` because shared-pool entries lack the per-agent `access_count` series required to compute `access_freq_norm` (each pool entry is read by N agents whose individual access counts the pool does not retain — RFC 0009 capability tokens would be required to attribute reads). FIFO is the simplest deterministic policy that preserves provenance ordering. PR 5's procedural-decay work does not apply to shared pools either: pool entries carry caller-supplied `confidence` but no `last_validated_at`, so decay would have no anchor.

#### Tests

Unit:
- Reader in ACL → `read` succeeds.
- Reader not in ACL → `read` raises `SharedMemoryPermissionError`; `shared_pool_denied{operation=read}` increments.
- Writer in ACL with valid entry → `write` succeeds; `source_agent` matches the calling agent.
- Writer not in ACL → `write` raises; metric increments.
- Caller-set `source_agent` on input → rejected with `provenance_set`.
- Missing `confidence` on write → schema validation rejects.
- `confidence > 1.0` or `< 0.0` → rejected.
- `min_confidence=0.7` filters out entries with `confidence=0.5`.
- Sensitive pool: writer in ACL but `sensitive: true` → `publish_to_pool` rejected with `sensitive_pool_isolation`.
- `max_entries=10`: 11th write triggers same-pool FIFO eviction (`created_at` ascending) before insert; metric `shared_pool_evictions` records.

Integration:
- Three agents (writer A, reader B, denied C); A writes 3 entries; B retrieves all 3; C is denied on both read and write.
- A publishes from isolated memory via `publish_to_pool`; the original isolated entry remains; the shared copy carries `source_agent=A` and the framework `created_at`.

#### PR checklist

- [ ] `make test` passes
- [ ] `make lint` clean
- [ ] `make validate` passes (`schemas/agent.schema.json` `shared_memory_pools` additions)
- [ ] Deny-by-default: agents not in a pool's `readers` / `writers` raise `SharedMemoryPermissionError` (no silent fallthrough)
- [ ] Provenance: `source_agent` is framework-injected from the calling `agent_id`; caller-set values rejected with `provenance_set`
- [ ] Sensitive-pool isolation ([RFC §H](0008-agent-memory-context-optimization.md#h-shared-vs-isolated-memory) safety constraint #3): `publish_to_pool` rejects writes to `sensitive: true` pools regardless of writer ACL, with reason `sensitive_pool_isolation`
- [ ] `min_confidence` filter on `read_from_pool` works without a default (explicit operator opt-in)
- [ ] RFC 0009 upgrade path documented in code comments (capability tokens augment, not replace, the config ACL)
- [ ] [RFC 0008 PR 5](#pr-5-feature-v030-rfc0008-procedural-revalidation---phase-4b-confidence-decay--revalidation) reviewer pinged: shared pools land before procedural decay so PR 5's stale-entry handling can rely on the provenance shape

---

### PR 5: `feature/v030-rfc0008-procedural-revalidation` — Phase 4b: Confidence Decay + Revalidation

**Depends on**: PR 4.
**Estimated size**: ~250–400 lines (calibrated).

#### Scope

| File | Change |
|------|--------|
| `agents/memory/decay.py` | **New** — `compute_decayed_confidence(c0, age_seconds, lambda_per_day)` implementing $c_t = c_0 \cdot e^{-\lambda t}$ per [RFC §G](0008-agent-memory-context-optimization.md#g-memory-eviction-decay-and-validation). Default `lambda = 0.01/day` (half-life ≈ 69 days). |
| `agents/memory/episodic.py` | Add `confidence` column to procedural-memory records (notes tier already has `importance`; add `confidence REAL NOT NULL DEFAULT 1.0` via a non-destructive migration). Schema migration version bump documented in the RFC 0005 migration log. |
| `agents/memory/episodic.py` | `recall_procedures(query, *, c_min=0.1)` — applies decay at read time using `created_at`/`last_validated_at`; filters out entries below `c_min`. |
| `agents/memory/episodic.py` | `refresh_confidence(key)` — sets `confidence = 1.0` and `last_validated_at = now()` on successful procedural reuse. Called by `MemoryFacade.store_procedure` when an existing key is re-stored. |
| `agents/memory/eviction.py` | Extend the eviction pass to evict procedural entries whose decayed confidence falls below `c_min` (default `0.1`). |
| `agents/memory/facade.py` | `MemoryFacade.retrieve_relevant` for procedural-tier queries emits a structured log `stale_memory_injection` (carrying `decayed_confidence`, `key`, `agent_id`) when an admitted entry's decayed confidence is between `c_min` and `stale_confidence_alert_threshold` (default `0.3` per [Open Question 5](0008-agent-memory-context-optimization.md#5-stale-procedural-memory--downgrade-confidence-and-continue-do-not-block)). The orchestrator-side observability layer (see `internal/observability/` row below) registers and counts the metric on log receipt. Execution is **not** blocked. |
| `config/agents.yaml` | New `procedural_memory` block: `lambda_per_day` (default `0.01`), `c_min` (default `0.1`), `stale_confidence_alert_threshold` (default `0.3`). |
| `schemas/agent.schema.json` | Schema additions; `make validate` enforces. |
| `internal/observability/` (Go) | New metrics required by [Open Question 12](0008-agent-memory-context-optimization.md#12-memory-eviction-parameter-calibration--ship-defaults-with-mandatory-metrics-collection): `evictions_count`, `average_confidence_at_eviction`, `average_importance_at_eviction`, `memory_utilization_ratio`, `oldest_surviving_entry_age_days`, `entries_below_stale_threshold`, `stale_memory_injection`. The `stale_memory_injection` counter is registered exactly once on the orchestrator side (incremented from agent log ingestion); agents emit the structured log only — they do not register the metric, to avoid duplicate emission across the gRPC boundary. |
| `docs/rfcs/0008-calibration-review.md` | **New** — placeholder file scheduling the 30-day post-merge review of eviction parameters per [Open Question 12](0008-agent-memory-context-optimization.md#12-memory-eviction-parameter-calibration--ship-defaults-with-mandatory-metrics-collection). PR 6 (close) replaces the placeholder with the actual review summary before flipping the RFC to `✅ Implemented`. |
| `tests/unit/python/test_memory_decay.py` | **New** — decay math, refresh, eviction integration. |

#### Key implementation details

- **Decay computation** is a pure function — no DB access. The episodic-memory query path computes decayed confidence at read time using the entry's `last_validated_at` (or `created_at` if never validated) and the configured `lambda_per_day`. Storing decayed confidence at rest would require a periodic rewrite pass — the read-time approach is simpler and the formula is cheap.
- **Migration safety**: the new `confidence` column has `DEFAULT 1.0` so existing notes/procedures upgrade cleanly without backfill. A migration test confirms a v0.2.x DB opens cleanly under v0.3.0.
- **Refresh contract**: `MemoryFacade.store_procedure(key, ...)` with an existing `key` does not blindly overwrite — it calls `refresh_confidence(key)` and updates `content` only if provided. This implements the RFC's "Confidence refresh on successful reuse".
- **Stale alert threshold**: when an admitted entry's decayed confidence is in `[c_min, stale_confidence_alert_threshold)`, the facade logs `stale_memory_injection` with the decayed value, key, and agent_id. Operators can set alerting rules on this metric per [Open Question 5](0008-agent-memory-context-optimization.md#5-stale-procedural-memory--downgrade-confidence-and-continue-do-not-block).
- **30-day calibration commitment**: per [Open Question 12](0008-agent-memory-context-optimization.md#12-memory-eviction-parameter-calibration--ship-defaults-with-mandatory-metrics-collection), this PR includes a `docs/rfcs/0008-calibration-review.md` placeholder file scheduling the 30-day post-merge review of eviction parameters. PR 6 (close) updates the placeholder with actual review findings before flipping the RFC to `✅ Implemented`.

#### Tests

Unit:
- `compute_decayed_confidence(1.0, 0, 0.01) == 1.0`.
- After 69 days at `lambda=0.01/day`: `≈ 0.5` (within 1e-3).
- After 230 days: `≈ 0.1` (boundary on default `c_min`).
- `recall_procedures` filters out entries with decayed confidence `< c_min`.
- `refresh_confidence(key)`: subsequent decay computation uses the new `last_validated_at`; confidence resets to `1.0`.
- Eviction pass evicts procedural entries below `c_min`.
- `stale_memory_injection` fires for admitted entries with decayed confidence in `[0.1, 0.3)`; does not fire for entries `≥ 0.3` or those filtered below `0.1`.
- Migration: a fixture v0.2.x DB without the `confidence` column opens; missing column populated with `1.0` defaults.

Integration:
- Agent stores a procedure today; mock-clock-advance 100 days; `retrieve_relevant` returns it at decayed confidence ≈ 0.37; `stale_memory_injection` metric fires.
- Agent re-stores the same procedure key after 100 days; subsequent retrieval returns confidence ≈ 1.0 with no stale warning.

#### PR checklist

- [ ] `make test` passes
- [ ] `make lint` clean
- [ ] `make validate` passes (`schemas/agent.schema.json` `procedural_memory` additions)
- [ ] `confidence` column migration is non-destructive (`DEFAULT 1.0`); a fixture v0.2.x DB opens cleanly under v0.3.0
- [ ] Decay is computed at read time using `last_validated_at` (or `created_at` if never validated); no periodic rewrite pass
- [ ] `MemoryFacade.store_procedure` on an existing key calls `refresh_confidence(key)` (does not blindly overwrite)
- [ ] `stale_memory_injection` is registered exactly once, orchestrator-side (incremented from agent structured-log ingestion); agents emit the log but do not register the counter, to avoid duplicate emission across the gRPC boundary
- [ ] `docs/rfcs/0008-calibration-review.md` placeholder file landed; PR 6 will replace it with the 30-day review summary
- [ ] 30-day calibration review (PR 6) must validate or retune `avg_entry_tokens = 100` (the PR 2 advisory-budget translation constant) against the observed `episodic_entry_token_count` distribution; record outcome in the calibration review summary
- [ ] PR 6 reviewer pinged: 30-day calibration timer starts on this PR's merge

---

### PR 6: `feature/v030-rfc0008-close` — Review Follow-Ups + RFC Close

**Depends on**: PR 5.
**Estimated size**: ~150–300 lines.

#### Scope

| File | Change |
|------|--------|
| `docs/rfcs/0008-agent-memory-context-optimization.md` | Status → `✅ Implemented`. |
| `ROADMAP.md` | RFC 0008 row → `✅ Implemented`; merged-PR rows for PRs 1–5 added to history. |
| `docs/v0.3.0-plan.md` | Master Progress Overview row 4 → ✅. |
| `docs/rfcs/0008-calibration-review.md` | Replace the PR 5 placeholder with the 30-day eviction-parameter review summary required by [Open Question 12](0008-agent-memory-context-optimization.md#12-memory-eviction-parameter-calibration--ship-defaults-with-mandatory-metrics-collection). Cite the actual `evictions_count`, `average_confidence_at_eviction`, `memory_utilization_ratio` ranges observed; record any default retunes (one-line config changes) or confirm the shipped defaults stood up. |
| `docs/rfcs/0008-pr-plan.md` | Final review-follow-up table aggregating low/medium findings from PR 1–5 deep reviews under a `## From PR Reviews` subsection. (Note: this plan combines the followups + close steps that other RFC PR plans sometimes split into two terminal PRs — e.g. [RFC 0020 PR plan](0020-pr-plan.md) splits them across PR 6 (followups) + PR 7 (close) — into a single PR 6 to preserve the 6-PR count this plan's downstream consumers pin.) |

##### From PR 1 review

Carry-over findings from the PR 1 (`feature/v030-rfc0008-context-budget`, merged as #218) deep review that were not blocking and were deferred here. Each is self-contained so the merged history needs no external report.

- M6 — Sort candidate IDs in `attachContextPackage` (`internal/scheduler/context_package.go`) before constructing the candidate slice. Today it ranges over a `map[string]string` so iteration order is randomised per process; that defeats the `Packager.Build` "emit admitted in original input order" determinism contract once a packaging-aware agent (PR 2) starts comparing packages across retries. Fix: `keys := slices.Sorted(maps.Keys(outputsCopy))` then range `keys`. Add a determinism guard test with ≥ 5 outputs of equal density asserting `pkg.StepOutputs` ordering is stable across two `attachContextPackage` calls (covers L8 too).
- M7 — Decide planner-tighten vs. scheduler-soften for the `Σ overrides == total` zero-budget case. Today the allocator returns `0` for every non-overridden step when overrides exhaust the total, and `stage_runner.executeStep` skips packaging because of its `budget > 0` gate, so a forgotten step gets legacy passthrough even though the workflow opted into packaging. Pick one: (a) reject the workflow at parse time when `Σ overrides + nonOverriddenCount > total` (require ≥ 1 token per non-overridden step); or (b) drop the `budget > 0` gate and always attach a package when `contextBudgets != nil`, emitting a `zero_budget` warning. Add a planner test (`TestParse_AllOverridesEqualTotal_NonOverriddenStepRejected`) or an allocator test asserting the chosen semantics.
- M8 — Document v1 advisory-only budget semantics. The dispatch carries both raw upstream outputs (under `out1`, `out2`, …) AND the same content embedded inside `_context_package.step_outputs[].content`, so a packaging-unaware agent reads raw outputs and bypasses the budget entirely. This is acceptable for PR 1 — PR 2's `MemoryFacade` is the natural enforcement point — but the contract must be explicit. Add a sentence to `Package` GoDoc and to RFC 0008 §D documenting that v1 packaging is *advisory ordering*; actual budget enforcement requires the agent to consume `step_outputs` in lieu of raw outputs, deferred to PR 2. Optionally add a packaging-on test asserting the raw keys remain in `Context` so the contract is locked in code.
- M9 — Decouple the `RelevanceScorer` interface from the heuristic backend's dep boost. `importanceForCandidate` returns `0.9` for IDs in `step.DependsOn` and `0.5` otherwise; `HeuristicScorer.Score` then adds both `importanceWeight * importance` AND `depWeight * 1{ID ∈ DependsOn}`, double-counting the dependency signal. A future embedding scorer would receive `Importance = 0.9` calibrated for the heuristic's dep boost. Pick: (a) make the scheduler emit a uniform `Importance` (e.g. always 0.5) and let the scorer own dep-proximity entirely (preferred — cleaner cross-package decoupling); or (b) tighten the `Candidate.Importance` GoDoc to say the slot may encode dep-proximity hints and embedding-backed scorers must renormalise.
- L7 — Replace `len(s)/4` with `utf8.RuneCountInString(s)/4` in `estimateTokens` (`internal/scheduler/context_package.go`). `len()` returns byte count and inflates estimates 2–4× for multibyte UTF-8 (CJK, emoji, accented Latin). Acceptable since estimates only order candidates by relative density, but the bias should be removed or documented.
- L9 — Add a `Packager.Build` test asserting the `remaining < 0 → 0` clamp covers a negative `budgetTokens` argument. The allocator never emits negatives so the contract is theoretical today, but PR 2's `MemoryFacade` may compute `B_step − B_memory_reservation` and could plausibly underflow.
- L10 — Add an explicit assertion in `tests/unit/python/test_context_package_wire_shape.py` that decoded `metrics.get("warnings", [])` returns `[]` when absent (since `Metrics.Warnings` uses `omitempty`). Locks the v1 wire-shape contract so a future tag change to non-omitempty surfaces.
- ✅ L11 (landed in PR 1b #219) — Add a per-(step, warning) sampler around the `zap.Warn` fan-out in `attachContextPackage`. A workflow with 50 steps each tripping `high_compression_ratio` produces 50 log lines per run today. **✅ Landed in PR 1b (#219).**
- ✅ L12 (landed in PR 1b #219) — Extract the RFC 0008 validation block from `internal/planner/planner.go` into a sibling `planner_context_budget.go` to give headroom under the 500-line soft cap. **✅ Landed in PR 1b (#219).**
- N5 — Add a one-line comment to the `_context_package`-collision skip in `attachContextPackage` referencing the `outputKeyRegex` (`^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?$`) that prohibits leading underscore — explains why the defence-in-depth guard cannot fire today.
- N6 — Reword the `context_budget` schema description in `schemas/workflow.schema.json`. Current text says `0 means inherit (equal-split)`; "inherit" suggests RFC 0006 agent-default semantics. Replace with: "0 (or omitted) participates in the equal-split allocation; > 0 sets a hard per-step override".
- N7 — Drop the diagnostic-loop deadline in `TestContextPackage_DisabledByDefault` from 3 s to 1 s (or short-circuit on terminal status) before the real `waitForRunStatus` (5 s) fires. Saves CI time on green runs.
- N8 — Update `Packager` GoDoc: replace "stateless and safe for concurrent use" with "Concurrency-safety depends on the injected `RelevanceScorer`; the default `HeuristicScorer` is safe."
- Wire-shape contract follow-up — promote the Python wire-shape test to read a Go-produced fixture (e.g. a Go test writes `json.Marshal(pkg)` to `tests/fixtures/context_package_v1.json` and the Python test reads it). Catches unilateral tag renames on either side.

##### From PR 1b review

Carry-over findings from the PR 1b (`feature/v030-rfc0008-context-metrics`, merged as #219) deep review. Each is self-contained so the merged history needs no external report.

- M10 — `markStepFailed` erases `RemainingContextBudget` on every failure path (`internal/scheduler/stage_runner.go`). PR 1b's stated purpose is "future scheduler-level retries can resume from the leftover budget." Today `UpdateStepState` full-replacement zeros the field on every `markStepFailed` call site, so a step that previously persisted a remainder loses it on any subsequent failure. The retry-resume contract is unreachable across success → fail → retry transitions. Fix (option A, code): read the prior `StepState` and copy `RemainingContextBudget` forward before writing, or accept it as a parameter to `markStepFailed` and thread it from the four call sites. Fix (option B, docs): amend the `RemainingContextBudget` docstring in `internal/state/state.go` and this plan to note that failure-path preservation lands in the retry-implementation PR (post-v0.3 — a TODO + tracking note suffices).
- M11 — `warningSampler.counts` (`sync.Map`) grows unbounded across the scheduler's lifetime (`internal/scheduler/context_package.go`). One entry per `(execID, stepID, warning)` tuple — a long-running orchestrator accumulates entries without bound. Fix: prune by run-completion event (the scheduler already knows terminal status via `executeRun`), or move the sampler under `WorkflowRun` so it is GC'd with the run.
- M12 — `shouldEmit()` calls `LoadOrStore(key, new(samplerCounter))` which allocates a fresh `samplerCounter` on every call even when the key already exists (`internal/scheduler/context_package.go`). Fix: `Load` first; only fall through to `LoadOrStore` on miss.
- L13 — Unreachable defensive cast `c, ok := value.(*samplerCounter); if !ok { return true }` in `shouldEmit()` (`internal/scheduler/context_package.go`). The fallback `return true` is also wrong — it would re-enable unsampled warns on the impossible path. Either `panic` to surface the impossible state in tests, or delete the branch entirely.
- L14 — `warningSampleCap = 1` with a cap-comparison wrapper (`takeOne` / `if c.hit >= warningSampleCap`) is over-engineering for a value that is the strictest possible. Either inline the check (`if c.hit > 0`) or raise to a meaningful sample rate (3–5) so the abstraction earns its keep. Consider renaming to `warningOnceCap` if the value stays at 1.
- L15 — `remainingContextBudgetForStep()` returns `allocated` even when `allocated < 0` under the nil-store / unrecognised-run fallback (`internal/scheduler/context_budget_remaining.go`). The allocator never emits negatives but the function's contract is "return the budget to use" and a negative value flows into the packager. Add `max(0, allocated)` clamp for defence in depth.
- L16 — `TestRemainingContextBudgetForStep_RemainderCappedAtAllocation` constructs a `StepState` with `Status: 0` (i.e. `RunPending`) (`internal/scheduler/context_metrics_test.go`). If the function ever filters by status this test silently passes without exercising the intended path. Set an explicit `Status: state.RunCompleted`.
- L17 — `TestContextPackage_PersistsCostMetricsAndRemainingBudget` asserts `assert.GreaterOrEqual(t, entry.ContextPackage.CandidatesAdmitted, 0)` which is tautological for a non-negative `int` (`internal/scheduler/context_package_persistence_test.go`). Replace with explicit per-step checks: s1 admits 0 upstream outputs, s2 admits 1 from `out1`.
- N9 — `NewContextPackageMetrics(admitted int, ...)` takes `admitted` as a plain `int` that is always derived from `len(pkg.StepOutputs)` at the only call site (`internal/cost/context_package_metrics.go`). Consider taking `*packaging.Package` directly so the helper cannot be miswired by a future caller.
- N10 — Sampler key construction `execID + "|" + stepID + "|" + warning` collides if any field contains `|` (`internal/scheduler/context_package.go`). Today all three fields are framework-controlled and cannot contain `|`, but `fmt.Sprintf("%q|%q|%q", execID, stepID, warning)` or a struct key is collision-free for defence in depth.
- N11 — `WorkflowScheduler.warningSampler` is embedded by value but contains a `sync.Map` (`internal/scheduler/scheduler.go`). The zero value is correct but a value-copy would silently share the underlying map's internal state. Add a `// noCopy` sentinel (a zero-size `noCopy` struct with a `Lock()` method) or embed `*warningSampler` to surface violations under `go vet -copylocks`.
- N12 — `recordStepUsage` GoDoc for the new `pkg` parameter is multi-paragraph and partially duplicates the `context_package_metrics.go` package header (`internal/scheduler/budget.go`). Trim to one sentence and link to the cost-side doc.

#### Key implementation details

- The 30-day calibration review is the **gate** for flipping RFC 0008 to `✅ Implemented`. If the metrics indicate the shipped defaults need adjustment (e.g. `memory_utilization_ratio` consistently > 0.95 → `episodic_cap` too low; consistently < 0.2 → too high), the retune ships in this PR as a `config/agents.yaml` default change. The retune is a one-line change per parameter; no code changes expected.
- Review-follow-up findings from PR 1–5 deep reviews are summarized inline here per the project convention. The committed text must not contain any `docs/pr-reviews/` path (those reports are local-only per [Status Hygiene rules](../development-workflow.md#status-hygiene)); each finding is restated in full so the merged history is self-contained.

#### Tests

- `make test` passes after any retuned defaults.
- `make validate` passes after `agents.yaml` changes.
- Doc-status pre-commit hook accepts the new `✅ Implemented` markers.

#### PR checklist

- [ ] 30-day calibration review summary recorded in `docs/rfcs/0008-calibration-review.md`
- [ ] All PR 1–5 deep-review follow-ups either resolved or explicitly deferred with a tracking issue
- [ ] ROADMAP.md RFC 0008 row → `✅ Implemented`
- [ ] [v0.3.0-plan.md](../v0.3.0-plan.md) Master Progress Overview row 4 → ✅
- [ ] No reference to `docs/pr-reviews/` files in this committed plan
- [ ] Plan-self-review: every cross-RFC pin in this plan ([RFC 0007 PR plan](0007-pr-plan.md) PR 3, [RFC 0011 PR plan](0011-pr-plan.md) PR 5, [RFC 0020 PR plan](0020-pr-plan.md) PR 4) still resolves and is reciprocated by the counterpart plan before the RFC flips to `✅ Implemented`

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| PR 1 budget contract disagrees with RFC 0007 loop budget contract | Both RFC PR plans were fleshed out in the combined Phase 1 PR per [v0.3.0-plan.md](../v0.3.0-plan.md#phase-1--author-the-six-rfc-pr-plans); contract gaps surface during deep-review of either flesh-out PR (this PR cross-checked [RFC 0007 PR plan](0007-pr-plan.md) PR 3 reciprocally). |
| `MemoryFacade.compress` shape disagrees with RFC 0020 summarize-on-close call site | Cross-reference RFC 0020 PR 4 in this plan's PR 2 review checklist. |
| Phase 4 (shared pools) scope creeps into authentication territory belonging to RFC 0009 | ACL is policy-only; auth tokens are RFC 0009 P3–4 (deferred to v0.4.0). |

---

## ROADMAP Hygiene

- **PR 1 opens** → ROADMAP RFC 0008 → `🚧 Implementing`; Master Progress Overview row 4 → 🔄.
- **PR 6 merges** → ROADMAP RFC 0008 → `✅ Implemented`; row 4 → ✅.
