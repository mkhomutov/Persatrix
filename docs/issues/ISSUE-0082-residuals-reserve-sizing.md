# ISSUE-0082 residuals — the close-path reserve re-size (record)

**Companion to**: [ISSUE-0082 residuals PR plan](ISSUE-0082-residuals-pr-plan.md) (PR 4b)
**Issues**: [ISSUE-0123](ISSUE-0123-per-speaker-interaction-scope.md) (R-1) · [ISSUE-0131](ISSUE-0131-derived-memory-has-no-speaker-attribution.md) (the speaker axis)
**Status**: ✅ Discharged — the re-size, the clamp signal and the threshold basis shipped in residuals **PR 4b** (2026-08-31); the deferred calibration is [ISSUE-0138](ISSUE-0138-close-reserve-multiplier-calibration.md), filed in the [ISSUE-0109](ISSUE-0109-rfc0052-autonomous-defaults-calibration.md) idiom
**Split out**: 2026-08-23, when the [v0.3.15 plan](../v0.3.15-plan.md) added the threshold-basis and signal obligations and the combined doc reached the 3 000-word cap — the same move that produced the [Phase 0 gate record](ISSUE-0082-residuals-phase0-gate.md) on 2026-08-21.

This is the sizing analysis behind residuals PR 4b. It is a *record*, not a
constant: the number is deliberately not guessed here.

## What shipped (PR 4b, 2026-08-31)

All four obligations below, and nothing beyond them:

- **The multiplier**, as [`wallet.CloseRecordUpperBound`](../../internal/wallet/synthesis_reserve.go). The personas and the principal-bearing people PARTITION the roster, since a tenant enters a room only when an authenticated person publishes — so the room's bound is the maximum over that partition, `⌊(c+1)²/4⌋ × (c + ControlSenderSpeakers)`, rather than the naive product a literal reading of `personas × principals × speakers` gives. The partition divides that product by about four; it does **not** lower the degree, and the bound stays cubic. The constant is what matters anyway: on a 4-seat room the partition bound is 36 records and the naive one is 96 — neither fits a 200k cap without clamping, which is why the signal ships with the multiplier rather than after it. It is still a WORST CASE — sizing off the interaction's OBSERVED pairs needs per-interaction state the trigger does not carry, and is ISSUE-0138's to decide.
  - **The speaker axis is not bounded by the roster** (review finding). The record key's speaker half is the ingested event's `sender_id`, and RFC 0052's two forced-turn directives ride SYNTHETIC senders — `orchestrator:convene`, `orchestrator:synthesis` — that are deliberately not valid participant ids and hold no seat, yet each opens a close record the room fan then closes and meters. So the speaker term is `c + 2` (`wallet.ControlSenderSpeakers`). It is load-bearing at exactly the shape the fleet ships: a 3-seat room (one authenticated person + two personas) sits AT the partition maximum, so it has zero slack and the member-count-only bound was not a bound. The wallet cannot import `internal/channels`, so the count is a constant there and `TestOrchestratorDispatchSenders_MatchTheReserveSpeakerAllowance` pins it structurally against the enumerated senders — a third one cannot be added by omission.
- **The signal**, as `channel.conversation.synthesis_reserve_clamped{channel_type, trigger}` plus a Warn line carrying the channel, interaction, room size, `R`, cap and reserve. Emitted from `boundedClose` — the single funnel every bounded close path passes through, behind the tombstone CAS — so it counts once per close that actually FIRED, never for a crossed bound that the fresh-config re-check refuses or that loses its arm/tombstone race. That placement is what makes the counter divisible by `interaction_closed`, which is how ISSUE-0138 reads it. On BOTH triggers, since a `max_rounds` close runs the same close path against the same clamped reserve. Two cadences, deliberately: the COUNTER is per close (the rate depends on it), the WARN is once per channel per configuration, because the clamp is a property of the room and the cap and a per-close line would repeat verbatim forever. And neither fires on a fleet with **no wallet** — `interaction_budget_tokens` is channel config and mandatory on an autonomous channel, but a deployment with no cost config draws no close-path lease, so it cannot suffer the failure. The agent-side half of the pair, a counter at the `SUMMARY_UNAVAILABLE_TEXT` commit, was **already in place**: `summarize_close.py` has emitted `agent.interactions.summary.failed{reason="budget_denied"}` at the wallet-denial arm since the PR #718 review, and the janitor emits `{reason="janitor"}` at its own backfill. Nothing was added there; the obligation is discharged by what exists, which the PR states rather than re-implements.
- **The threshold basis**, by making it the same value. `maybeBoundedClose` resolves `R` once and hands it to both `SynthesisSoftBudgetTokens` and (through it) `SynthesisReserveTokens`, so the one-directional requirement below holds by construction. The Go half needed its own slot exactly as predicted — two packages, `internal/wallet` and `internal/channels`.
- **The calibration**, as [ISSUE-0138](ISSUE-0138-close-reserve-multiplier-calibration.md): the basis, the clamp policy, the per-call unit re-soaked against the new multiplier, and the single-close-summarizer alternative.

### The two halves, in detail

