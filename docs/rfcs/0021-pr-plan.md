# RFC 0021 — PR Implementation Plan (Phase 1 — v0.3.0 scope)

**RFC**: [0021-persona-temporal-awareness.md](0021-persona-temporal-awareness.md)
**Created**: 2026-04-25
**Branch prefix**: `feature/v030-rfc0021p1-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.0-plan.md Phase 1 (combined plans PR)](../v0.3.0-plan.md#phase-1--author-the-six-rfc-pr-plans)

---

## Overview

RFC 0021 spans four phases. **Only Phase 1 lands in v0.3.0**: now-anchor in the system prompt, recency rendering on episode and relationship recall, `Clock` abstraction. Phases 2–4 (commitments, REMINDER event, duration calibration) are deferred to v0.4.0 per the [RFC's Phased Implementation Plan](0021-persona-temporal-awareness.md#phased-implementation-plan).

This plan splits Phase 1 into **3 PRs**. Each stays well under the [BRANCHING.md](../BRANCHING.md) 500-line soft cap.

> **Estimate calibration**: 1.7× factor per [RFC 0017 PR plan precedent](0017-pr-plan.md#overview).

**Prerequisite**: [RFC 0020 PR plan](0020-pr-plan.md) PR 1 merged — Phase 1 consumes the `started_at` / `closed_at` columns introduced there. No other v0.3.0 RFC dependency.

**Cross-RFC sequencing**: PR 1 of this plan can open as soon as RFC 0020 PR 1 is in review (no merge dependency). PR 2 requires RFC 0020 PR 1 merged.

---

## Resolved Open Questions

[RFC 0021 §Decision/Next Steps](0021-persona-temporal-awareness.md#decision--next-steps) requires OQ #1, #2, and #8 be resolved in this companion document before Phase 1 implementation begins. Resolutions:

| OQ | Question | Resolution | Where landed |
|----|----------|------------|--------------|
| **#1** | Today / HH:MM threshold for recency rendering | **Duration-driven buckets only in v0.3.0.** `format_relative` ships seven past-tense buckets (`just now` / `N min ago` / `N hours ago` / `yesterday` / `N days ago` / `last week` / `N weeks ago` / `N months ago` / `over a year ago`) and the symmetric future buckets. Calendar-aware alternatives (`today, HH:MM`, `last <weekday>`, `calendar-tomorrow`) are deferred to PR 2 follow-ups — RFC 0021 §D lists them as "or" forms and the duration form is sufficient for the prompt-shape contract PR 2 lands. | PR 1 ([agents/temporal/rendering.py](../../agents/temporal/rendering.py) module docstring + bucket boundaries) |
| **#2** | `reminder_horizon_sec` default | **Not in scope for Phase 1.** Reminders ship in Phase 3 (v0.4.0) with the REMINDER event. Default value to be pinned then. | Deferred to v0.4.0 — out of scope for this plan |
| **#8** | Timezone display format in the now-anchor | **Persona-local time, no TZ name in v0.3.0.** `Clock` exposes epoch seconds; the tz-aware `datetime` is computed inside the rendering layer using `persona.timezone` (default `UTC`). The now-anchor block (PR 2) shows local wall-clock + part-of-day word; explicit TZ-name disambiguation (e.g., "14:30 Tokyo") is a v0.4.0 follow-up if cross-tz operator deployments justify it. | PR 1 (`format_part_of_day` bands) + PR 2 (now-anchor format — pinned when PR 2 opens) |

---

## Dependency Graph

```
PR 1 (Clock abstraction + agents/temporal/rendering pure functions)
  ↓
PR 2 (Now-anchor in system prompt + recency rendering on episode/relationship recall)
  ↓
