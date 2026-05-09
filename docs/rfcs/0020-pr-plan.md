# RFC 0020 — PR Implementation Plan

**RFC**: [0020-interaction-lifecycle.md](0020-interaction-lifecycle.md)
**Created**: 2026-04-25
**Branch prefix**: `feature/v030-rfc0020-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.0-plan.md Phase 1 (combined plans PR)](../v0.3.0-plan.md#phase-1--author-the-six-rfc-pr-plans)

---

## Overview

RFC 0020 introduces **Interaction** as a first-class concept and makes the *interaction*, not the message, the unit of episodic memory. The v0.3.0 scope covers Phases 1–3; Phase 4 (topic-shift detection) is deferred post-v0.3.0.

This plan splits the work into **7 PRs**. Each stays under the [BRANCHING.md](../BRANCHING.md) 500-line soft cap and leaves the repo in a passing-tests, lint-clean state.

> **Estimate calibration**: prior RFC 0005 / 0006 / 0016 / 0017 PRs landed within a 1.7× factor of the initial estimate. This plan applies the same factor.

**Prerequisite**: v0.2.3 merged (✅ — released 2026-04-24). RFC 0020 P1 is the dep-chain root for v0.3.0 and has no v0.3.0-RFC dependencies; see [v0.3.0-plan.md §Dependency Graph](../v0.3.0-plan.md#dependency-graph-rfc-level).

**Cross-RFC sequencing**:
- **PR 1 of this plan must merge before any RFC 0008, 0011, or 0021 implementation PR opens** — they all consume `started_at` / `closed_at` columns or `InteractionTracker.add_turn`.
- **PR 4 (Phase 2 close path)** pairs with the RFC 0008 `MemoryFacade` summarization hook. Coordinate landing order with the RFC 0008 PR plan; the joint pairing rationale is documented in [ROADMAP.md](../../ROADMAP.md#why-rfc-0020-p2-pairs-with-rfc-0008-d).
- **PR 5 (Phase 3)** is the joint-delivery PR with RFC 0011 P3 — see RFC 0011 PR plan, same PR row.

**Memory Quality Roadmap §D — Outcome-tagged importance** ([memory-quality-roadmap.md §D](../memory-quality-roadmap.md#d-outcome-tagged-importance-not-turn-count-importance)). Resolves [RFC 0020 Open Question #6](0020-interaction-lifecycle.md#open-questions). Because PR 4 (summarize-on-close) is already merged, §D ships as a **v0.3.x carve-out PR** after PR 7 closes — not as an in-line PR-4 amendment. Surface (additive only):

- Summarizer prompt change in [`agents/persona_runtime/summarize_close.py`](../../agents/persona_runtime/summarize_close.py) emits `outcome ∈ {neutral, agreement, conflict, disclosure, commitment}` and `emotional_weight ∈ [0.0, 1.0]` alongside the prose summary.
- New columns on `interactions` (additive, nullable for legacy rows): `outcome TEXT`, `emotional_weight REAL`.
- `_compute_interaction_importance` formula change: `importance = clamp(0.4 + 0.4 * emotional_weight + outcome_bonus, 0, 1)`, where `outcome_bonus` is a small lookup (`disclosure: +0.2`, `commitment: +0.2`, `conflict: +0.15`, `agreement: +0.05`, `neutral: 0.0`). `turn_count` becomes a tiebreaker, not a primary signal.
- Backward compatibility: legacy rows with `outcome IS NULL` continue to use the existing `0.3 + 0.05 * turn_count` formula; OQ #6 resolution applies only to new rows.

Tracked as **MQ-1** in [v0.3.0-plan.md §Memory Quality Follow-Ups](../v0.3.0-plan.md#memory-quality-follow-ups-v03x-and-beyond). Branch suggestion: `feature/v03x-rfc0020-outcome-tags`.

---

## Dependency Graph

```
PR 1 (InteractionTracker + schema migration + boundary detector interfaces)
  ↓
PR 2 (Single-turn routing — TICK, tool-only — through the tracker; parity test)
  ↓
PR 3 (Multi-turn aggregation for human-chat + DM; close on session end / idle)
  ↓
PR 4 (Summarization-on-close LLM hook + closing-state janitor + record_interaction move)
  ↓
PR 5 (Phase 3 — joint with RFC 0011 P3: per-channel scoping + channel memory wired to InteractionTracker)
  ↓
PR 6 (Review follow-ups)
  ↓
