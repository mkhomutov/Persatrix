---
id: ISSUE-0109
summary: "RFC 0052 OQ #5 — the autonomous-channel defaults (max_rounds, the interaction cost cap, the roster-scaled 1+N synthesis-reserve fraction, and the standing aggregate bound max_convenings/standing_budget_tokens) shipped as CONSERVATIVE, UNCALIBRATED values: calibration needs a soak on real rosters. Tune after observed autonomous runs (the live MT-AUTONOMOUS-* runs + the four-vendor MT-AUTONOMOUS-MULTIPROVIDER-001). RESOLVED: tuned from the 7-arc v0.3.11 live soak — max_rounds default 12→8 (the cascade-depth cap is the de facto productive-chain length knob; max_rounds is the stall-arc net), full-roster end_vote_threshold on the shipped autonomous templates (K=2 closed 4/7 arcs early without the chair synthesis), the reserve unit soak-validated unchanged (zero close-path denials; peak cap utilization 0.59), and the interaction_cap_utilization close histogram lands so the next pass reads off telemetry."
status: resolved
severity: low
area: channels
created: 2026-07-13
closed: 2026-07-24
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

## Resolution

**Tuned 2026-07-24** from the 7-arc v0.3.11 live soak (the
[execution report capture](../manual-tests/v0.3.11-execution-report.md#issue-0109-calibration-capture)),
mapping the five findings to four changes — every safety gate (mandatory cap,
aggregate bound, fail-closed reserve, arming validation) byte-for-byte
unchanged:

- **`max_rounds` default 12 → 8** (`DefaultAutonomousMaxRounds` +
  `config/channels.yaml` roundtable + both blueprints). Finding 1 made the
  bound's real role legible: the ISSUE-0110 productive-round continuation
  advances the round tally and the reply's cascade depth *together*, so any
  `max_rounds` above the cascade-depth cap (5) is structurally unreachable on
  a productive chain — all five productive soak arcs closed on the depth
  bound, and `max_rounds` never fired at 6/8/12. It is the net for
  STALL-driven arcs (convener cadence turns reset depth); 8 keeps that net
  above every observed arc (MT-AUTONOMOUS-001 ran 8 end to end) while
  tightening worst-case stall-loop spend. The cascade-depth cap is documented
  as the de facto length knob (guide §13), with its Go/Python alignment
  constraint stated.
- **Full-roster `end_vote_threshold` on the shipped autonomous templates**
  (roundtable 3, multivendor blueprint 4; the global K=2 default and every
  human channel untouched). Finding 4: 2-of-3 votes closed 4 of 7 arcs early
  — some as ~20 s confirmation stubs — and an end-vote close arms NO chair
  synthesis, skipping the artifact a brainstorm exists for. A full-roster bar
  routes convergence to the artifact-bearing bounded close unless the roster
  unanimously ends.
- **`channel.conversation.interaction_cap_utilization`** (the issue's
  telemetry ask): a close-funnel histogram — spend-at-close ÷ cap, labelled
  `channel_type` + `trigger`, capped interactions only, recorded once in
  `recordInteractionClosedMetric` for every close cause — so cap and
  standing-bound sizing reads off telemetry instead of wallet-ledger
  scraping. Pinned by `interaction_cap_utilization_test.go` (cost +
  structural fractions; uncapped and wallet-less closes record nothing).
- **Reserve unit soak-validated unchanged** (`DefaultSynthesisCallReserveTokens`
  3500): every close path fit inside the `1 + N` reserve with zero close-path
  lease denials (post-ISSUE-0111), no arc reached the soft threshold (peak
  utilization 0.59 of cap), and neither synthesis_reserve.go KNOWN GAP bit
  live. Findings 2/3/5 (persona-naming as the fresh-store lever; standing
  per-convening topic freshness) ship as operator guidance — guide §13
  "Tuning an autonomous roster" — since no shipped standing default exists to
  tune.

Residuals deliberately NOT taken here: a per-channel cascade-depth override
(a real feature crossing the Go/Python depth-cap alignment, not a default);
close-path-cost telemetry (a second sample at the wallet eviction settle
point, coupled to the RFC 0052 PR 7 `EvictInteraction` wiring); automated
standing topic freshness (a convener-prompt feature).

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