PR 3 (Review follow-ups + RFC close — Phase 1 scope only)
```

---

## PR Sequence

### PR 1: `feature/v030-rfc0021p1-clock-rendering` — Clock + Rendering Module

**Depends on**: Nothing. (PR 1's surface — `Clock` + the pure rendering functions in `agents/temporal/rendering.py` — does not reference `closed_at` or any RFC 0020 column. The RFC 0020 PR 1 dependency surfaces in PR 2, where the now-anchor and recency-prefix wiring actually consume the schema.)
**Estimated size**: ~250–400 lines (two new modules + exhaustive unit tests).

#### Scope

| File | Change |
|------|--------|
| `agents/clock.py` | **New** — `Clock` Protocol; `WallClock` (wraps `time.time()`); `FrozenClock` test impl. |
| `agents/temporal/__init__.py` | **New** — package marker. |
| `agents/temporal/rendering.py` | **New** — pure functions: `format_relative(then, now, tz) → str`, `format_duration(seconds) → str`, `format_part_of_day(hour) → str`. |
| `tests/unit/python/test_clock.py` | **New** — `WallClock` is monotonic-ish (smoke); `FrozenClock` advances on demand. |
| `tests/unit/python/test_temporal_rendering.py` | **New** — exhaustive matrix covering seconds / minutes / hours / days / weeks / months thresholds; future and past; DST and non-DST timezones. |

#### Key implementation details

- `Clock.now()` returns a `float` (seconds since epoch, UTC) per RFC 0021 §B — naive `datetime` is never exposed; rendering helpers convert to a tz-aware `datetime` internally via the persona's configured timezone (defaults to `UTC`).
- `format_relative` thresholds match RFC 0021 §D (verbatim) and produce strings like `"3 minutes ago"`, `"2 hours ago"`, `"yesterday"`, `"3 days ago"`, `"last month"`.
- No persona-runtime wiring in this PR — that is PR 2.

#### Tests

- Every threshold boundary in `format_relative` exercised (each ±1s).
- Future timestamps render as `"in N minutes"`.
- `FrozenClock` advance produces deterministic relative renders.
- Timezone smoke: a UTC-stored timestamp renders correctly under a non-UTC test tz.

#### PR checklist

- [x] `pytest tests/unit/python/ -v` passes
- [x] `ruff check agents/` clean
- [x] `mypy agents/` clean
- [x] No persona-runtime files touched
- [x] ROADMAP.md row for RFC 0021 → `🚧 Implementing` on this PR opening
- [x] Master Progress Overview row 3 → 🔄 In progress

---

### PR 2: `feature/v030-rfc0021p1-now-anchor-recency` — Now-Anchor + Recency Rendering

**Depends on**: PR 1 merged + RFC 0020 PR 1 merged (`started_at` / `closed_at` columns required for episode recency).
**Estimated size**: ~300–500 lines.

#### Scope

| File | Change |
|------|--------|
| `agents/persona_runtime/prompt_assembly.py` | `_build_system_prompt` gains the now-anchor block. Clock injected via the persona-runtime constructor. |
| `agents/persona_runtime/memory_context.py` | Episode recall packaging gains recency-prefix rendering (uses `closed_at` from RFC 0020 §D where available; falls back to `created_at` for legacy rows). |
| `agents/memory/relationship.py` | Relationship summary path renders `last_interaction_at` as a recency tag and computes the cadence bucket. |
| `agents/persona_types.py` | Optional `persona.timezone` config field added to persona schema; defaults to `UTC`. |
| `agents/observability/metrics.py` | New counters: `temporal.now_anchor_emitted`, `temporal.recency_rendered{source=episode\|relationship}`. |
| `tests/integration/test_temporal_prompt_shape.py` | **New** — system prompt contains now-anchor; recalled episodes carry recency prefix; relationship summary carries last-interaction recency. |

#### Key implementation details

- Now-anchor block is a small fixed-width section near the top of the system prompt, computed once per event-handler call (not per memory item).
- Recency prefixes are pre-computed in Python; the LLM never does date arithmetic.
- Token cost of the now-anchor + per-item recency prefixes is bounded — RFC 0021 §J specifies ≤ 32 tokens for the anchor and ≤ 8 tokens per recency tag. Verify in tests.

#### Tests

- System prompt under a `FrozenClock` produces a deterministic now-anchor.
- Episode recall with `closed_at` set renders relative tag; without `closed_at` (legacy row) renders `created_at`-based tag.
- Relationship summary renders `last_interaction_at` as `"3 weeks ago"` etc.
- Token budget invariant: now-anchor + recency tags add < 100 tokens to a typical prompt.

#### PR checklist

- [ ] `pytest tests/integration/ -v` passes
- [ ] Token-cost assertion green
- [ ] No regression on RFC 0017 working-memory bound
- [ ] `persona.timezone` defaults to `UTC` when absent

---

### PR 3: `feature/v030-rfc0021p1-close` — Review Follow-Ups + RFC Phase-1 Close

**Depends on**: PR 2.
**Estimated size**: ~100–250 lines (review follow-ups + status updates).

| File | Change |
|------|--------|
| `docs/rfcs/0021-persona-temporal-awareness.md` | Phase 1 status → `⚠️ Partially Implemented` (Phases 2–4 remain for v0.4.0). |
| `ROADMAP.md` | RFC 0021 row → `⚠️ Partially Implemented (Phase 1)`; merged-PR rows. |
| `docs/rfcs/0021-pr-plan.md` | Checklists complete; merged PR numbers + dates. |
| `docs/v0.3.0-plan.md` | Master Progress Overview row 3 → ✅ Merged. |
| Code | Apply review follow-ups from PRs 1–2; out-of-scope items file as v0.4.0 follow-up issues. |

CHANGELOG.md is **deferred to v0.3.0 release prep** (Phase 4 PR 3).

#### PR checklist

- [ ] All deferred review findings addressed or downgraded
- [ ] `make test` passes; `make lint` clean
- [ ] RFC 0021 status reflects partial-implementation reality

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| PR 1 lands before RFC 0020 PR 1, leaving recency rendering unable to cite `closed_at` | PR 1 is independent — only renders strings. PR 2 hard-depends on RFC 0020 PR 1 merged; `closed_at` fallback to `created_at` is documented as the legacy-row path. |
| Now-anchor token cost erodes the RFC 0017 working-memory bound | Unit test in PR 2 asserts token cost; counters surface drift in production. |
| Persona timezone misconfigured in operator deployments | Default `UTC`; explicit config validation at persona load time. |
| Phase 1 "tastes like Phase 2" — operators expect commitments because they see temporal awareness | RFC 0021 PR 3 status flip to `⚠️ Partially Implemented` makes the gap explicit. v0.3.0 release notes call out "Phases 2–4 deferred to v0.4.0." |

---

## ROADMAP Hygiene

- **PR 1 opens** → ROADMAP RFC 0021 row → `🚧 Implementing`; Master Progress Overview row 3 → 🔄.
- **Each PR merges** → tick checklist; update merged-PR table.
- **PR 3 merges** → ROADMAP RFC 0021 row → `⚠️ Partially Implemented`; Master Progress Overview row 3 → ✅.
