# ISSUE-0082 residuals — the close-path reserve re-size (record)

**Companion to**: [ISSUE-0082 residuals PR plan](ISSUE-0082-residuals-pr-plan.md) (PR 4)
**Issues**: [ISSUE-0123](ISSUE-0123-per-speaker-interaction-scope.md) (R-1) · [ISSUE-0131](ISSUE-0131-derived-memory-has-no-speaker-attribution.md) (the speaker axis)
**Status**: 📋 Open — the calibration itself files as its own issue in the [ISSUE-0109](ISSUE-0109-rfc0052-autonomous-defaults-calibration.md) idiom
**Split out**: 2026-08-23, when the [v0.3.15 plan](../v0.3.15-plan.md) added the threshold-basis and signal obligations and the combined doc reached the 3 000-word cap — the same move that produced the [Phase 0 gate record](ISSUE-0082-residuals-phase0-gate.md) on 2026-08-21.

This is the sizing analysis behind residuals PR 4. It is a *record*, not a
constant: the number is deliberately not guessed here.

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