PR 7 (RFC close)
```

---

## PR Sequence

### PR 1: `feature/v030-rfc0020-tracker-schema` — InteractionTracker + Additive Schema + Boundary-Detector Interfaces

**Depends on**: Nothing (v0.2.3 baseline).
**Estimated size**: ~350–500 lines (new module + migration + unit tests).

#### Scope

| File | Change |
|------|--------|
| `agents/memory/interactions.py` | **New** — `InteractionTracker` keyed by scope; `start`, `add_turn`, `close`, `idle_check` methods. No LLM calls (placeholder summary). |
| `agents/memory/episodic.py` | Schema migration: add `interaction_id`, `started_at`, `closed_at`, `turn_count`, `scope` columns + `idx_episodes_scope` index. `store_episode` accepts the new fields with backward-compatible defaults. |
| `agents/memory/boundary_detectors.py` | **New** — `BoundaryDetector` Protocol + `StructuralCloseDetector`, `IdleGapDetector`, `TopicShiftDetector` no-op. |
| `tests/unit/python/test_interaction_tracker.py` | **New** — tracker lifecycle, scope keying, idle-check semantics. |
| `tests/unit/python/test_episodic_schema_migration.py` | **New** — fresh DB migration; legacy DB upgrade; column defaults. |
| `agents/observability/metrics.py` | New counters: `interactions.opened`, `interactions.closed`, `interactions.closed.by_idle_gap`, `interactions.closed.by_structural`, `interactions.summary.failed`. |

#### Key implementation details

- `InteractionTracker.add_turn(scope, turn)` returns the open `Interaction`; if no interaction is open in scope, starts a new one and emits `interactions.opened`.
- `idle_check(now)` is called from a periodic janitor (wired in PR 4); for PR 1 it is exercised only by tests.
- Schema migration is **additive** — existing rows keep `interaction_id=NULL`; recall code in PR 2 handles both paths.
- `BoundaryDetector` Protocol is the seam for Phase 4's topic-shift implementation; PR 1 ships a registry that returns the no-op detector by default.

#### Tests

- Tracker creates a single interaction for repeated `add_turn` calls in the same scope.
- Different scopes produce independent interactions.
- `close(reason="structural")` increments `interactions.closed.by_structural`; idle-gap closure increments the by-idle-gap counter.
- Schema migration is idempotent; running twice produces no diff.
- Recall on a mixed-schema DB (legacy rows + new rows) returns both.

#### PR checklist

- [x] `pytest agents/tests/ tests/unit/python/ -v` passes
- [x] `ruff check agents/` clean
- [x] `mypy agents/` clean
- [x] Schema migration shipped behind the existing `EpisodicMemory.initialize()` path
- [x] ROADMAP.md row for RFC 0020 → `🚧 Implementing` on this PR opening
- [x] Master Progress Overview row 2 → 🔄 In progress

---

### PR 2: `feature/v030-rfc0020-single-turn-routing` — Single-Turn Routing Through Tracker

**Depends on**: PR 1.
**Estimated size**: ~300–450 lines.

#### Scope

| File | Change |
|------|--------|
| `agents/persona_runtime/__init__.py` | Per-agent `InteractionTracker` instantiation; no shared state across agents. |
| `agents/persona_runtime/state_persistence.py` | `_StatePersistenceMixin._store_event_episode()` — positive allowlist routes single-turn events (TICK + 6 tool-only types) through `InteractionTracker.add_turn` + `close(reason="structural")`; multi-turn events (`MESSAGE_RECEIVED`, `MENTION`) retain the legacy NULL-interaction shape until PR 3; unknown event types warn and fall back to legacy shape. `scope` column carries `event_type.value` (or `SCOPE_TICK` for ticks). |
| `agents/persona_runtime/action_loop.py` | `_on_event_inner()` step 6 delegates to `_store_event_episode`; preserves log-and-continue semantics on persistence failure. |
| `tests/integration/test_interaction_single_turn_parity.py` | **New** — behavioral parity vs. pre-RFC episode shape for TICK and tool-only events; pins the PR 2/PR 3 boundary by parametrizing the multi-turn legacy-shape case over `MESSAGE_RECEIVED` + `MENTION`; covers the `store_episode` failure-swallowed-and-logged contract. |

#### Key implementation details

- TICK and tool-only paths are the easy case — start, one turn, close. Behavior parity is verifiable by comparing pre/post episode counts and summary text.
- Multi-turn aggregation (human-chat, DMs, channels) is **not** wired in this PR — that is PRs 3 + 5.

#### Tests

- TICK event with empty episodic store produces exactly one closed-interaction episode.
- Tool-only event ditto.
- Episode count after N TICKs equals N (parity vs. pre-RFC).

#### PR checklist

- [x] `pytest tests/integration/ -v` passes
- [x] Parity test green
- [x] No change to working-memory token bound (RFC 0017 invariant preserved)

---

### PR 3: `feature/v030-rfc0020-multi-turn-aggregation` — Multi-Turn for Human-Chat + DM

**Depends on**: PR 2.
**Estimated size**: ~350–500 lines.

#### Scope

| File | Change |
|------|--------|
| `agents/persona_runtime/state_persistence.py` | Multi-turn aggregation handler `_handle_multi_turn_event` for `MESSAGE_RECEIVED` / `MENTION`; turns accumulate in the open interaction; close on session-end metadata or idle gap. Cross-scope idle flush at the top of `_store_event_episode`. Strict-truthy session-end metadata parsing; structural-envelope-only turn payload (no message body). |
| `agents/persona_runtime/__init__.py` | Plumb `interaction_idle_timeout_sec` from `config["memory"]` into `InteractionTracker(idle_timeout_sec=…)` with `<= 0` reject + default fallback. |
| `agents/memory/interactions.py` | `Clock` Protocol seam (`_DEFAULT_CLOCK: Clock = time.time`; per-instance `self._clock`) replacing scattered `time.time()` defaults. RFC 0021 P1 will alias `Clock` to its canonical type. |
| `agents/memory/boundary_detectors.py` | `MaxTurnsDetector` wired into `default_detectors()` after `IdleGapDetector`. |
| `schemas/agent.schema.json` | Additive entry for `interaction_idle_timeout_sec` in `memory` block (`exclusiveMinimum: 0`, default 600). |
| `tests/integration/test_interaction_multi_turn.py` | **New** — ten-turn collapse, idle-gap closure, DM scope symmetry, single-turn parity sentinel, parametrised session-end truthiness matrix, message-body-not-persisted assertion. |
| `tests/integration/test_interaction_single_turn_parity.py` | Inverted multi-turn assertions from PR-2 "legacy NULL shape" to PR-3 "open scope, no episode" contract. |
| `tests/unit/python/test_interaction_tracker.py` | `Clock` seam coverage (per-instance clock injection). |

#### Key implementation details

- "Session end" = `metadata.chat_end` / `metadata.session_end` strict-truthy values (RFC 0016 emits these post-PR-3) or `IdleGapDetector` close on `idle_check`.
- DM scope keying: `(local_agent_id, peer_id)` — symmetric so the agent's own outbound messages count toward the same interaction. `channel_id` takes precedence over `sender_id` when both are set; PR 5 will reconcile per-channel routing.
- `Clock` seam at the tracker level today; `_LLMPersonaAgent` does not yet forward a `clock=` kwarg (deferred — see PR 6 finding #16). RFC 0021 P1 will swap the alias.

#### Tests

- Ten turns from the same chat session collapse into one interaction.
- Idle-gap closure: clock-advance in test produces a closed interaction; subsequent turn opens a new one.
- DM scope symmetry: A→B and B→A in a DM count toward the same interaction.

#### PR checklist

- [x] Multi-turn integration test green
- [x] No regression on PR 2's single-turn parity test

---

### PR 4: `feature/v030-rfc0020-summarize-on-close` — Summarization Hook + Janitor + record_interaction Move

**Depends on**: PR 3, RFC 0008 PR 2 (`MemoryFacade.compress` surface — this PR consumes the compression contract, not the per-step context-budget contract).
**Estimated size**: ~400–500 lines.

#### Scope

| File | Change |
|------|--------|
| `agents/memory/interactions.py` | Summarization-on-close LLM call — uses the model selection from `optimization.yaml` → `context_management.summarization.model`. |
| `agents/memory/interactions.py` | `closing`-state janitor with `closing_grace_sec` enforcement and fallback summary text. |
| `agents/memory/relationship_mutations.py` | Move `record_interaction` call site from per-event handler to the interaction-close path. |
| `agents/persona_runtime/__init__.py` | `auto_reflect_after` counter switches to increment on close. |
| `tests/integration/test_summarize_on_close.py` | **New** — ten-turn session produces one episode with a coherent summary; failure path falls back to the synthetic summary. |
| `docs/rfcs/0020-interaction-lifecycle.md` | Add the "Migration Notes" appendix (recalibration checklist for trust-bootstrap thresholds). |

#### Key implementation details

- Trust-bootstrap thresholds that assumed per-message `interaction_count` increments are recalibrated as part of this PR; the RFC's Migration Notes appendix is the single owner of the recalibration table.
- Summarization failure (LLM error, timeout) → fallback summary text + `interactions.summary.failed` counter increment; the interaction still transitions to `closed`.
- Janitor runs on the existing tick cadence — no new scheduler.

#### Tests

- Multi-turn session → single coherent summary.
- Summarization failure → fallback path; counter increment.
- `record_interaction` called exactly once per closed interaction (not per turn).
- Janitor closes a stuck `closing` interaction after `closing_grace_sec`.

#### PR checklist

- [x] Migration Notes appendix lands with this PR
- [x] RFC 0008 `MemoryFacade.compress` import resolves (cross-RFC dep is concrete)
- [x] No regression on RFC 0017 token-bound contract

---

### PR 5: `feature/v030-rfc0020-channel-integration` — Phase 3 (joint with RFC 0011 P3)

**Depends on**: PR 4, RFC 0011 PR 4 (Phase 2 — proto + agent delivery).
**Estimated size**: ~300–500 lines (this RFC's surface only — RFC 0011's joint PR carries the channel-side wiring).

#### Scope

| File | Change |
|------|--------|
| `agents/persona_runtime/__init__.py` | `CHANNEL_MESSAGE` event handler routes each turn to `InteractionTracker.add_turn` with the scope derived per RFC 0020 §G. |
| `agents/memory/facade.py` | `MemoryFacade.retrieve_relevant` filters out rows whose summary is the `SUMMARY_PENDING_TEXT` sentinel (defense in depth — the two-phase close path can race the recall caller). |
| `agents/memory/interactions.py` | Per-channel scope helpers idempotent in the prefix (accept either the bare key or the wire-side channel id) so callers don't have to strip `group:` / `thread:` at every site. |
| `agents/persona_runtime/state_persistence.py` | `_scope_for_multi_turn_event` discriminates DM / thread / group via `payload.channel_type` (with channel-id-prefix fallback) and `event.thread_id`. PR 3 thread-only fallback removed. |
| `tests/integration/test_channel_interaction_scoping.py` | **New** — scope-discrimination matrix + six-agent / 15-message acceptance: one episode per agent on close, not 15 per-message episodes. |
| `tests/unit/python/test_memory_facade.py` | Defense-in-depth recall regression: `[summary pending]` row filtered, `[interaction summary unavailable]` row preserved. |

#### Key implementation details

- **Joint delivery** with [RFC 0011 PR plan §PR 5](0011-pr-plan.md#pr-sequence) — both PRs land in the same merge window. If pairing slips, RFC 0011 P3 ships per-event episodic writes and this PR backfills in v0.3.x (documented as accepted divergence in both RFCs).
- Thread archive + channel-leave structural-close hooks ride the existing `StructuralCloseDetector` path; channel-side wiring (calling `Interaction.structural_close_reason = REASON_STRUCTURAL` on archive / leave) lands in RFC 0011 PR 5 — this RFC's tracker side already supports it.
- Scope helpers are now idempotent: `scope_for_group("group:planning") == scope_for_group("planning") == "group:planning"`. PR 3 callers passing the bare name continue to work; PR 5 callers pass the wire-side channel id.

#### Tests

- Group / DM / thread channel types route to their canonical scope builder.
- `event.thread_id` set takes precedence over `channel_type` (a thread reply inside a group rolls under the thread, not the parent channel).
- Channel-id-prefix fallback when `channel_type` is missing (legacy chat path).
- Six-agent group-channel acceptance: one closed-interaction episode per agent on `chat_end`.
- DM and group scopes for the same peer remain isolated.
- Defense-in-depth filter drops `SUMMARY_PENDING_TEXT` from `retrieve_relevant` results; `SUMMARY_UNAVAILABLE_TEXT` (janitor fallback) is preserved.

#### PR checklist

- [x] Joint with RFC 0011 PR 5 — both PRs reference each other's PR number
- [x] Channel-scoping integration test green
- [x] No regression on PR 4's summarization tests

---

### PR 6: `feature/v030-rfc0020-followups` — Review Follow-Ups

**Depends on**: PR 5.
**Estimated size**: ~200–400 lines.
**Status**: 🚧 Slice 7 of N in flight. Slice 1 (PR-4 review #20–#30) ✅ merged as [#266](https://github.com/mkhomutov/Persatrix/pull/266). Slice 2 (PR-1 review #2 + #3 — typed `CloseReason` + table-driven `_emit_closed` dispatch + the three missing per-reason subtotal counters) ✅ merged as [#296](https://github.com/mkhomutov/Persatrix/pull/296). Slice 3 (PR-1 review #1 + #4 + #5 — drop redundant `commit()` on the `_apply_migration_5`/`_apply_migration_6` no-op early returns + regression test for the empty-`episodes` guard + autouse `_reset_metrics_state` fixture for `TestMetricEmission`) ✅ merged as [#297](https://github.com/mkhomutov/Persatrix/pull/297). Slice 4 (PR-2 review #6 + #7 + #9 + #10 + #11 — tighten `_store_event_episode` exception-handler comment, replace silent `or interaction` fallback with an explicit invariant guard, telemetry probe + full single-turn `EventType` matrix + unknown-event-fallback coverage in the parity suite, and move `AgentAction` / `AgentEvent` under `TYPE_CHECKING`) ✅ merged as [#298](https://github.com/mkhomutov/Persatrix/pull/298). Slice 5 (PR-3 review #13 + #14 + #16 + #19 — wire the persona's `Clock` through to `InteractionTracker`, add the end-to-end cross-scope idle-flush test via `on_event`, lift the flush loop out of the outer `try/except` so failure warnings name the failed scope's identity instead of the in-flight `event_type`, and fold the `_coerce_event_timeout` + `<= 0` re-check into a single `min_value=` call) ✅ merged as [#299](https://github.com/mkhomutov/Persatrix/pull/299). Slice 6 (PR-3 review #12 + #15 + #17 + #18 — inline MaxTurns cap enforcement in `add_turn` so close fires on the cap-th turn rather than the next event, mirror PR-2's failure-swallow test on the multi-turn close path, parametrised scope-routing coverage for `MENTION` aggregation / two concurrent open scopes / `channel_id` vs `sender_id` precedence / `scope=None` fallback, and the cosmetic `payload: dict[str, Any]` type-drift fix) ✅ merged as [#300](https://github.com/mkhomutov/Persatrix/pull/300). Slice 7 (PR-4 deferred review #25 — tighten `_llm_client` from `LLMClient | None` to `LLMClient` on `_ActionLoopMixin` / `_EpisodeRoutingMixin`, remove the dead silent-drop branches in `_on_event_inner` and `_persist_closed_interaction`, and migrate `test_no_llm_client` off the `agent._llm_client = None` seam to a structural annotation contract test) opens next against this branch. PR-2 review #8 was already discharged by PR 3's `interaction_idle_timeout_sec` plumbing. Splitting keeps each diff focused and well under the 500-line review window.

#### Slice 1 (this PR) — PR 4 review findings #20, #21, #22, #23, #24, #26, #27, #28, #29, #30

| # | Finding | Disposition | Code site |
|---|---------|-------------|-----------|
| 20 | Phase 2 ↔ janitor write race (unscoped UPDATE + double-tick failure counter). | ✅ Fixed inline. UPDATE scoped to `WHERE summary = SUMMARY_PENDING_TEXT`; on `False` return, `record_closed_interaction` and `_tick_auto_reflect_counter` are skipped. | `agents/memory/episodic_queries.py::update_episode_summary`, `agents/persona_runtime/summarize_close.py::finalize_closed_interaction` |
| 21 | `assert interaction.interaction_id is not None` stripped under `python -O`. | ✅ Fixed inline. Replaced with explicit `if … is None: logger.warning(...); return` guard. | `agents/persona_runtime/summarize_close.py::finalize_closed_interaction` |
| 22 | Unreachable empty-summary `ValueError` raise. | ✅ Fixed inline. Validation removed; single-writer invariant documented in the docstring. | `agents/memory/episodic_queries.py::update_episode_summary` |
| 23 | `drain_pending_summaries` snapshot semantics depend on `_lock`. | ✅ Fixed inline. Lock-dependency comment added at the drain call site. | `agents/persona_runtime/state_persistence.py::drain_pending_summaries` |
| 24 | No `agent.interactions.janitor.failed` counter. | ✅ Fixed inline. Counter registered in `Instruments`; incremented in `maybe_run_janitor` exception handler. | `agents/observability/metrics.py`, `agents/persona_runtime/summarize_close.py::maybe_run_janitor` |
| 26 | No regression test for the Phase 2 ↔ janitor race (#20). | ✅ Added. `TestPhase2JanitorRace::test_janitor_wins_against_late_phase2`. | `tests/integration/test_summarize_on_close_phases.py` |
| 27 | No test pinning `close_memory`-without-explicit-drain shutdown ordering. | ✅ Added. `TestCloseMemoryDrainsImplicitly::test_close_memory_finalises_summary_without_explicit_drain`. | `tests/integration/test_summarize_on_close_phases.py` |
| 28 | No test pinning `update_episode_summary`'s `agent_id` scoping. | ✅ Added. `TestUpdateEpisodeSummaryAgentScoping::test_update_does_not_touch_other_agents_row`. | `tests/unit/python/test_episodic_memory_pending_filter.py` |
| 29 | Janitor cooldown exercised only indirectly. | ✅ Added. `TestMaybeRunJanitorCooldown::test_two_calls_within_interval_runs_cleanup_once`. | `tests/unit/python/test_summarize_close_helpers.py` |
| 30 | `await asyncio.sleep(0)` race in `test_pending_sentinel_visible_before_drain`. | ✅ Fixed inline. Replaced with `await gated.started.wait()`; `make_gated_summary_client` helper sets the event from the mock provider's first await. | `tests/integration/test_summarize_on_close_phases.py`, `tests/integration/_summarize_close_helpers.py` |
| 25 | `_persist_closed_interaction` silently drops the close path when `_llm_client is None`. | ⏭ **Deferred to slice 7.** Tightening at construction time means flipping `_StatePersistenceMixin._llm_client` from `LLMClient \| None` to `LLMClient` and migrating `tests/unit/python/test_llm_persona_agent.py::test_no_llm_client` off the `agent._llm_client = None` seam. The mixin file is at the 500-line cap, so the change is safer in its own slice where it can land alongside the test seam migration. Now lives on `_EpisodeRoutingMixin` after slice 4's split — see slice 7 below for disposition. | `agents/persona_runtime/episode_routing.py::_persist_closed_interaction` |

#### Slice 2 — PR 1 review findings #2 + #3 (typed close reason + table-driven dispatch)

| # | Finding | Disposition | Code site |
|---|---------|-------------|-----------|
| 2 | `InteractionTracker.close(reason: str)` accepts arbitrary strings; typo silently bypasses the per-reason counter dispatch. | ✅ Fixed inline. Added `CloseReason = Literal["structural", "idle_gap", "max_turns", "topic_shift", "shutdown"]` next to the `REASON_*` constants, tightened `close()`'s `reason` kwarg to `CloseReason`, narrowed the `BoundaryDetector.evaluate` Protocol to a tagged union (`tuple[Literal[True], CloseReason] \| tuple[Literal[False], Literal[""]]`), and re-typed each `REASON_*` constant from `str` to its `Literal[...]` value. Typo at any call site is now an `arg-type` mypy error. | `agents/memory/boundary_detectors.py`, `agents/memory/interactions.py` |
| 3 | `_emit_closed()` per-reason dispatch is hand-coded `if/elif`; silently misses subtotal counters when a new reason lands. | ✅ Fixed inline. Replaced the `if/elif` chain with `_REASON_COUNTER_ATTR: dict[CloseReason, str]` mapping each reason to its `_Instruments` attribute name, then `getattr(inst, attr).add(1, attrs)`. Surfaced (and fixed) the long-standing gap flagged in `MaxTurnsDetector`'s docstring: registered `agent.interactions.closed.by_max_turns` / `by_topic_shift` / `by_shutdown` (the breakout that the docstring promised but PR 4 never landed). | `agents/memory/interactions.py::_emit_closed`, `agents/observability/metrics.py::_Instruments` |

Tests added in `tests/unit/python/test_interaction_tracker.py::TestMetricEmission::test_close_emits_per_reason_subtotal` — parametrised over all five `REASON_*` values, asserts both the generic `agent.interactions.closed` counter and the per-reason `by_<reason>` subtotal increment together. The three new-counter cases (`max_turns`, `topic_shift`, `shutdown`) red before the dispatch-table refactor; green after.

#### Slice 3 — PR 1 review findings #1 + #4 + #5 (migration no-op cleanup + autouse metrics fixture)

| # | Finding | Disposition | Code site |
|---|---------|-------------|-----------|
| 1 | `_apply_migration_5` issues `await db.commit()` on the missing-`episodes` early return — harmless but a wasted round-trip and asymmetric with `_apply_migration_4`'s tail-commit contract. | ✅ Fixed inline. Dropped the redundant `commit()` and added a parity comment matching `_apply_migration_4`'s "version-record happens after this returns" note. The same no-op pattern in `_apply_migration_6` is fixed alongside for code consistency. | `agents/memory/_migration_handlers.py::_apply_migration_5`, `_apply_migration_6` |
| 4 | No regression test for the `_apply_migration_5` empty-`episodes` guard (the partial-restore baseline where `schema_version` records v1–v4 but the `episodes` table is missing). | ✅ Added. `TestEmptyEpisodesGuard::test_handler_no_op_on_missing_episodes_table` asserts no exception, no `episodes` table created, no `idx_episodes_scope` index, and no `schema_version` write from a direct handler call. `test_umbrella_records_v5_even_on_no_op` pins the umbrella's contract that v5 is recorded as applied even when the handler short-circuited. | `tests/unit/python/test_episodic_schema_migration.py::TestEmptyEpisodesGuard` |
| 5 | `TestMetricEmission` lacks an autouse cleanup fixture; the class mutates the module-global metrics registry, leaving sibling test classes order-coupled with whatever state was last installed. | ✅ Fixed inline. Added a class-scoped autouse `_reset_metrics_state` fixture that snapshots `metrics_mod._provider` / `_instruments` before each test, zeros them so each method starts from the uninitialised baseline, then restores the snapshot via the public `metrics_mod.shutdown()` API for the active provider so SDK background threads do not leak across tests. `test_no_metric_emission_when_uninitialised` is simplified — the inline `asyncio.run(metrics_mod.shutdown())` call is dropped now that the fixture owns that contract. New `test_autouse_fixture_clears_state_before_each_test` pins the fixture's pre-test invariant. | `tests/unit/python/test_interaction_tracker.py::TestMetricEmission` |

#### Slice 4 — PR 2 review findings #6 + #7 + #9 + #10 + #11 (parity-test telemetry + invariant guard + import hygiene)

| # | Finding | Disposition | Code site |
|---|---------|-------------|-----------|
| 6 | Single `try` block in `_store_event_episode` wraps `idle_check` flush, multi-turn handling, single-turn `add_turn` / `close`, and `store_episode`, but the surrounding comment frames the handler as if it only covers the `store_episode`-after-successful-`close` race; the warning text also no longer mentions the open scope the prior comment claimed to log. | ✅ Fixed inline. Rewrote the `except` comment to enumerate all three failure surfaces (I/O failures from `_persist_closed_interaction` / `store_episode` / multi-turn handler; the common single-turn close-then-store race the operator-facing `event_type` warning targets; rare tracker-side programming errors now pinned by finding #7's explicit guard). | `agents/persona_runtime/episode_routing.py::_store_event_episode` |
| 7 | `closed = self._interaction_tracker.close(...) or interaction` silently masks an invariant violation — the `or interaction` fallback is dead under the current contract (the scope was just opened under `_lock`) but exists only to placate the type checker, so a future `close` contract change (e.g. returning `None` for already-closed scopes) would silently produce NULL interaction columns. | ✅ Fixed inline. Renamed the local to `structural_close` (so mypy reads it as a fresh binding distinct from the `for closed in idle_check()` loop above), kept the `Interaction \| None` return shape, and replaced the fallback with an explicit `if structural_close is None: raise RuntimeError(...)` guard naming the invariant. Explicit guard rather than `assert` because `assert` is stripped under `python -O` (PR 6 review #21 precedent). | `agents/persona_runtime/episode_routing.py::_store_event_episode` |
| 9 | The PR-2 single-turn parity suite was the first runtime caller of `agent.interactions.opened` / `closed.by_structural`, but no test asserted the counters increment from the runtime path — only the unit-tracker suite did. A regression that dropped the `close` call (or routed single-turn events through the legacy path) would not have been caught by the parity suite. | ✅ Added. `test_telemetry_counters_increment_on_single_turn_event` builds an `InMemoryMetricReader` per the slice-3 snapshot/restore pattern, drives a `TASK_ASSIGNED` event end-to-end, and asserts `agent.interactions.opened` + `agent.interactions.closed` + `agent.interactions.closed.by_structural` each increment once. Shared `counter_total` helper added to `_persona_parity_helpers.py` so the assertion isn't bound to the unit-test class. | `tests/integration/test_interaction_single_turn_parity_followups.py::TestSingleTurnParityFollowups::test_telemetry_counters_increment_on_single_turn_event` |
| 10 | Single-turn parity coverage is uneven — only `TICK` + `TASK_ASSIGNED` were exercised, leaving `SUB_AGENT_COMPLETED` / `APPROVAL_REQUESTED` / `APPROVAL_RESPONSE` / `AGENT_JOINED` / `AGENT_LEFT` admitted to `_SINGLE_TURN_EVENT_TYPES` without a routing test, and the unknown-event-fallback branch (warn + legacy NULL-interaction shape) had no coverage at all. | ✅ Added. `test_all_single_turn_event_types_route_through_tracker` parametrises over the six non-TICK members and asserts `scope == event_type.value` + `turn_count == 1` + `closed_at is not None` + summary-text shape for each. `test_unknown_event_type_falls_back_to_legacy_shape` empties both routing frozensets on the agent instance via `monkeypatch.setattr` and asserts the legacy NULL-interaction shape lands plus a warning naming the event type and the `_TURN_EVENT_TYPES` constants. | `tests/integration/test_interaction_single_turn_parity_followups.py` |
| 11 | `AgentAction` (and `AgentEvent`) are referenced only in `_store_event_episode` / `_handle_multi_turn_event` annotations after the routing refactor. With `from __future__ import annotations` already in effect they don't need to be resolved at import time. | ✅ Fixed inline. Moved `AgentAction` and `AgentEvent` under `if TYPE_CHECKING:` in the new `episode_routing.py`; left `EventType` (runtime membership check + enum dispatch) at module scope. | `agents/persona_runtime/episode_routing.py` |

PR-2 review #8 (`InteractionTracker` constructed with default `idle_timeout_sec`) is **already discharged** by PR 3's `interaction_idle_timeout_sec` plumbing — `agents/persona_runtime/__init__.py` now reads `memory.interaction_idle_timeout_sec` from config, validates `> 0`, and forwards to `InteractionTracker(idle_timeout_sec=…)`. No further work needed.

#### Slice 5 — PR 3 review findings #13 + #14 + #16 + #19 (clock seam + cross-scope flush attribution + helper fold)

| # | Finding | Disposition | Code site |
|---|---------|-------------|-----------|
| 16 | `_LLMPersonaAgent` constructed `InteractionTracker` without forwarding `clock=`; production locked to `time.time()` and tests had to inject via per-call `now=` overrides at every call site. | ✅ Fixed inline. Forward `self._clock.now` (the persona's `agents.clock.Clock` bound method, `() -> float`) to `InteractionTracker(idle_timeout_sec=…, clock=self._clock.now)`. A single `create_persona_agent(clock=FrozenClock(...))` injection now flows through both the prompt layer and the tracker; RFC 0021 P1's planned `Clock` alias swap collapses the two Protocols without further changes here. | `agents/persona_runtime/__init__.py::_LLMPersonaAgent.__init__` |
| 14 | The PR-3 idle-gap test exercised `idle_check(now=future)` and `_persist_closed_interaction` separately but never asserted that an event arriving in scope-B flushes a stale scope-A through `_store_event_episode` — the production hot path. | ✅ Added. `TestCrossScopeIdleFlushViaOnEvent::test_event_in_scope_b_flushes_stale_scope_a` opens scope A under a `FrozenClock`, advances past the idle window via `clock.advance(...)` (no per-call `now=` plumbing thanks to #16), fires a `CHANNEL_MESSAGE` in scope B, and asserts scope A persisted with `REASON_IDLE_GAP` while scope B opened independently. | `tests/integration/test_interaction_multi_turn_followups.py` |
| 13 | The cross-scope idle-flush loop (`for closed in self._interaction_tracker.idle_check(): await self._persist_closed_interaction(closed)`) sat inside the outer `try/except` of `_store_event_episode`. If `_persist_closed_interaction` raised past its own inner try (`asyncio.CancelledError`, programming error in ctx-construction), the outer handler logged `event_type=<current event>` — misattributing the failure to the in-flight event rather than the stale scope that owned it. The same nesting also let a flush failure swallow the current event's processing entirely. | ✅ Fixed inline. Lifted the flush loop out of the outer `try/except` and wrapped each iteration in its own scope-aware `try/except` that logs `Failed to flush idle interaction for agent %s (scope=%s, interaction_id=%s)`. The outer block now guards only the *current* event's routing path; the `event_type` field in its warning text is now always accurate. Two regression tests pin both contracts: `test_flush_failure_warning_names_failed_scope_not_current_event` (warning attribution) and `test_flush_failure_does_not_block_current_event` (independence — scope B's first turn still opens its interaction even when scope A's flush raised). | `agents/persona_runtime/episode_routing.py::_store_event_episode`, `tests/integration/test_interaction_multi_turn_followups.py` |
| 19 | `_coerce_event_timeout(...)` followed by an inline `if idle_timeout_sec <= 0: logger.warning(...); idle_timeout_sec = 600.0` re-check at the call site — the two-step "coerce then validate" pair was an anti-pattern compared to a single helper call. | ✅ Fixed inline. Added `min_value: float \| None = None` and `setting_name: str = "event_timeout"` kwargs to `_coerce_event_timeout`; the helper logs and falls back to *default* when the coerced value is `<= min_value`. The `interaction_idle_timeout_sec` call site now passes `min_value=0.0, setting_name="interaction_idle_timeout_sec"` and drops the inline re-check. The re-tightened helper also frees the byte budget needed to fit the slice's other inline comments under the 500-line file-size cap. | `agents/persona_runtime/__init__.py::_coerce_event_timeout` |

PR-3 review #12 (`MaxTurnsDetector` enforced one event late — the cap fires only via `idle_check` at the top of the next event rather than inline in `add_turn`), #15 (mirror PR-2's failure-swallow test for `_persist_closed_interaction` on the multi-turn close path), #17 (parametrised expansion: `MENTION` aggregation, two concurrent open scopes, `channel_id` vs. `sender_id` precedence, scope=`None` fallback), and #18 (`payload: dict[str, object]` vs. `ctx: dict[str, Any]` type drift) land in slice 6 (below).

**File-size split (mechanical, this slice).** `_store_event_episode` + the multi-turn aggregation helpers (`_scope_for_multi_turn_event`, `_is_session_end_event`, `_handle_multi_turn_event`) + the close-path orchestrator (`_persist_closed_interaction`, `drain_pending_summaries`, `_tick_auto_reflect_counter`) + the janitor entry point (`cleanup_closing_interactions`) moved out of `agents/persona_runtime/state_persistence.py` into a new `agents/persona_runtime/episode_routing.py` (`_EpisodeRoutingMixin`). Both files now sit under the 500-line cap enforced by `scripts/checks/file_size.py --strict`. `_LLMPersonaAgent` gains the new mixin alongside `_StatePersistenceMixin`; `close_memory` still calls `drain_pending_summaries` (now provided by `_EpisodeRoutingMixin`) under `self._lock`. The frozensets `_MULTI_TURN_EVENT_TYPES` / `_SINGLE_TURN_EVENT_TYPES` move with the methods that read them; `tests/unit/python/test_channel_message_runtime.py` was updated to import `_EpisodeRoutingMixin` from the new home. Mirror split on the test side: PR-2 review #9 + #10 follow-ups land in `tests/integration/test_interaction_single_turn_parity_followups.py`, with shared persona-config / mock-LLM / episode-probe / counter-probe helpers extracted to `tests/integration/_persona_parity_helpers.py`. No behaviour change.

#### Slice 6 — PR 3 review findings #12 + #15 + #17 + #18 (inline cap + close-path failure swallow + scope-routing coverage + type drift)

| # | Finding | Disposition | Code site |
|---|---------|-------------|-----------|
| 12 | `MaxTurnsDetector` enforced one event late — the cap fired only via `idle_check` at the top of the next event, so an interaction whose `turn_count` reached the cap stayed open until *another* event arrived. A structural close in between mislabelled the closure as `REASON_STRUCTURAL` and surfaced the RFC 0020 §Security amplification window. | ✅ Fixed inline. `InteractionTracker` caches the cap from whichever `MaxTurnsDetector` is in the chain (or `None` if absent) at construction time. `add_turn` evaluates the cap inline after appending; on overflow it calls `self.close(scope, reason=REASON_MAX_TURNS, now=ts)` and returns the (now-closed) interaction so the caller can route it straight to `_persist_closed_interaction`. `_handle_multi_turn_event` checks `interaction.is_open` after `add_turn` and persists immediately when the cap fired, then short-circuits before the session-end branch. Sourcing the cap from the chain (rather than a separate constructor kwarg) keeps the cap-config knob in one place; a custom chain without `MaxTurnsDetector` correctly sees no inline cap. | `agents/memory/interactions.py::InteractionTracker.__init__` (cap caching), `InteractionTracker.add_turn` (inline check), `agents/persona_runtime/episode_routing.py::_handle_multi_turn_event` (auto-close detection) |
| 15 | The multi-turn close path runs `store_episode` from inside `_persist_closed_interaction`'s own inner `try/except`, but no test asserted the same swallow-and-log contract held there as on the single-turn parity path. A regression that bubbled the exception out of `_handle_multi_turn_event` would have crashed the event loop instead of being log-and-continue. | ✅ Added. `TestMultiTurnCloseFailureIsSwallowedAndLogged::test_session_end_persist_failure_is_logged_and_state_consistent` opens a multi-turn interaction, patches `store_episode` to raise, fires a session-end event, and asserts (1) tracker state is consistent (scope popped after close, regardless of persist failure), (2) the warning text names the failed scope, (3) a subsequent event opens a fresh interaction in the same scope. | `tests/integration/test_interaction_multi_turn_cap_failure.py::TestMultiTurnCloseFailureIsSwallowedAndLogged` |
| 17 | Scope-routing coverage was uneven — `MENTION` aggregation was asserted only at the parity level (no episode persisted yet); two concurrent open scopes interleaving on the same agent had no isolation regression test; `channel_id` vs. `sender_id` precedence and `thread_id` precedence over both were implicit; the `scope=None` fallback (no channel_id, no sender_id) had no test pinning the legacy NULL-interaction shape + warning. | ✅ Added. New `tests/integration/test_interaction_multi_turn_scoping.py` carries four classes: `TestMentionAggregation` (collapse + session-end persistence parity with `CHANNEL_MESSAGE`), `TestConcurrentOpenScopesIsolation` (interleaved A/B turns produce two independent interactions, closing one leaves the other untouched), `TestChannelIdSenderIdPrecedence` (`channel_type=group` + `sender_id` routes to the group scope; `thread_id` wins over both), `TestUnderPopulatedEventFallback` (no channel_id and no sender_id → legacy NULL-interaction row + warning naming the event type). | `tests/integration/test_interaction_multi_turn_scoping.py` |
| 18 | `payload: dict[str, object]` in `_handle_multi_turn_event` drifted from the surrounding `ctx: dict[str, Any]` annotation; both end up in the same persisted JSON. | ✅ Fixed inline. Tightened `payload` to `dict[str, Any]` to match `ctx`. Cosmetic; no behaviour change. | `agents/persona_runtime/episode_routing.py::_handle_multi_turn_event` |

Tests added in `tests/unit/python/test_interaction_tracker.py::TestMaxTurnsInlineCap` cover the tracker side of #12 (five cases: cap fires inline, below-cap stay open, subsequent turn opens fresh interaction, max-turns subtotal counter increments inline, no inline cap when `MaxTurnsDetector` is absent from the chain). The integration counterpart `TestMaxTurnsCapMultiTurnPath::test_cap_th_event_persists_with_max_turns_reason` runs the persona end-to-end with `_max_turns = 3` and asserts exactly one episode is persisted with `close_reason == REASON_MAX_TURNS` after the third multi-turn event — no fourth event needed.

**File-size split (mechanical, this slice).** Slice 5's `tests/integration/test_interaction_multi_turn_followups.py` already housed the multi-turn helpers (persona config, mock LLM client, clock-aware agent factory, episode probe). Slice 6 extracts those helpers to `tests/integration/_interaction_multi_turn_helpers.py` so three test files can share them: the slice-5 `test_interaction_multi_turn_followups.py` (idle-flush / clock-seam / flush-failure attribution), the new `test_interaction_multi_turn_cap_failure.py` (slice-6 #12 + #15), and the new `test_interaction_multi_turn_scoping.py` (slice-6 #17). All three sit under the 500-line cap; the followups file shrinks from 565 lines (over cap after #12 + #15 were added inline) to 337 lines once the helpers move out. No behaviour change.

#### Slice 7 — PR 4 deferred review #25 (`_llm_client` type tightening + dead-branch removal)

| # | Finding | Disposition | Code site |
|---|---------|-------------|-----------|
| 25 | `_persist_closed_interaction` silently drops the close path when `_llm_client is None`, and `_on_event_inner` returns `"LLM client not configured"` on the same `None` path. Both branches are dead in production — `_LLMPersonaAgent.__init__` requires a non-None `llm_client` kwarg — but the `LLMClient \| None` annotation on the persona-runtime mixins kept them reachable via the `agent._llm_client = None` test seam. | ✅ Fixed inline. Tightened `_ActionLoopMixin._llm_client` and `_EpisodeRoutingMixin._llm_client` to `LLMClient` (no `\| None`); removed the `if self._llm_client is None: return [...]` early return in `_on_event_inner` and the `or self._llm_client is None` clause in `_persist_closed_interaction`. Added a matching `_llm_client: LLMClient` re-declaration on `_LLMPersonaAgent` itself, which silences the MRO conflict against `BaseAgent._llm_client` (inferred `LLMClient \| None` — stays loose for `TaskAgent`'s no-llm-client bootstrap path) without needing a `# type: ignore` under the project's current non-strict mypy config. The narrowing is sound at the persona-runtime layer because `__init__` overwrites `self._llm_client` with a real client immediately after `super().__init__`. **PR-6 review #1 follow-up:** the slice originally added `# type: ignore[misc]` on each of the three annotation sites as defensive scaffolding, but `mypy --warn-unused-ignores agents/` flagged all three as unused (the mixin annotations don't trigger the conflict in isolation, and the `_LLMPersonaAgent` re-declaration alone is enough at the inheritance site). The ignores were dropped; the comments above each annotation now explain the actual mypy mechanics so a future strict-mode push has the right rationale to add ignores back if needed. | `agents/persona_runtime/action_loop.py::_ActionLoopMixin._on_event_inner`, `agents/persona_runtime/episode_routing.py::_EpisodeRoutingMixin._persist_closed_interaction`, `agents/persona_runtime/__init__.py::_LLMPersonaAgent` |

