# RFC 0017 — PR Implementation Plan

**RFC**: [0017-persona-memory-injection-budget.md](0017-persona-memory-injection-budget.md)
**Created**: 2026-04-21
**Branch prefix**: `feature/v022-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)

---

## Overview

RFC 0017 replaces three independent character caps (`_MAX_RELATIONSHIP_CHARS`, `_MAX_EPISODE_CHARS`, `_MAX_NOTE_CHARS`) and a brittle TICK-skip / `should_fall_back` heuristic in `agents/persona_runtime/memory_context.py` with:

1. A `MemoryBudget` token allocator that bounds memory injection at the *event* layer.
2. A `min_score` relevance threshold on `EpisodicMemory.recall` / `recall_notes` that filters noise at the *recall* layer.
3. An empty-context TICK short-circuit ([Section F](0017-persona-memory-injection-budget.md#f-empty-context-tick-short-circuit)) that closes a real cold-start cost-drain bug exposed once (1) and (2) make "zero memory admitted" a trustworthy signal.

The RFC spans 2 substantive phases plus a wrap-up phase. This plan splits the work into **7 PRs** so each stays under the 500-line BRANCHING.md limit and each leaves the repo in a passing-tests, lint-clean state.

> **Estimate calibration**: RFC 0005, 0006, and 0016 PRs landed within a 1.7× calibration factor relative to initial estimates. This plan applies the same factor. Sizes below are calibrated estimates.

**Prerequisite**: RFC 0016 fully merged (7/7 PRs) — chat interface establishes the `MESSAGE_RECEIVED` event path that Phase 2's "low-keyword `hi`" integration test relies on. RFC 0005 fully merged (memory subsystem foundation). No other RFC dependencies.

**Recommended merge order**: **PR 1** → **PR 2** → **PR 3** → **PR 4** → **PR 5** → **PR 6** → **PR 7**.

All PRs are sequential. PR 1 introduces the allocator type; PR 2 calls into it; PR 3 adds the recall-layer threshold that PR 4 wires into the allocator's call sites while removing the legacy gates; PR 5 builds on the `memory_admitted_tokens` signal exposed by PR 2.

---

## Dependency Graph

```
PR 1 (MemoryBudget allocator + token-aware _truncate_with_ellipsis)
  ↓
PR 2 (_inject_memory_context rewrite using MemoryBudget; gates retained)
  ↓
PR 3 (min_score on EpisodicMemory.recall / recall_notes)
  ↓
PR 4 (Wire min_score into _inject_memory_context; remove TICK skip + should_fall_back)
  ↓
PR 5 (Empty-context TICK short-circuit — Section F)
  ↓
PR 6 (Review follow-ups)
  ↓
PR 7 (RFC close)
```

---

## PR Sequence

### PR 1: `feature/v022-memory-budget` — MemoryBudget Allocator + Token-Aware Truncation

**Depends on**: Nothing (builds on v0.2.1 infrastructure)
**Branch**: `feature/v022-memory-budget`
**Estimated size**: ~200–350 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `agents/persona_runtime/memory_budget.py` | **New** — `MemoryBudget` dataclass with `remaining: int`, `min_tokens: int`, and `allocate(item_text: str) -> tuple[str | None, int]` returning `(admitted_text, admitted_tokens)` |
| `agents/persona_runtime/memory_context.py` | Switch `_truncate_with_ellipsis` to a token-aware mode when called by budget callers; preserve char-mode for any non-budget callers during the transition |
| `agents/tests/test_persona_runtime_memory_context.py` | Token-aware truncation tests (with and without `tiktoken`) |
| `agents/tests/test_memory_budget.py` | **New** — `MemoryBudget` unit tests covering every branch in [Section B](0017-persona-memory-injection-budget.md#b-memory-budget-allocator) |

#### Key implementation details

- `MemoryBudget.allocate(item_text)`:
  - If `remaining <= 0` → return `(None, 0)`.
  - Compute token count via the same path `WorkingMemory` uses (`tiktoken` if available, `len(text) // 4` fallback).
  - If item fits whole → admit whole, decrement `remaining` by exact token count.
  - If item exceeds `remaining` but truncating to `remaining` would still leave `>= min_tokens` → admit truncated copy via the new token-aware `_truncate_with_ellipsis`, decrement `remaining` to zero (or precisely by truncated token count).
  - Else → drop entirely, leave `remaining` unchanged.
