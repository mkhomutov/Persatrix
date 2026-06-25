# RFC 0034 Phase 3 — Instrumentation + Cache Bound — PR Implementation Plan (v0.3.10 scope)

**RFC**: [0034-persona-conversational-working-memory.md](0034-persona-conversational-working-memory.md) (Phase 3 — [§Phase 3 instrumentation and tuning](0034-persona-conversational-working-memory.md#phase-3-instrumentation-and-tuning) / [§F caching and fetch policy](0034-persona-conversational-working-memory.md#f-caching-and-fetch-policy))
**Phase 1 plan**: [0034-pr-plan.md](0034-pr-plan.md) (DM channels — shipped v0.3.1; [§Future Phases](0034-pr-plan.md#future-phases) holds the two carry-forwards this plan discharges)
**Phase 2 plan**: [0034-phase2-pr-plan.md](0034-phase2-pr-plan.md) (group channels — shipped v0.3.7)
**Created**: 2026-06-25
**Branch prefix**: `feature/v0310-rfc0034-phase3-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.10-plan.md](../v0.3.10-plan.md) (an *optional invisible fold-in* — synergistic with the RFC 0051 headline, which leans on the same transcript/cache seam; cuttable without touching the headline)

---

## Overview

Phases 1–2 shipped the Conversation Window's behaviour: every persona turn rebuilds the LLM `messages` array from the channel store, for DM channels (Phase 1, v0.3.1) and multi-peer group channels (Phase 2, v0.3.7). Both phases **deliberately shipped no telemetry** ("shipping inert counters now would invite premature dashboard work" — [Phase 1 plan](0034-pr-plan.md)) and left the per-turn fetch cache **unbounded**.

Phase 3 closes both gaps the [Phase 1 §Future Phases](0034-pr-plan.md#future-phases) recorded:

1. **The fetch cache has no eviction.** `_WINDOW_CACHE` never deleted an entry, so over a long-lived orchestrator serving many distinct `(channel, limit, agent)` triples the dict grew without bound. **Fixed:** a bounded LRU.
2. **No observability.** The `max_turns` / `max_tokens` defaults and the cache bound cannot be re-tuned from data because nothing is measured. **Fixed:** the `conversation_window.*` OTEL metric family.

The data-driven **retune** of `max_turns` / `max_tokens` is explicitly *not* in this PR — it is gated on collecting a telemetry sample, which only exists once the metrics this PR ships have run. The defaults (`N=20`, `max_tokens=2048`) are unchanged; the retune is a one-line constant change tracked as the RFC's sole remaining follow-up.

This is a single PR (the phase is small and cohesive), authored TDD-first.

## Design / what ships

### A. Bounded LRU fetch cache

`_WindowCache` — a small bounded LRU (`collections.OrderedDict`): both a `get` hit and a `put` mark an entry most-recently-used; on overflow the least-recently-used entry is evicted; an in-place key update (the §F message-id re-stamp) refreshes recency without growing the cache, so a hot channel is never evicted by its own invalidation. `put` returns the eviction count so the call site charts the metric without the cache depending on the observability layer. Capacity is a process-global constant `DEFAULT_WINDOW_CACHE_CAPACITY = 256` — **not** a per-agent config knob (the cache is shared across personas in a process), a one-line retune like the `max_turns` default. A capacity `< 1` is rejected at construction (a zero/negative bound would evict every fresh insert — a cache that never caches).

### B. `conversation_window.*` instrumentation

Module-owned instruments in `agents/observability/_metrics_conversation_window.py`, registered in the same `_Instruments.__init__` loop as the other helper-metric modules:

| Instrument | Kind | Use |
|------------|------|-----|
| `conversation_window.cache_access` | counter, `result=hit\|miss` | cache-hit rate = `hit / (hit + miss)` over *consulted* look-ups |
| `conversation_window.cache_evictions` | counter | a sustained rate ⇒ the bound is undersized (thrashing) |
| `conversation_window.fetch_duration` | histogram (ms) | the per-turn fetch cost the cache exists to avoid (successful fetch only) |
| `conversation_window.fallback` | counter, `reason=fetch_failed\|fetch_none` | the silent degrade-to-current-event-only the [§F risk table](0034-pr-plan.md) flagged as masking a history-endpoint outage |

Every `record_*` helper is a no-op until registered and best-effort (`contextlib.suppress`) — the look-up/fetch already resolved, so a metric-export hiccup must never propagate and undo the turn (the same contract as `record_deliberation` / `record_reflexion`).

### C. File-size constraint (the load-bearing structural call)

Two files are at the 500-line review cap (`scripts/checks/file_size.py`, not grandfathered):

- **`agents/observability/metrics.py`** — at the cap, so the new instruments are **module-owned** in `_metrics_conversation_window.py` (the RFC 0051 precedent), and `metrics.py` gains **zero net lines** (the standalone helper-imports comment was dropped into a trailing comment to make room for the one new import + tuple entry).
- **`agents/persona_runtime/conversation_window.py`** — already at exactly 500 lines, so the cache concern (`_WindowCache`, the singleton, the constant, and the cache-fronted `_fetch_window` — the cache's only consumer) was **extracted** into a new `agents/persona_runtime/_conversation_window_cache.py`. `conversation_window.py` re-exports `_WINDOW_CACHE` so the existing test cache-reset fixtures keep working unchanged.

## Test strategy (TDD)

- **`tests/unit/python/test_conversation_window_cache.py`** — pins `_WindowCache` LRU semantics in isolation: put/get round-trip, eviction at capacity, `get`/in-place-update refresh recency (LRU, not FIFO), update never grows/evicts, `put` returns the eviction count, `clear`, capacity `< 1` rejected.
- **`tests/unit/python/test_conversation_window_metrics.py`** — drives the real emit sites through `build_conversation_messages` with an `InMemoryMetricReader` (mirrors `test_reflexion_metrics.py`): miss-then-hit charts both, a real fetch records duration, fetch-raise / fetch-None chart `fallback` and degrade to current-event-only, a forced overflow charts `cache_evictions`, disabled/channelless turns emit nothing, and every `record_*` helper swallows injected instrument errors.

## Acceptance

- [x] Both new test files green; the existing conversation-window suites + the integration continuity test stay green (the cache-reset fixtures still resolve via `conversation_window._WINDOW_CACHE`).
- [x] `metrics.py` and `conversation_window.py` both ≤ 500 lines; `make`-equivalent file-size gate passes.
- [x] `ruff` (agents + tests) + `mypy` clean on the changed modules.
- [x] RFC 0034 → `✅ Implemented`; ROADMAP RFC Master Index row updated; `make rfcs` regenerates [INDEX.md](INDEX.md); CHANGELOG `[0.3.10]` note added.

## Out of scope / follow-up

- **Re-tune `max_turns` / `max_tokens` defaults** — gated on a telemetry sample from the metrics this PR ships (the RFC's sole remaining item).
- **The [§F "Known gap"](0034-persona-conversational-working-memory.md#f-caching-and-fetch-policy) (a)→(b) cache re-spec** — optional, deferred until the hit-rate telemetry justifies it.
