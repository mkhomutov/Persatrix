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

- [ ] `pytest agents/tests/ tests/unit/python/ -v` passes
- [ ] `ruff check agents/` clean
- [ ] `mypy agents/` clean
- [ ] Schema migration shipped behind the existing `EpisodicMemory.initialize()` path
- [ ] ROADMAP.md row for RFC 0020 → `🚧 Implementing` on this PR opening
- [ ] Master Progress Overview row 2 → 🔄 In progress

---

### PR 2: `feature/v030-rfc0020-single-turn-routing` — Single-Turn Routing Through Tracker

**Depends on**: PR 1.
**Estimated size**: ~300–450 lines.

#### Scope

| File | Change |
|------|--------|
| `agents/persona_runtime/__init__.py` (or `action_loop.py`) | Wire single-turn event paths (TICK, tool-only) through `InteractionTracker`. Each emits one closed interaction with `turn_count=1`. |
| `agents/dispatch.py` | Tracker hook in the dispatch loop — single-turn events open + close in one call. |
| `tests/integration/test_interaction_single_turn_parity.py` | **New** — behavioral parity vs. pre-RFC episode shape for TICK and tool-only events. |

#### Key implementation details

- TICK and tool-only paths are the easy case — start, one turn, close. Behavior parity is verifiable by comparing pre/post episode counts and summary text.
- Multi-turn aggregation (human-chat, DMs, channels) is **not** wired in this PR — that is PRs 3 + 5.

#### Tests

- TICK event with empty episodic store produces exactly one closed-interaction episode.
- Tool-only event ditto.
- Episode count after N TICKs equals N (parity vs. pre-RFC).

#### PR checklist

- [ ] `pytest tests/integration/ -v` passes
- [ ] Parity test green
- [ ] No change to working-memory token bound (RFC 0017 invariant preserved)

---

### PR 3: `feature/v030-rfc0020-multi-turn-aggregation` — Multi-Turn for Human-Chat + DM

**Depends on**: PR 2.
**Estimated size**: ~350–500 lines.

#### Scope

| File | Change |
|------|--------|
| `agents/persona_runtime/__init__.py` | Multi-turn aggregation for human-chat sessions: turns accumulate in the open interaction; close on session end or idle. |
| `agents/memory/interactions.py` | `IdleGapDetector` runtime wiring — uses `Clock` (introduced by RFC 0021 P1 if landed; else direct `time.time()` with a `Clock`-shaped seam to swap in). |
| `tests/integration/test_interaction_multi_turn.py` | **New** — ten-turn human-chat session produces exactly one open interaction, closed on session end. |

#### Key implementation details

- "Session end" = explicit RFC 0016 `chat_end` event or idle timeout (`idle_timeout` default 600s, configurable per channel).
- DM scope keying: `(local_agent_id, peer_id)` — symmetric so the agent's own outbound messages count toward the same interaction.
- If RFC 0021 P1 has landed, depend on its `Clock`; otherwise use `time.time()` and add a TODO marker for the P1 swap (one-line change, low risk).

#### Tests

- Ten turns from the same chat session collapse into one interaction.
- Idle-gap closure: clock-advance in test produces a closed interaction; subsequent turn opens a new one.
- DM scope symmetry: A→B and B→A in a DM count toward the same interaction.

#### PR checklist

- [ ] Multi-turn integration test green
- [ ] No regression on PR 2's single-turn parity test

---

### PR 4: `feature/v030-rfc0020-summarize-on-close` — Summarization Hook + Janitor + record_interaction Move

**Depends on**: PR 3, RFC 0008 PR 2 (`MemoryFacade.compress` surface; PR 2 transitively brings in PR 1's context-budget foundation).
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

- [ ] Migration Notes appendix lands with this PR
- [ ] RFC 0008 `MemoryFacade.compress` import resolves (cross-RFC dep is concrete)
- [ ] No regression on RFC 0017 token-bound contract

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

#### PR checklist

- [ ] All deferred review findings addressed or downgraded to tracked issues
- [ ] `make test` passes; `make lint` clean

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