- `_truncate_with_ellipsis(text, limit, *, mode: Literal["chars", "tokens"] = "chars")`:
  - `mode="tokens"` truncates at the token boundary, then re-encodes/re-decodes via `tiktoken` if available; falls back to `len(text) // 4 ≈ tokens` approximation by slicing chars proportionally if `tiktoken` is absent. Never panics on missing `tiktoken`.
  - The ellipsis `…` itself counts toward the token budget.
- No callers wired yet — PR 2 does that. This PR only introduces the type and its truncation primitive.

#### Tests

`MemoryBudget`:
- Empty input (`item_text == ""`) → returns `(None, 0)`, `remaining` unchanged.
- Item smaller than `remaining` → admitted whole; `remaining` decremented by exact token count.
- Item larger than `remaining` and truncated size `>= min_tokens` → admitted truncated; `remaining` reaches zero (or tracks exact admitted tokens).
- Item larger than `remaining` and truncated size `< min_tokens` → dropped; `remaining` unchanged.
- Sequence of items fills budget greedily in order; later items dropped when `remaining` exhausted; earlier items intact.
- `remaining=0` initial budget → every call returns `(None, 0)`.

`_truncate_with_ellipsis` token mode:
- Token count below limit → unchanged.
- Token count above limit, `tiktoken` available → truncated to token boundary; result re-encodes to ≤ `limit` tokens including ellipsis.
- Token count above limit, `tiktoken` unavailable (simulated via monkeypatch) → falls back to char-proportional slice; never panics.

#### PR checklist

- [ ] `pytest agents/tests/ -v` passes
- [ ] `ruff check agents/` clean
- [ ] `mypy agents/` clean
- [ ] `agents/persona_runtime/memory_budget.py` exports `MemoryBudget`
- [ ] `_truncate_with_ellipsis` accepts `mode="tokens"` keyword and falls back gracefully without `tiktoken`

---

### PR 2: `feature/v022-memory-context-rewrite` — `_inject_memory_context` Allocate-Loop

