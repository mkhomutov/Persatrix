---
id: ISSUE-0058
summary: The personal-tier recall-latency perf gate compares measured p99/p50 against a baseline at a fixed 20% tolerance; once enforcing, GitHub-hosted-runner hardware-class variance may exceed 20% with no code change and fail CI — revisit the tolerance/methodology once enforcing-gate CI data exists.
status: open
severity: medium
area: ci
created: 2026-05-18
refs:
  - docs/rfcs/0029-pr-plan.md
  - docs/rfcs/0029-personal-society-storage-split.md
  - tests/perf/personal_tier_latency.py
  - .github/workflows/ci.yml
---

## Summary

The personal-tier recall-latency perf gate
(`tests/perf/personal_tier_latency.py`, RFC 0029 §Test Strategy) fails the
build when measured p99 or p50 exceeds the committed baseline by more than
a fixed 20% (`DEFAULT_REGRESSION_TOLERANCE`). GitHub-hosted runners are not
a fixed hardware target — runs land on different physical host classes —
so once the gate is enforcing, run-to-run variance alone may exceed 20%
and fail CI with no code regression behind it.

## Context

Captured during the RFC 0029 Phase 1 PR 5 (#376) review. PR 5 ships the
gate logic informational-only; it flips to enforcing when the
`perf-baseline-capture` workflow's follow-up PR lands
`tests/perf/baselines/personal_tier_latency.json`.

The PR already mitigates *measurement* noise two ways: a 20% tolerance,
and a p50 co-gate alongside p99 (p50 is the steadier statistic). Neither
addresses *hardware-class* variance — the baseline is captured on one
runner, and a later gate run on a slower host class is compared against
it. The p50 co-gate in particular does not reduce this risk; it adds a
second metric that can trip on the same host variance.

This is deliberately **not** pre-tuned. Whether 20% is too tight (false
failures on unrelated PRs) or too loose (real regressions slip through)
cannot be known without observing real enforcing-gate CI runs. The fix
trigger is the arrival of that data, not a date.

## Impact

While the gate is informational-only (its state as shipped in PR 5) there
is no impact — `main` exits 0 regardless of the measurement. Once the
baseline lands and the gate enforces, host-variance false positives would
fail unrelated PRs' CI, eroding trust in the gate and prompting reflexive
re-runs that mask any genuine regression the gate later catches.

## Proposed fix / investigation path

Once enforcing-gate CI data exists, review the observed false-positive
rate and, if warranted, choose among:

- Widen `DEFAULT_REGRESSION_TOLERANCE` to cover observed host variance.
- Capture and compare a median (or best-of-N) across repeated
  measurements within one job to damp single-run noise.
- Pin the gate run (and the baseline capture) to a larger or more
  consistent runner class.
- Downgrade a single-run breach to a warning and fail only on a
  regression sustained across consecutive runs.

Defer the choice until observed enforcing-gate data exists — do not
speculatively retune the tolerance before there is anything to measure.

## Notes

> 2026-05-18 — initial capture during the RFC 0029 Phase 1 PR 5 (#376)
> review. The gate is informational-only as shipped; this issue is the
> standing reminder to revisit the tolerance/methodology before — or
> shortly after — the `perf-baseline-capture` follow-up PR flips it to
> enforcing.