Tests:
- New: `tests/unit/python/test_llm_persona_agent.py::TestLLMPersonaAgent::test_persona_runtime_mixins_require_non_none_llm_client` — structural annotation contract that asserts both mixins declare `_llm_client: LLMClient` (no `| None`). Reds before the tightening; greens after. Pins the contract so a future refactor that re-widens to `| None` is caught immediately rather than re-opening the silent-drop surface. **PR-6 review #2 follow-up:** the assertion accepts both the PEP 563 source-text form (current state, since both mixin modules carry `from __future__ import annotations`) and the PEP 649 evaluated class-object form (future state if the future-import is dropped) so a Python-default flip does not silently break the check. The comparison reads `cls.__annotations__["_llm_client"]` directly rather than calling `typing.get_type_hints(cls)`: the mixins also annotate attributes whose types are imported only under `if TYPE_CHECKING:` (e.g. `MemoryNamespace` on `_EpisodeRoutingMixin`), and `get_type_hints` evaluates *all* annotations on the class, raising `NameError` for any TYPE_CHECKING-only name absent from the runtime namespace.
- Removed: `tests/unit/python/test_llm_persona_agent.py::TestLLMPersonaAgent::test_no_llm_client` — was reachable only via the `agent._llm_client = None` seam, which is gone alongside the dead branches it covered. Replaced by the annotation contract test above (the "migrate off the seam" half of the deferred slice).