**Depends on**: PR 1 merged (`MemoryBudget` and token-aware `_truncate_with_ellipsis` available)
**Branch**: `feature/v022-memory-context-rewrite`
**Estimated size**: ~300–450 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `agents/persona_runtime/memory_context.py` | Replace `_MAX_RELATIONSHIP_CHARS`, `_MAX_EPISODE_CHARS`, `_MAX_NOTE_CHARS` with single `_MEMORY_BUDGET_TOKENS = 1500` (per [OQ1 resolution](0017-persona-memory-injection-budget.md#open-questions)). Rewrite `_inject_memory_context` as a uniform allocate-loop over the three tiers in priority order (relationship=8, episodic=7, notes=6 — [OQ4](0017-persona-memory-injection-budget.md#open-questions)). Each tier's items pass through `MemoryBudget.allocate()`. **Preserve the existing TICK skip and `should_fall_back` heuristic** — they are removed in PR 4 once the recall layer can stand in for them. Expose `memory_admitted_tokens` (sum returned by the allocator) on the function's return path so PR 5 can consume it. |
| `agents/tests/test_persona_runtime_memory_context.py` | Replace char-cap assertions with token-bound assertions: `working_memory.total_tokens()` after `_inject_memory_context` is `<= base_prompt_tokens + _MEMORY_BUDGET_TOKENS`. Add tests asserting tier ordering (relationship admitted before episodic before notes when budget is tight). Add a test verifying `memory_admitted_tokens` is returned and equals the sum of per-tier admissions. |

#### Key implementation details

- The allocate-loop iterates tiers in fixed priority order (no fairness — [OQ4](0017-persona-memory-injection-budget.md#open-questions)). For each tier, items are queried via the existing recall paths *unchanged in this PR* (PR 3 adds `min_score`).
- Each item from a tier is passed through `budget.allocate(format_item(item))`. The formatted, possibly-truncated string is then appended to the working memory section for that tier.
- `_MEMORY_BUDGET_TOKENS = 1500` is the committed Phase 1 default ([OQ1](0017-persona-memory-injection-budget.md#open-questions)). Acceptance does not gate retuning — any retune is a one-line constant change.
- The TICK skip and `should_fall_back` short-circuit at the top of `_inject_memory_context` are **kept verbatim**. Their removal is PR 4's job; isolating it makes the diff reviewable and bisectable.
- Return shape: the function previously returned implicitly via mutation of the working memory object. PR 2 adds an explicit return tuple `(memory_admitted_tokens: int, ...)` or attaches `memory_admitted_tokens` as an attribute on a small return record. The exact shape is pinned during PR 2 review; PR 5 only requires that the signal is reachable from the caller. Either way, callers ignoring the return value behave identically to today.

#### Tests

- Token bound holds: synthetic relationship/episodic/notes content far exceeding 1500 tokens → resulting working-memory injection ≤ 1500 tokens beyond baseline.
- Tier ordering: when tier-1 (relationship) consumes the entire budget, tiers 2 and 3 admit zero items.
- Mid-tier truncation: a single oversized note is admitted truncated when its truncated form ≥ `min_tokens`, dropped otherwise.
- `memory_admitted_tokens` returned equals the per-tier admitted-tokens sum.
- All previously-passing memory-context tests still pass (the TICK skip and `should_fall_back` behaviour is unchanged).

#### PR checklist

- [ ] `pytest agents/tests/ -v` passes
- [ ] `ruff check agents/` clean
- [ ] `mypy agents/` clean
- [ ] `_MAX_*_CHARS` constants removed from `memory_context.py`
- [ ] `_MEMORY_BUDGET_TOKENS` set to 1500
- [ ] TICK skip and `should_fall_back` heuristic preserved (removed in PR 4)
- [ ] `memory_admitted_tokens` reachable from `_inject_memory_context` callers
- [ ] ROADMAP.md RFC tracker row: status → 🚧 Implementing on this PR opening (per RFC Decision/Next Steps step 2)

---

### PR 3: `feature/v022-min-score-recall` — `min_score` on `EpisodicMemory.recall` / `recall_notes`

**Depends on**: PR 2 merged (so the budget allocator is in place; per-tier defaults make sense once tier-aware allocation exists)
**Branch**: `feature/v022-min-score-recall`
**Estimated size**: ~300–450 lines (implementation + tests + calibration script outputs)

#### Scope

| File | Change |
|------|--------|
| `agents/memory/episodic.py` | Add `min_score: float \| None = None` parameter to both `recall()` and `recall_notes()`. When `None` → behaviour identical to today. When set → FTS5 results below the threshold are filtered. BM25 raw scores are normalised to `[0, 1]` via the documented mapping in [Section C](0017-persona-memory-injection-budget.md#c-relevance-threshold-in-the-recall-layer). LIKE-fallback path documents that `min_score` is ignored when FTS5 is unavailable (LIKE has no scoring) and emits a one-time `WARNING` log on first such call. |
| `agents/memory/episodic.py` | Add module-level constants `_DEFAULT_EPISODIC_MIN_SCORE` and `_DEFAULT_NOTES_MIN_SCORE` ([OQ3 resolution: per-tier](0017-persona-memory-injection-budget.md#open-questions)). Values committed from the calibration script output. |
| *(throwaway)* `scripts/calibrate_min_score.py` | **Run locally, not committed.** Loads a populated FTS5 DB, computes BM25 score histograms across representative queries, prints recommended thresholds for episodes and notes. The PR description records the run output and the chosen values. |
| `agents/tests/test_episodic.py` | Add `min_score` boundary tests: `min_score=None` (default, no filter), `min_score=0.0` (no filter), `min_score=1.0` (almost everything filtered), `min_score` near the calibrated default (mixed results). Assert FTS5 normalisation produces values in `[0, 1]`. Assert LIKE-fallback ignores `min_score` and logs the warning once per process. |

#### Key implementation details

- BM25 normalisation: SQLite FTS5 returns negative BM25 scores where more-negative = better match. The contract is documented in [Section C](0017-persona-memory-injection-budget.md#c-relevance-threshold-in-the-recall-layer). The mapping clamps to `[0, 1]` and is exposed as a private helper for testability (`_normalize_bm25(raw: float) -> float`).
- `min_score` semantics: `None` preserves current behaviour exactly; explicit `0.0` is documented as "filter only items with literally zero relevance" and is *also* a no-op in practice but is semantically explicit (useful for callers that want to opt into the filtered path without choosing a threshold).
- Calibration script is throwaway — not committed. The PR description must include the exact command run, the input DB description, and the histogram summary that justifies the committed defaults. This pattern matches the RFC's [OQ2 resolution](0017-persona-memory-injection-budget.md#open-questions).
- `_inject_memory_context` is **not** modified in this PR. Wiring `min_score` into the call sites is PR 4's job. This isolates the recall-layer change from the gate-removal change.
- If FTS5 unavailable → LIKE-fallback emits `logger.warning("min_score requested but FTS5 unavailable; threshold ignored", extra={"path": "like_fallback"})` once per process (gated by a module-level flag).

#### Tests

- `recall(min_score=None)` returns identical results to current behaviour.
- `recall(min_score=0.0)` returns identical results (no item normalises below 0).
- `recall(min_score=1.0)` returns at most one perfect-match item (or empty).
- `recall(min_score=_DEFAULT_EPISODIC_MIN_SCORE)` against a fixture DB returns the curated subset.
- Same matrix for `recall_notes` with `_DEFAULT_NOTES_MIN_SCORE`.
- `_normalize_bm25` returns `0.0` for missing/`None` raw scores; clamps to `[0, 1]` for extreme inputs.
- LIKE-fallback path with `min_score=0.5` returns same results as `min_score=None` and emits exactly one warning across multiple calls in the same process.

#### PR checklist

- [ ] `pytest agents/tests/test_episodic.py -v` passes
- [ ] `ruff check agents/memory/` clean
- [ ] `mypy agents/memory/` clean
- [ ] `min_score` parameter added to both `recall` and `recall_notes`
- [ ] Per-tier defaults `_DEFAULT_EPISODIC_MIN_SCORE` and `_DEFAULT_NOTES_MIN_SCORE` committed with calibration values
- [ ] PR description records calibration script run and chosen values
- [ ] LIKE-fallback warning fires once per process

---

### PR 4: `feature/v022-memory-gate-removal` — Wire `min_score` and Remove Legacy Gates

**Depends on**: PR 3 merged (`min_score` available on recall methods)
**Branch**: `feature/v022-memory-gate-removal`
**Estimated size**: ~250–400 lines (implementation + integration test)

#### Scope

| File | Change |
|------|--------|
| `agents/persona_runtime/memory_context.py` | Pass per-tier defaults into the recall calls: episodic recall uses `_DEFAULT_EPISODIC_MIN_SCORE`, notes recall uses `_DEFAULT_NOTES_MIN_SCORE`. **Delete the TICK skip** at the top of `_inject_memory_context` (the recall threshold + budget allocator subsume it). **Delete the `should_fall_back` heuristic** for the same reason. |
| `agents/tests/test_persona_runtime_memory_context.py` | Remove tests that asserted the TICK skip and `should_fall_back` shapes; replace them with tests asserting the new behaviour: TICK events now flow through `_inject_memory_context` and admit `~0` tokens via the threshold; low-keyword "hi"-style messages similarly admit `~0` tokens. |
| `tests/integration/test_memory_budget_e2e.py` | **New** — synthetic event-stream integration test. Drives a persona agent through a sequence of `TICK`, `MESSAGE_RECEIVED("hi")`, `MESSAGE_RECEIVED("substantive question with keywords")`, and `TICK` events. Asserts: (a) `working_memory.total_tokens()` stays below ceiling at every step; (b) low-signal events inject `~0` memory tokens (`memory_admitted_tokens == 0` ± slack); (c) substantive events inject non-zero memory tokens and ≤ `_MEMORY_BUDGET_TOKENS`. |

#### Key implementation details

- The TICK skip and `should_fall_back` are deleted, not commented out. Their git-blame remains traceable to PR 2's preservation note.
- `_inject_memory_context` no longer special-cases event type at the top. The recall threshold makes TICK and "hi" produce empty result sets; the allocator then admits zero tokens; the existing flow handles the rest.
- The PR 5 `memory_admitted_tokens` consumer is *not* added here — this PR's only behavioural promise is "low-signal events inject ~zero memory tokens". The TICK short-circuit (which acts on that promise) is PR 5.
- Integration test uses an in-memory SQLite DB seeded with a small but realistic memory snapshot (relationships, episodes, notes). Reusable as a fixture for PR 5's tests.

#### Tests

- Unit test: TICK event with empty episodic store → `_inject_memory_context` admits 0 tokens (was: skipped entirely).
- Unit test: TICK event with one high-relevance episode → admits non-zero tokens (was: skipped).
- Unit test: `MESSAGE_RECEIVED("hi")` with cluttered episodic store → admits ≤ a small upper bound (threshold filters most items).
- Unit test: `MESSAGE_RECEIVED("substantive query with keywords from episodes")` → admits multi-tier content up to budget.
- Integration test: 4-event stream as described above; budget ceiling holds at every step; low-signal events admit ~0; substantive events admit non-zero and ≤ ceiling.

#### PR checklist

- [ ] `pytest agents/tests/ -v` passes
- [ ] `pytest tests/integration/ -v` passes
- [ ] `ruff check agents/` clean
- [ ] `mypy agents/` clean
- [ ] TICK skip removed from `_inject_memory_context`
- [ ] `should_fall_back` heuristic removed
- [ ] Per-tier `min_score` defaults wired into recall calls
- [ ] Integration test asserts token-bound and low-signal-zero contracts

---

### PR 5: `feature/v022-empty-context-tick-shortcircuit` — Section F TICK Short-Circuit

**Depends on**: PR 4 merged (low-signal events provably admit zero memory tokens, making the short-circuit safe)
**Branch**: `feature/v022-empty-context-tick-shortcircuit`
**Estimated size**: ~200–350 lines (implementation + tests)

> **Open at PR-plan time**: which file owns the TICK handler that issues the LLM call. RFC's Files Touched lists `agents/persona_runtime/__init__.py (or TICK handler module)`. The actual call site is in `agents/persona_behavior.py` / `agents/persona.py` — *not* in [`agents/tick.py`](../../agents/tick.py), which only schedules ticks via `TickScheduler`. PR 5's first commit pins the file by name with an inline reference back to this note. The two-accessor contract below assumes `PersonaAgent.handle_event(event)` (or its equivalent) is the call site.

#### Scope

| File | Change |
|------|--------|
| `agents/persona_runtime/memory_context.py` | Confirm `memory_admitted_tokens` is exported on the return path (added in PR 2; this PR adds a stable accessor if PR 2's shape needs adjustment). |
| `agents/persona_behavior.py` (or whichever module owns the TICK handler — pinned during PR 5 review) | Add the four-condition short-circuit guard before the LLM call: `event.type == TICK` AND `memory_admitted_tokens == 0` AND no active goal payload AND no pending conversation turn. On match: record `DO_NOTHING` (advances `idle_count`), log at `DEBUG` with `extra={"reason": "empty_context_tick", "agent_id": self.agent_id}`, return without invoking the LLM. |
| *(if needed)* `agents/persona.py` | Read-only accessors for the two ambient state predicates if they aren't already directly inspectable: `_has_active_goal_payload() -> bool` and `_has_pending_turn() -> bool`. No new persisted state. The exact attribute names are pinned in the same PR-5 first commit that pins the TICK handler module. |
| `agents/tests/test_persona_tick_shortcircuit.py` | **New** — covers all five required cases from [RFC test strategy](0017-persona-memory-injection-budget.md#phase-2-relevance-threshold-in-recall-layer--gate-removal): (a) empty-context TICK with no goal/turn → no LLM call, `idle_count` increments; (b) non-empty TICK → LLM call still issued; (c) empty-context TICK **with active goal payload** → LLM call still issued (positive guard); (d) empty-context TICK **with pending conversation turn** → LLM call still issued (positive guard); (e) **non-TICK** event with `memory_admitted_tokens == 0` (e.g., low-keyword `MESSAGE_RECEIVED`) → LLM call still issued (the short-circuit must not fire outside TICK). |

#### Key implementation details

- The short-circuit guard reads ambient state via accessors, not direct attribute access, so the unit tests can monkeypatch them cleanly.
- `idle_count` is the existing counter on [`TickScheduler`](../../agents/tick.py). The short-circuit increments it via the same path a regular `DO_NOTHING` action would (so `idle_after_ticks` semantics are preserved exactly).
- Logging: `DEBUG`-level with `reason="empty_context_tick"`. RFC 0019 (OTEL Completion) may later promote this to a counter; the field name is chosen to be stable for that future work without renaming.
- No telemetry counter added in this PR — the RFC's [Section F](0017-persona-memory-injection-budget.md#f-empty-context-tick-short-circuit) explicitly defers metrics to RFC 0019.

#### Tests

Five required cases (a)–(e) above, each as a separate test function. Plus:

- Idle suppression engages immediately on cold-start (verifies the *outcome* of the short-circuit: `idle_count` reaches `idle_after_ticks` after that many empty-context TICK events, with zero LLM calls in between).
- The `DEBUG` log entry is emitted exactly once per short-circuited tick with the expected `extra` fields.

#### PR checklist

- [ ] `pytest agents/tests/test_persona_tick_shortcircuit.py -v` passes
- [ ] All five required cases (a)–(e) implemented as separate test functions
- [ ] `ruff check agents/` clean
- [ ] `mypy agents/` clean
- [ ] TICK handler module pinned by name in the PR description (resolves the open-at-plan-time question)
- [ ] `idle_count` increments on short-circuited ticks
- [ ] DEBUG log with `reason="empty_context_tick"` field

---

### PR 6: `feature/v022-rfc0017-followups` — Review Follow-Ups

**Depends on**: PR 5 merged (all core PRs complete)
**Branch**: `feature/v022-rfc0017-followups`
**Estimated size**: ~150–300 lines (fixes + new tests)

#### Scope

Review findings from PRs 1–5, grouped by component. Items below are populated from PR review reports as PRs are reviewed.

##### From PR 1 review

*(populated after PR 1 review)*

##### From PR 2 review

*(populated after PR 2 review)*

##### From PR 3 review

*(populated after PR 3 review)*

##### From PR 4 review

*(populated after PR 4 review)*

##### From PR 5 review

*(populated after PR 5 review)*

#### Tests

Test gaps deferred from earlier PR reviews are added here.

#### PR checklist

- [ ] All deferred review findings addressed
- [ ] All deferred test gaps filled
- [ ] `make test` passes
- [ ] `make lint` clean
- [ ] `make validate` passes

---

### PR 7: `feature/v022-rfc0017-close` — RFC Close

**Depends on**: PR 6 merged (all follow-ups addressed)
**Branch**: `feature/v022-rfc0017-close`
**Estimated size**: ~50–100 lines (status updates only)

#### Scope

| File | Change |
|------|--------|
| `docs/rfcs/0017-persona-memory-injection-budget.md` | Status → `✅ Implemented` |
| `ROADMAP.md` | RFC 0017 status → `✅ Implemented`; merged count = `7/7`; component status updates; merged-PR rows for PRs 6 and 7; header `Last updated` and `Current milestone` refreshed |
| `docs/rfcs/0017-pr-plan.md` | All checklists complete; merged-PR numbers and dates filled in for every PR |

`CHANGELOG.md` update is **deferred to the v0.2.2 release process**, mirroring RFC 0016's PR 7 precedent.

#### Tests

No new tests. `make test`, `make lint`, and `make validate` are run to confirm no regressions from doc-only changes.

#### PR checklist

- [ ] RFC 0017 status = `✅ Implemented`
- [ ] ROADMAP.md RFC Tracker updated
- [ ] ROADMAP.md merged-PR history includes PRs 1–7
- [ ] `make test` passes
- [ ] `make lint` clean
- [ ] `make validate` passes