* **The asymmetry is retired, not resolved** — and the audit says the field is dead, so it is dropped. `pendingSynthesisClose.principal` had exactly one read: re-stamping the timeout net's `context.Background()` so the close fan landed in the arming person's tenant. Three legs, each checked rather than assumed, say that context now selects nothing: `close_notification.py` closes `records_for_scope` principal-blind and `record_write_scopes` re-binds each record's OWN frozen principal for its whole derivation; `markerCloseNotification` resolves `expectsReply() == false`, so the fan leaves no ISSUE-0124 attribution entry to re-stamp a later publish with; and `finalizeInteractionClose` dispatches without publishing, so ISSUE-0130's server-stamped `messages.principal_id` never sees it. The audit lives in `synthesis_close.go`'s header, and `TestRestamp_IsTheOnlyPrincipalStampInThisPackage` narrows from two allowlisted stamp sites to one — a strengthening, and the thing that stops the re-stamp coming back by omission.
* **Reserve re-size**: `1 + N` becomes `1 + (personas × principals × speakers)`, as `wallet.CloseRecordUpperBound`. The personas and the principal-bearing people PARTITION the roster (a tenant enters a room only when an authenticated person publishes) and the speaker axis carries the two synthetic control senders on top of the members, so the bound is `⌊(c+1)²/4⌋ × (c + 2)` rather than the naive product — still cubic, a constant factor of about four smaller, and that factor decides whether the half-cap clamp bites at an ordinary cap. Under-sizing degrades *silently* into `SUMMARY_UNAVAILABLE_TEXT`, so the **signal** ships with it and the constant does not: `channel.conversation.synthesis_reserve_clamped{channel_type, trigger}` from `boundedClose`, once per close that fired, on both triggers. The agent-side counter that obligation also names was already in place (`summarize_close.py`'s `budget_denied` arm), so the PR states it rather than re-adding it. The threshold basis is settled by making it the same value — one `R`, resolved once, handed to both. Full record: the [reserve-sizing record](ISSUE-0082-residuals-reserve-sizing.md); calibration: [ISSUE-0138](ISSUE-0138-close-reserve-multiplier-calibration.md), in the [ISSUE-0109](ISSUE-0109-rfc0052-autonomous-defaults-calibration.md) idiom.

**RFC 0052 §D is amended, not re-derived.** The sizing rule stands; what it counts changed. The amendment states the clamp consequence (bounded: a clamped reserve is half the cap, so the discussion gets half its budget and the close fires earlier, never later) rather than fixing it. Two 500-line splits were preconditions, as with PR 4: `bounded_close.go` → `bound_verdict.go`, `synthesis_close.go` → `synthesis_metrics.go`.

**Three test obligations the signal creates**, all pinned: the threshold and reserve bases are ONE value, so a later edit cannot violate the one-directional requirement; the multiplier is pinned against its DEFINITION (brute-forced over every partition point) rather than its closed form, so an algebra slip fails loudly instead of under-sizing every reserve in the fleet; and the counter has a NEGATIVE — an adequately-funded close must stay silent, or a signal that fires on every capped close is indistinguishable from no signal.

One thing the analysis below did not anticipate, found while sizing it: the v0.3.11 ISSUE-0109 soak validated `DefaultSynthesisCallReserveTokens` against `1 + N` **records**, not against the per-call cost in isolation, so it does not carry forward to `1 + R`. The unit is unchanged and still tracks one bounded RFC 0020 summary; the re-soak is ISSUE-0138's.

## The multiplier

The RFC 0052 PR 4a `1 + N` close-path reserve assumes one summary per persona.
With Phase 0b the interaction record is keyed `(principal, speaker, scope)`, so
the multiplier becomes `1 + (personas × principals × speakers)` — the largest
single cost in this workstream.

Under-sizing turns extra summaries into a denied lease →
`SUMMARY_UNAVAILABLE_TEXT`, and the janitor never retries a committed
unavailable row. That is a **silent** quality regression: nothing fails, and
the placeholder persists.

## The half-cap clamp — and why a third dimension changes its character

The calibration issue **must cover the clamp, not just the multiplier.**
[`synthesis_reserve.go`](../../internal/wallet/synthesis_reserve.go) already
carries it as a KNOWN GAP: the clamp guarantees the discussion a positive
working budget, so on a modest cap with a full roster it holds back *less* than
even raw `1 + N`, and the denied personas fall through to the placeholder.

A third dimension makes the clamp bite in the **normal** case rather than the
edge case — an all-agent room under `auth.mode: disabled` (MT Leg 8) is exactly
that shape. KNOWN GAP #2 is additive: the `1` under-sizes the chair turn, whose
input scales with the discussion.

## Two obligations attached by the v0.3.15 plan

**The signal is not deferred with the constant.** The clamp site computes the
condition and returns silently today. A WARN or counter there, and a counter at
the `SUMMARY_UNAVAILABLE_TEXT` commit, ship *with* the re-size. Both are
additive and neither needs the deferred constant — together they are what makes
deferring the calibration safe rather than merely stated.

**The threshold basis moves with the reserve basis, and that half is Go.**
[`bounded_close.go`](../../internal/channels/bounded_close.go) passes
`channelSize` into a roster-sized reserve and documents the requirement as
one-directional — the threshold basis must never be smaller than the roster the
reserve was carved for. Today `channelSize` is always ≥ the persona roster, so
it holds. Once the close emits one record per `(speaker, principal)`, a member
count can be *smaller* than the effective roster, inverting it and re-opening
the "close leases denied" hole the comment warns about. The split is also not
observable from Go — it happens in `agents/persona_runtime/close_path.py`, and
`Interaction` carries no principal or speaker field. So this half does **not**
ride residuals PR 4, which is Python; it needs its own slot in two Go packages.

## Related

- [ISSUE-0082 residuals PR plan](ISSUE-0082-residuals-pr-plan.md) — PR 4 is where the re-size lands.
- [v0.3.15 plan](../v0.3.15-plan.md) — scope lock 1 attaches the two obligations above.
- [ISSUE-0109](ISSUE-0109-rfc0052-autonomous-defaults-calibration.md) — the calibration idiom this follows.