`BaseAgent._llm_client` and the `TaskAgent` `test_no_llm_client_returns_failed` tests in `tests/unit/python/test_agents.py` and `tests/unit/python/test_task_agent.py` are deliberately untouched — task-agent bootstrap is a different surface that legitimately runs with `llm_client=None` (the `BaseAgent.handle()` path returns a `TaskStatus.FAILED` `TaskOutput`, not a silent drop).

Apply review findings from PRs 1–5 (the "From PR N review" pattern from [RFC 0017 PR plan](0017-pr-plan.md#status-by-finding-pr-6-implementation)). Out-of-scope items downgrade to tracked issues with rationale.

#### Scope

Review findings, grouped by source PR. Per [.github/copilot-instructions.md](../../.github/copilot-instructions.md) ("PR review reports are local-only artifacts"), each entry paraphrases the finding and **must not** reference or link any local PR review report.

##### From PR 1 review

1. **`_apply_migration_5` issues `await db.commit()` on the missing-`episodes` early return** (`agents/memory/episodic.py`).
   When the legacy DB has no `episodes` table, the v5 migration logs and returns — but still calls
   `await db.commit()` on the empty transaction. Harmless but a wasted round-trip and asymmetric with
   `_apply_migration_4`'s early-return shape. Drop the `commit()` on the no-op path and add a brief
   comment matching `_apply_migration_4`'s "version-record happens after this returns" note for parity.

2. **`InteractionTracker.close(reason: str)` accepts arbitrary strings** (`agents/memory/interactions.py`).
   A typo at any caller silently bypasses the per-reason counter dispatch in `_emit_closed()` and only
   the generic `interactions.closed` counter increments. Tighten the signature to
   `Literal["structural", "idle_gap", "topic_shift", "shutdown"]` (or a `CloseReason` enum / `Final`
   constants set) so mypy catches the typo at the call site. Pair this with finding #3.

3. **`_emit_closed()` per-reason dispatch is hand-coded `if/elif`** (`agents/memory/interactions.py`).
   Won't scale cleanly when PR 4 adds `interactions.closed.by_shutdown` and PR 5 (or post-v0.3.0
   Phase 4) adds `interactions.closed.by_topic_shift`. Refactor to a `dict[CloseReason, str]`
   counter-name table iterated once. Keeps `close()` logic focused on lifecycle, not metric naming.

4. **No regression test for the `_apply_migration_5` empty-`episodes` guard**
   (`tests/unit/python/test_episodic_schema_migration.py`). The early-return branch fires only on a DB
   that has the migrations table but lacks `episodes` — an unusual but real shape during partial
   restores. Add a fixture that constructs that DB shape and asserts the migration is a no-op (no
   exception, no rows added, version row written by the outer harness as expected).

5. **`TestMetricEmission` lacks an autouse cleanup fixture** (`tests/unit/python/test_interaction_tracker.py`).
   The class mutates the module-global metrics registry; without an autouse `reset_metrics` fixture the
   tests are order-coupled with anything else that touches metrics in the same pytest session. Add a
   class-scoped autouse fixture that snapshots and restores the relevant counters (or the whole
   registry) around each test method.

##### From PR 2 review

6. **Exception-handler comment in `_store_event_episode` is misleading** (`agents/persona_runtime/state_persistence.py`).
   The single `try` block wraps both `InteractionTracker.close` and `EpisodicMemory.store_episode`,
   but the surrounding comment reads as if the open scope is always cleaned before the `except` runs.
   Either split the `try` so only `store_episode` is guarded (and `close` runs unconditionally), or
   tighten the comment to state that the handler covers `store_episode` failure after a successful
   `close`. Pair with finding #7.

7. **`closed = self._interaction_tracker.close(...) or interaction` masks future contract changes**
   (`agents/persona_runtime/state_persistence.py`). The `or interaction` fallback is currently
   unreachable (the scope was just opened under the agent's `asyncio.Lock`), so it exists only to
   placate the type checker. If `InteractionTracker.close()`'s return contract ever changes (PR 4
   may return `None` for already-closed scopes), this fallback will silently mask the bug. Replace
   with an `assert closed is not None` (or `typing.cast`) so the invariant is testable and breakage
   surfaces at runtime.

8. **`InteractionTracker()` constructed with default `idle_timeout_sec`**
   (`agents/persona_runtime/__init__.py`). PR 2 instantiates the tracker with the library default;
   no `idle_timeout_sec` is plumbed from `optimization.yaml` / `agents.yaml`. Acceptable for PR 2
   (the idle-gap janitor lands in PR 4) but the silent default is now reachable from production
   code. Plumb the config knob in PR 4 alongside the janitor wiring; this PR 6 entry is a tracking
   note so the default doesn't get inherited indefinitely.

9. **Parity test lacks a telemetry probe**
   (`tests/integration/test_interaction_single_turn_parity.py`). PR 2 is the first runtime site that
   fires `interactions.opened` and `interactions.closed.by_structural`, but no test asserts the
   counters increment. Add a `try_get_instruments`-shaped probe (or use the same metrics-snapshot
   fixture introduced by PR 6 finding #5) to lock the contract.

10. **Single-turn coverage is uneven across `EventType` members**
    (`tests/integration/test_interaction_single_turn_parity.py`). The parity test exercises `TICK`
    and `TASK_ASSIGNED` but not the other five single-turn members (`SUB_AGENT_COMPLETED`,
    `APPROVAL_REQUESTED`, `APPROVAL_RESPONSE`, `AGENT_JOINED`, `AGENT_LEFT`). Add a single
    `pytest.mark.parametrize` case over the full set asserting `scope == event_type.value` and
    `turn_count == 1`. Also add a test for the unknown-event fallback branch (monkey-patch a
    synthetic `EventType` or spoof the membership check) to prove the warn-and-fallback path.

11. **`AgentAction` import is type-only after the routing refactor**
    (`agents/persona_runtime/state_persistence.py`). With `from __future__ import annotations`
    already in effect, `AgentAction` is referenced only inside `_store_event_episode`'s annotation.
    Move the import under `if TYPE_CHECKING:` to shave one import cycle. Negligible impact;
    bundle with whichever PR 6 cleanup touches the file.

##### From PR 3 review

12. **`MaxTurnsDetector` enforced one event late** (`agents/memory/boundary_detectors.py`,
    `agents/memory/interactions.py`). PR 3 wires `MaxTurnsDetector` into `default_detectors()`,
    but the cap fires only via `idle_check`, which the runtime calls at the *top* of the next
    event — *before* the current event's `add_turn`. Turn `max_turns + 1` is admitted; the close
    fires only on the subsequent event. Tighten enforcement to `add_turn` (check the cap inline
    after appending; close-and-reopen on overflow) so the runtime invariant matches the documented
    "hard cap on turns per interaction". Closes the off-by-one window on the resource-amplification
    surface RFC 0020 §Security names.

13. **Cross-scope idle-flush failure logs the wrong `event_type`**
    (`agents/persona_runtime/state_persistence.py`). The `for closed in idle_check(): await
    _persist_closed_interaction(closed)` loop sits inside the outer `try/except` of
    `_store_event_episode`. If `_persist_closed_interaction` raises past its own inner
    try (e.g. `asyncio.CancelledError` or a programming error in ctx-construction), the outer
    handler logs `event_type=<current event>` — which is *not* the event that owned the failed
    flush scope. Either lift the inner try around the entire `_persist_closed_interaction` body,
    or pull the flush loop out from under the outer `except`.

14. **End-to-end test for cross-scope idle flush via `on_event`**
    (`tests/integration/test_interaction_multi_turn.py`). The PR-3 idle-gap test exercises
    `idle_check(now=future)` and `_persist_closed_interaction` separately but never asserts that
    an event arriving in scope-B flushes a stale scope-A through `_store_event_episode`. The PR
    description markets this as "the production hot path". Add a test that opens scope A, advances
    time past the idle window (inject a fake clock via the new seam — pairs with finding #16),
    fires an event in scope B, and asserts A persisted with `REASON_IDLE_GAP` and B opened
    independently.

15. **Mirror PR-2's failure-swallow test for `_persist_closed_interaction`**
    (`tests/integration/test_interaction_multi_turn.py`). PR 2 has
    `test_store_episode_failure_is_swallowed_and_logged` for `_store_event_episode`; PR 3's
    multi-turn close path has the same inner `try/except` around `store_episode` but no test
    pins the contract. Patch `agent._episodic_memory.store_episode = _boom` inside an idle-gap
    or session-end test; assert the warning is emitted and tracker state is consistent.

16. **Wire the `Clock` seam through to `_LLMPersonaAgent`** (`agents/persona_runtime/__init__.py`,
    `agents/memory/interactions.py`). Today `_LLMPersonaAgent` constructs `InteractionTracker`
    without forwarding a `clock=`; production code is locked to `time.time()` and tests inject via
    per-call `now=` overrides. Accept `clock=None` on the agent constructor and forward to the
    tracker so tests can construct an agent with a fake clock instead of patching per call. Reduces
    the eventual RFC 0021 P1 swap diff to one site.

17. **Add coverage for `MENTION` aggregation, two concurrent open scopes, `channel_id` vs.
    `sender_id` precedence, and scope=`None` fallback** (`tests/integration/test_interaction_multi_turn.py`).
    Multi-turn aggregation is asserted only for `MESSAGE_RECEIVED`; `MENTION` is covered only at
    the "no episode persisted yet" parity level. No test pins that DM-A and thread-B accumulating
    in parallel on the same agent stay independent until each closes; no test pins the
    `channel_id`-precedence so PR 5's reshuffle will be hard to read; no test fires a
    `MESSAGE_RECEIVED` with neither `channel_id` nor `sender_id` to assert the legacy NULL-interaction
    fallback + warning. Five lines each; bundle as one parametrised expansion.

18. **Type drift between `payload: dict[str, object]` and `ctx: dict[str, Any]`**
    (`agents/persona_runtime/state_persistence.py`, `_handle_multi_turn_event`). Both end up in
    the same persisted JSON. Pick `dict[str, Any]` to match the rest of the file.

19. **Fold `_coerce_event_timeout` + `<= 0` reject into one helper**
    (`agents/persona_runtime/__init__.py`). The two-step "coerce then validate `<= 0`" pair
    around `interaction_idle_timeout_sec` could be one call by adding a `min_value=` kwarg to
    `_coerce_event_timeout` (or a small `_coerce_positive_float` helper). Cosmetic.

##### From PR 4 review (round 2)

20. **Phase 2 ↔ janitor write race inflates `agent.interactions.summary.failed` and lets a
    late LLM overwrite a janitor-finalised row** (`agents/memory/episodic_queries.py`,
    `agents/persona_runtime/summarize_close.py`). The Phase 2 UPDATE in
    `update_episode_summary()` is unscoped — if the janitor sweeps the row first and writes
    `SUMMARY_UNAVAILABLE_TEXT`, a late-successful LLM completion will still overwrite it
    with the LLM text, and a late-failing Phase 2 will increment the failure counter a
    second time for the same interaction (`reason="janitor"` + `reason="timeout"`). Scope
    the UPDATE with `WHERE summary = SUMMARY_PENDING_TEXT`; on the returned-`False` path,
    skip `record_closed_interaction` and `_tick_auto_reflect_counter` so the janitor's
    decision is final. Document the contract in §C and in
    `finalize_closed_interaction`'s docstring.

21. **`finalize_closed_interaction` uses `assert` for a runtime invariant**
    (`agents/persona_runtime/summarize_close.py`). `assert interaction.interaction_id is
    not None` is stripped under `python -O`. Replace with an explicit
    `if interaction.interaction_id is None: logger.warning(...); return` guard so a future
    Phase-1 reorder cannot let `None` through silently.

22. **`update_episode_summary` raises `ValueError` on empty summary that the caller
    guarantees is non-empty** (`agents/memory/episodic_queries.py`). The validation is
    unreachable today (`summarize_closed_interaction` always returns either the LLM text
    or `SUMMARY_UNAVAILABLE_TEXT`), but the contract is undocumented at the call site and
    the exception escapes Phase 2's inner `try`. Either drop the validation (single-writer
    invariant) or document it as a precondition in the docstring.

23. **`drain_pending_summaries` snapshot semantics depend on `_lock`**
    (`agents/persona_runtime/state_persistence.py`,
    `agents/persona_runtime/summarize_close.py`). `drain_pending_summary_tasks()` snapshots
    the pending set with `list(pending)` so tasks spawned during drain are not awaited.
    `close_memory()` runs the drain under `self._lock`, which is the property the snapshot
    relies on. Add a one-line comment at the drain call site noting the lock dependency so
    a future refactor that moves the drain outside the lock does not silently lose
    late-arriving tasks.

24. **`maybe_run_janitor` swallows sweep failures and advances the cooldown by a full
    interval** (`agents/persona_runtime/summarize_close.py`). On a transient DB error the
    next sweep is delayed another `JANITOR_INTERVAL_SEC` (5 min), so during a persistent
    outage stuck rows accumulate quietly. Add an `agent.interactions.janitor.failed`
    counter, or shorten the cooldown on the failure path, so operators can SLO-alert on
    repeated failures.

25. **`_persist_closed_interaction` silently drops the close path when
    `_llm_client is None`** (`agents/persona_runtime/state_persistence.py`). Production
    agents always construct with an LLM client, so the early return exists only for
    test bootstrap. Tighten at construction time (assert non-None or fold the bootstrap
    path into a dedicated test seam) so the silent-drop branch is dead in production.

26. **No regression test for the Phase 2 ↔ janitor race** (finding #20)
    (`tests/integration/test_summarize_on_close_phases.py`). Extend `TestTwoPhaseWrite`
    with a case that calls `cleanup_closing_interactions(grace_sec=0.0)` between Phase 1
    and `gate.set()`, asserts the row is `SUMMARY_UNAVAILABLE_TEXT`, then releases the
    gate, drains, and asserts the row is *still* `SUMMARY_UNAVAILABLE_TEXT` (locks finding
    #20's fix in place).

27. **No test pins `close_memory`-without-explicit-drain shutdown ordering** (PR 4 round-2
    coverage gap). Add a test that triggers a close, then immediately calls `close_memory`
    with no explicit `drain_pending_summaries`, and asserts the final summary is the LLM
    text (not the sentinel). Locks in the contract that `close_memory` drains on the way
    out.

28. **No test pins `update_episode_summary`'s `agent_id` scoping**
    (`tests/unit/python/test_episodic_memory.py`). Add a two-agent test that updates agent
    A's row and asserts agent B's pending row is untouched. Closes the loop on the
    agent-scoped UPDATE contract.

29. **`maybe_run_janitor` cooldown is exercised only indirectly** (`tests/unit/python/`).
    `TestJanitorBackfillsPendingSummaries` calls `cleanup_closing_interactions` directly,
    not via `on_tick`. Add a unit test that invokes `on_tick` twice within
    `JANITOR_INTERVAL_SEC` and asserts the cleanup runs only once. Pins the cooldown
    semantics so a future refactor that drops the monotonic guard surfaces immediately.

30. **`test_pending_sentinel_visible_before_drain` relies on `await asyncio.sleep(0)` to
    observe Phase-1 mid-flight** (`tests/integration/test_summarize_on_close_phases.py`).
    Works today, fragile under event-loop scheduling changes. Replace with an
    `asyncio.Event` set from the mock provider's first await so the test deterministically
    waits for the Phase-2 task to park on `gate.wait()`.

---

### PR 7: `feature/v030-rfc0020-close` — RFC Close

**Depends on**: PR 6.
**Estimated size**: ~50–100 lines (status updates only).

| File | Change |
|------|--------|
| `docs/rfcs/0020-interaction-lifecycle.md` | Status → `✅ Implemented` |
| `ROADMAP.md` | RFC 0020 status → `✅ Implemented`; merged-PR rows for PRs 6 and 7. |
| `docs/rfcs/0020-pr-plan.md` | All checklists complete; merged-PR numbers and dates filled in for every PR. |
| `docs/v0.3.0-plan.md` | Master Progress Overview row 2 → ✅ Merged. |

CHANGELOG.md is **deferred to v0.3.0 release prep** (Phase 4 PR 3) — no per-RFC changelog edit.

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| PR 1 schema disagrees with RFC 0008 §D once implementation begins | Cross-link the RFC 0008 plan PR 2 (compression contract owner); surface the contract gap during Phase 1 PR review, not at PR 4 land time. Migration is additive — patches do not need a downtime window. |
| PR 4 trust-bootstrap recalibration causes silent persona-behavior drift | Migration Notes appendix lists every config knob with before/after values; release notes call out the recalibration explicitly. |
| PR 5 joint delivery with RFC 0011 P3 slips | Documented divergence path (per-event episodic writes, backfilled in v0.3.x). Both PRs reference each other's PR number to make slippage visible. |
| Summarization LLM failure rate higher than expected | `interactions.summary.failed` counter + fallback summary text. Operators can monitor; v0.3.x can swap the model selection without touching schema. |

---

## ROADMAP Hygiene

- **PR 1 opens** → flip [ROADMAP.md](../../ROADMAP.md) RFC 0020 row to `🚧 Implementing`; flip Master Progress Overview row 2 to 🔄.
- **Each PR merges** → tick the corresponding checklist line in this plan; update the merged-PR table in ROADMAP.
- **PR 7 merges** → flip RFC 0020 row to `✅ Implemented`; flip Master Progress Overview row 2 to ✅.
