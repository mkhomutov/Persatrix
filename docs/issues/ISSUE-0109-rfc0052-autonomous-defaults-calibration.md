---
id: ISSUE-0109
summary: "RFC 0052 OQ #5 — the autonomous-channel defaults (max_rounds, the interaction cost cap, the roster-scaled 1+N synthesis-reserve fraction, and the standing aggregate bound max_convenings/standing_budget_tokens) shipped as CONSERVATIVE, UNCALIBRATED values: calibration needs a soak on real rosters. Tune after observed autonomous runs (the live MT-AUTONOMOUS-* runs + the four-vendor MT-AUTONOMOUS-MULTIPROVIDER-001)."
status: open
severity: low
area: channels
created: 2026-07-13
refs:
  - docs/rfcs/0052-autonomous-agent-channels.md
  - docs/rfcs/0052-pr-plan.md
  - docs/manual-tests/MT-AUTONOMOUS-001.md
  - docs/manual-tests/MT-AUTONOMOUS-MULTIPROVIDER-001.md
---

## Summary

The RFC 0052 autonomous-channel tunables shipped as **conservative,
uncalibrated defaults** — the [OQ #5](../rfcs/0052-autonomous-agent-channels.md#open-questions)
resolution ("conservative defaults + a calibration tracked-issue, filed at
closeout"). This is that tracked issue, filed at the RFC 0052 PR 9 closeout.
None of them is *wrong*; none is *tuned*. Calibration needs a soak on real
rosters, which does not exist until the live acceptance runs.

The tunables:

- **`autonomous.max_rounds`** — the deterministic bounded-close round bound
  (the demos ship `12`; MT-AUTONOMOUS-001 Step 1 uses `8`). Too low truncates a
  productive discussion before it converges; too high wastes spend on a
  discussion that already reached its point.
- **`interaction_budget_tokens`** — the mandatory per-interaction cost cap (the
  demos ship `200000`). The soft-budget close fires at `cap − reserve`, so the
  cap and the reserve fraction jointly decide how much discussion happens before
  the close path.
- **The `1 + N` synthesis-reserve fraction** — the share of the cap held back
  for the chair synthesis turn + one metered RFC 0020 summary per participating
  persona ([`internal/wallet/synthesis_reserve.go`](../../internal/wallet/synthesis_reserve.go),
  clamped to ≤ half the cap). Too small denies a persona's summary on a
  budget-exhausted close (the `[interaction summary unavailable]` placeholder
  the reserve exists to prevent); too large starves the discussion.
- **The standing aggregate bound** — `max_convenings` and
  `standing_budget_tokens` (RFC 0052 §E), the ceilings a standing channel
  re-convenes under. Conservative low defaults; the right values depend on the
  cadence and the per-convening spend a real standing roster shows.

## Context

Captured at the RFC 0052 PR 9 closeout per the OQ #5 resolution. The defaults
were deliberately conservative because tuning them needs telemetry from real
autonomous runs, which the deterministic CI backbone (mock provider) does not
produce — it pins the *invariants* (bounded close fires, the `1 + N` reserve is
honoured, both artifacts produced, spend ≤ cap), not the *ergonomics* (does a
real roster converge within `max_rounds`, is the reserve fraction right for a
typical summary length).

## Impact

Low, and self-limiting: every default is a **safe** conservative value —
the safety gates (mandatory cap, aggregate bound, fail-closed reserve) hold
regardless, so a mis-tuned default degrades *quality* (a discussion truncated a
round early, a reserve slightly over-sized), never *safety*. The four-vendor run
adds a wrinkle worth watching in the soak: seats priced at very different
per-token rates (watsonx Llama-70B vs. Gemini Flash — a 6× input-price spread)
consume the single shared cap unevenly, so `max_rounds` and the cap may want
revisiting once cross-vendor spend distributions are observed.

## Proposed fix / investigation path

Run the live acceptance MTs during v0.3.11 release-prep (master-plan Phase 3):
[MT-AUTONOMOUS-001](../manual-tests/MT-AUTONOMOUS-001.md) (single-provider),
[MT-AUTONOMOUS-002](../manual-tests/MT-AUTONOMOUS-002.md) (anti-collapse),
[MT-AUTONOMOUS-003](../manual-tests/MT-AUTONOMOUS-003.md) (standing), and
[MT-AUTONOMOUS-MULTIPROVIDER-001](../manual-tests/MT-AUTONOMOUS-MULTIPROVIDER-001.md)
(four vendors). Record per-run: rounds-to-convergence vs. `max_rounds`, the
close trigger (`structural` vs. `cost`), the reserve headroom actually used
(chair turn + per-persona summary token counts vs. the held-back reserve), and
— for standing — per-convening spend across a window. Tune the shipped demo
defaults + the reserve fraction from those observations; keep the safety gates
unchanged. Consider surfacing the reserve headroom as an OTEL metric so the
soak reads off telemetry rather than log-scraping.

## Notes

> 2026-07-13 — filed at the RFC 0052 PR 9 closeout (OQ #5 "conservative defaults
> + a calibration tracked-issue, filed at closeout"). Blocked on the live soak;
> no code change until real autonomous-run telemetry exists.
> 2026-07-22 — the v0.3.11 release-prep live soak is DONE and the calibration
> data is recorded in the
> [execution report §ISSUE-0109 calibration capture](../manual-tests/v0.3.11-execution-report.md#issue-0109-calibration-capture)
> (7 live arcs across single-vendor + four-vendor rosters). Headline findings
> for the tuning PR: (1) the global `max_cascade_depth` (5) binds before every
> `max_rounds` tried (6/8/12) on a productive roster — it is the de-facto
> discussion-length knob and `max_rounds` never fired live; (2) `end_votes`
> dominates converged rosters (4 of 7 arcs) — the bounded close is the net,
> not the norm; (3) re-convening an unchanged topic yields push-back /
> redundant syntheses — standing channels need per-convening topic freshness;
> (4) the `1+N` reserve was never approached (largest arc spent 24 % of cap);
> (5) collapse-proneness inversely tracks accumulated channel memory — naming
> personas in the topic is the fresh-store lever. Tuning remains a follow-up
> code PR; the safety gates stay unchanged.
