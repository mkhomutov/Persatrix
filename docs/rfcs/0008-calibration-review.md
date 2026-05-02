# RFC 0008 — 30-Day Eviction Parameter Calibration Review

**Status**: 📋 Placeholder — landed in [PR 5](0008-pr-plan.md#pr-5-featurev030-rfc0008-procedural-revalidation--phase-4b-confidence-decay--revalidation); will be filled in by [PR 6](0008-pr-plan.md#pr-6-featurev030-rfc0008-close--review-follow-ups--rfc-close).
**Calibration window opens**: PR 5 merge date.
**Calibration window closes**: PR 5 merge date + 30 days.
**Gate**: this review must complete before RFC 0008 flips to `✅ Implemented` (Open Question 12 commitment).

---

## Purpose

[RFC 0008 Open Question 12](0008-agent-memory-context-optimization.md#12-memory-eviction-parameter-calibration--ship-defaults-with-mandatory-metrics-collection)
commits the project to shipping the v0.3.0 memory eviction defaults
(`episodic_cap = 1000`, `ttl_low_importance_days = 30`,
`lambda_per_day = 0.01`, `c_min = 0.1`, `stale_confidence_alert_threshold = 0.3`)
with a **mandatory** 30-day post-merge review. The review must either
validate the shipped defaults or retune them via a one-line
`config/agents.yaml` change before the RFC closes. The retune (if any)
ships in PR 6 alongside the RFC status flip.

---

## Inputs (to be filled in by PR 6)

Aggregate the following over the 30-day window from the
`orchestrator.memory.*` instruments registered in
[`internal/observability/metrics/metrics.go`](../../internal/observability/metrics/metrics.go)
(see RFC 0008 PR 5):

- `evictions_count` — per-tier (episodic|procedural) and per-reason (ttl|cap|decay) totals.
- `average_confidence_at_eviction` — distribution percentiles (p50, p90, p99).
- `average_importance_at_eviction` — distribution percentiles (p50, p90, p99).
- `memory_utilization_ratio` — per-agent fill ratio (count / `episodic_cap`); flag agents consistently > 0.95 or < 0.20.
- `oldest_surviving_entry_age_days` — distribution; flag agents with values exceeding the TTL.
- `entries_below_stale_threshold` — per-agent counts; sustained non-zero values indicate the stale window is too narrow or `lambda` is too aggressive.
- `stale_memory_injection` — counter; sustained-rate alerting baseline.

Additionally, validate the PR 2 advisory-budget translation constant
`avg_entry_tokens = 100` against the observed `episodic_entry_token_count`
distribution (PR 5 checklist item).

---

## Findings (PR 6)

> Replace this section with the actual findings before merging PR 6.

| Parameter | Shipped default | Observed range | Action | Justification |
|-----------|-----------------|----------------|--------|---------------|
| `episodic_cap` | 1000 | TBD | TBD | TBD |
| `ttl_low_importance_days` | 30 | TBD | TBD | TBD |
| `lambda_per_day` | 0.01 (~69-day half-life) | TBD | TBD | TBD |
| `c_min` | 0.1 | TBD | TBD | TBD |
| `stale_confidence_alert_threshold` | 0.3 | TBD | TBD | TBD |
| `avg_entry_tokens` | 100 | TBD | TBD | TBD |

---

## Decision (PR 6)

> Replace with one of:
>
> - **Defaults stand** — no config change required; instrumentation
>   confirmed the shipped values across the observed agent population.
> - **Retune** — one-line `config/agents.yaml` change(s) listed above;
>   schema defaults updated to match.

---

## References

- [RFC 0008 §G — Memory eviction, decay, and validation](0008-agent-memory-context-optimization.md#g-memory-eviction-decay-and-validation)
- [RFC 0008 Open Question 12 — Memory eviction parameter calibration](0008-agent-memory-context-optimization.md#12-memory-eviction-parameter-calibration--ship-defaults-with-mandatory-metrics-collection)
- [RFC 0008 PR plan PR 5 — Confidence decay + procedural revalidation](0008-pr-plan.md#pr-5-featurev030-rfc0008-procedural-revalidation--phase-4b-confidence-decay--revalidation)
- [RFC 0008 PR plan PR 6 — Review follow-ups + RFC close](0008-pr-plan.md#pr-6-featurev030-rfc0008-close--review-follow-ups--rfc-close)

---

## Memory Quality Roadmap addenda

The [Memory Quality Roadmap](../memory-quality-roadmap.md) (ratified 2026-05-01) folds two scope items into this calibration review window. Both are formula-level changes that ride the same 30-day data-collection pass — no new RFC needed. Tracked as **MQ-7** in [v0.3.0-plan.md §Memory Quality Follow-Ups](../v0.3.0-plan.md#memory-quality-follow-ups-v03x-and-beyond).

### §C — Salience score with use-based reinforcement

[Memory Quality Roadmap §C](../memory-quality-roadmap.md#c-salience-score-with-use-based-reinforcement). Replace [RFC 0008 §G](0008-agent-memory-context-optimization.md#g-memory-eviction-decay-and-validation)'s static `importance × 0.6 + recency_norm × 0.3 + access_freq_norm × 0.1` with a salience term that decays under the existing $c_t = c_0 \cdot e^{-\lambda t}$ but **resets on successful recall** — where "successful" means the entry was admitted into a prompt by the [`MemoryBudget` allocator](0017-persona-memory-injection-budget.md#b-memory-budget-allocator), not dropped under budget pressure.

Implementation surface (additive only):

- New column `last_recalled_at` on the relevant memory tables (episodes, notes, facts once [RFC 0026](0026-declarative-facts-tier.md) ships).
- Update path on `MemoryBudget`-admitted recall — set `last_recalled_at = now()`.
- Scoring formula change: salience uses `last_recalled_at` instead of `last_accessed_at` for the recency term; reinforcement effectively resets the decay curve to `c_0` on each admitted recall.
- Composes with [§D outcome tags](#d--outcome-tagged-importance-bootstrap) — outcome tags seed the initial salience; use reinforces it.

The retune (if any) lands in PR 6 alongside the static eviction parameter retune.

### §D — Outcome-tagged importance bootstrap

[Memory Quality Roadmap §D](../memory-quality-roadmap.md#d-outcome-tagged-importance-not-turn-count-importance) lands as a separate v0.3.x carve-out off [`0020-pr-plan.md`](0020-pr-plan.md) (MQ-1). This calibration review **consumes** the `outcome` and `emotional_weight` columns it adds — once §D ships, the calibration window's importance distribution becomes the primary input for tuning §C's reinforcement constant.

If §D has not landed by the calibration window close, calibrate §C against `turn_count`-derived importance and document the dependency in PR 6 findings.

### Recency-boost calibration (carved out of draft RFC 0023)

The recency-boost calibration originally proposed in draft RFC 0023 lands here, not in 0023 itself. Surface: a small additive boost to recall scores for items closed within a configurable recency window (default 24h). The boost magnitude is calibrated against the same data set as §C — the goal is that a multi-turn outcome-tagged interaction from yesterday outranks a 10-turn neutral interaction from last month.

The retune (if any) ships as a one-line `config/agents.yaml` change in PR 6.

### Findings table extension

Extend the [Findings (PR 6)](#findings-pr-6) table with rows for:

| Parameter | Shipped default | Observed range | Action | Justification |
|-----------|-----------------|----------------|--------|---------------|
| `salience_decay_lambda_per_day` (§C) | inherits `lambda_per_day` | TBD | TBD | TBD |
| `recency_boost_window_hours` (0023 carve-out) | 24 | TBD | TBD | TBD |
| `recency_boost_magnitude` (0023 carve-out) | 0.10 | TBD | TBD | TBD |
| `outcome_bonus.disclosure` (§D consumer) | 0.20 | TBD | TBD | TBD |
| `outcome_bonus.commitment` (§D consumer) | 0.20 | TBD | TBD | TBD |
