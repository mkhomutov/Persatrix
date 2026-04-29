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
| `agents/memory/episodic.py` | `MemoryFacade.retrieve_relevant` filters out non-`closed` rows (defense in depth). |
| `agents/memory/interactions.py` | Per-channel scoping rules (DM = pair, thread = thread, group = rolling per-channel-per-agent). |
| `tests/integration/test_channel_interaction_scoping.py` | **New** — six-participant `#planning` channel with a 15-message exchange produces one episode per agent. |

#### Key implementation details

- **Joint delivery** with [RFC 0011 PR plan §PR 5](0011-pr-plan.md#pr-sequence) — both PRs land in the same merge window. If pairing slips, RFC 0011 P3 ships per-event episodic writes and this PR backfills in v0.3.x (documented as accepted divergence in both RFCs).
- Thread archive + channel-leave events fire `StructuralCloseDetector`.

#### Tests

- Six-participant channel produces N episodes (one per agent), each summarizing that agent's view.
- Thread archive closes the open thread interaction immediately.
- Channel-leave closes the leaving agent's interaction.

#### PR checklist

- [ ] Joint with RFC 0011 PR 5 — both PRs reference each other's PR number
- [ ] Channel-scoping integration test green
- [ ] No regression on PR 4's summarization tests

---

### PR 6: `feature/v030-rfc0020-followups` — Review Follow-Ups

**Depends on**: PR 5.
**Estimated size**: ~200–400 lines.

Apply review findings from PRs 1–5 (the "From PR N review" pattern from [RFC 0017 PR plan](0017-pr-plan.md#status-by-finding-pr-6-implementation)). Out-of-scope items downgrade to tracked issues with rationale.

#### Scope

Review findings, grouped by source PR. Per [.github/copilot-instructions.md](../../.github/copilot-instructions.md) ("PR review reports are local-only artifacts"), each entry paraphrases the finding and **must not** reference or link any `docs/pr-reviews/*.md` file.

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
