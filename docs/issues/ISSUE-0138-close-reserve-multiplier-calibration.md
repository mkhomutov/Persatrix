---
id: ISSUE-0138
summary: "The close-path synthesis reserve is now sized off the room's WORST-CASE close-record count, which makes the half-cap clamp bite in ordinary configs — the multiplier's basis, the clamp policy and the per-call unit all need a soak-driven calibration rather than the conservative values shipped with the re-size"
status: open
severity: medium
area: channels
created: 2026-08-31
refs:
  - docs/issues/ISSUE-0082-residuals-reserve-sizing.md
  - docs/issues/ISSUE-0082-residuals-pr-plan.md
  - docs/issues/ISSUE-0109-rfc0052-autonomous-defaults-calibration.md
  - docs/rfcs/0052-autonomous-agent-channels.md
  - docs/v0.3.15-plan.md
---

## Summary

The RFC 0052 close-path reserve holds tokens back from a per-interaction cost
cap so the bounded close can still fund its artifacts — the chair synthesis
turn plus one RFC 0020 summary per close-derived record. v0.3.15 residuals
PR 4b re-sized its multiplier from the persona roster `1 + N` to the
close-**record** count `1 + R`, because the `(principal, speaker, scope)`
re-key made a room hold one record — and issue one metered summary — per
persona per `(principal, speaker)` pair.

The re-size is correct in **shape** and deliberately uncalibrated in
**value**, exactly the split [ISSUE-0109](ISSUE-0109-rfc0052-autonomous-defaults-calibration.md)
made for the autonomous defaults: a constant guessed off no telemetry is worse
than none. This is that tracked issue, filed with the re-size rather than after
it, and the signal it needs shipped alongside — see §Evidence.

## What shipped, and why each part is provisional

**The basis is the room's WORST CASE, not the interaction's.**
[`wallet.CloseRecordUpperBound`](../../internal/wallet/synthesis_reserve.go)
derives `R` from `channelSize` alone: `personas × principals × speakers`,
maximised over the partition of the roster into personas and principal-bearing
people (`⌊(c+1)²/4⌋ × (c + ControlSenderSpeakers)`). It is a true upper bound and
needs no per-interaction state, which is why it shipped — but a real interaction
touches only the `(principal, speaker)` pairs that actually **spoke**, and the
member count includes observer/operator seats that author no summary. On a
5-seat room the bound is 63 records where a two-tenant discussion might close 11.

The speaker term is the members **plus the orchestrator's synthetic control
senders** — the convene and synthesis directives ride sender ids that are not
valid participant ids and hold no seat, yet each keys a close record of its own.
That term was missing from the first shape of the multiplier, which made the
"upper bound" false on a three-seat room (the partition maximum there has zero
slack). It is deliberately loose in the other direction — it allows every
persona a record for BOTH directives when at most one persona receives each per
interaction — so tightening the speaker axis is part of this issue's basis
question, not a separate one.

**The clamp now bites in ordinary configs.** The half-cap clamp
(`maxSynthesisReserve*`) caps the reserve at half the cap so the discussion
always keeps a positive working budget. It does not scale down with `R`, and
`R` grows with the cube of the room — the partition argument divides the naive
product by about four, which is a constant factor rather than a degree — so an
ordinary roster against a six-figure cap now clamps where a persona-count roster
did not: the bundled four-seat `blueprints/autonomous-multivendor` clamps at its
own shipped 200 000 cap (R = 36, raw sizing 129 500, clearing it needs 259 000).
When it clamps, the reserve is a constant (half the cap) and the multiplier stops
informing it — which makes the clamp *rate* on that blueprint the first datum
this calibration has.

**The per-call unit was soak-validated against the OLD multiplier.**
`DefaultSynthesisCallReserveTokens = 3500` was validated across 7 live arcs in
the v0.3.11 ISSUE-0109 soak — but those arcs ran a pre-re-key close that
authored one summary per persona, so they validated the unit against `1 + N`
records, never against `1 + R`. The unit itself is unchanged and still tracks
one bounded RFC 0020 summary; what changed is how many of them a close issues.

## Why deferring is safe

Under-sizing this reserve degrades **silently**: a denied close-path lease
commits the RFC 0020 janitor's `"[interaction summary unavailable]"`
placeholder, and the janitor never retries a committed unavailable row. Nothing
fails; the placeholder persists.

PR 4b therefore shipped the **signal** with the re-size, which is what makes the
constant deferrable rather than merely deferred:

- `channel.conversation.synthesis_reserve_clamped{channel_type, trigger}` — one
  increment per bounded close that fired while the clamp was holding back less
  than the close is sized to need, plus a Warn line carrying the channel, the
  interaction, the room size, `R`, the cap and the reserve.
- `agent.interactions.summary.failed{reason="budget_denied"}` — the agent-side
  half, at the `SUMMARY_UNAVAILABLE_TEXT` commit (`summarize_close.py`).

Together they say both "the cap could not fund this close" and "a summary was
actually lost to it", which is the pair a calibration needs.

## The decisions this issue owns

1. **The multiplier's basis** — keep the room's worst case, or size off the
   interaction's OBSERVED `(principal, speaker)` pairs. The latter is tighter
   but needs per-interaction state the trigger does not carry today; the
   ISSUE-0124 attribution table sees the pairs but retires them on the
   consuming read.
2. **The clamp policy** — a per-roster minimum cap, a clamp that scales with
   `R`, or an operator-facing config error when the cap cannot fund the room.
3. **The per-call unit**, re-soaked against the new multiplier — and KNOWN
   GAP #2 with it: the `1` of `1 + R` reuses the bounded-summary unit for the
   chair turn, whose input scales with the whole discussion, so its true reserve
   is a FRACTION of the soft budget rather than a flat unit.
4. **Scoping the metered summary** to a single designated close-summarizer per
   RFC 0052 §D — the alternative that removes the multiplier instead of sizing
   it, at the cost of the per-record attribution v0.3.15 exists to provide.

## Evidence needed

A soak on real rosters, in the ISSUE-0109 idiom: the
`synthesis_reserve_clamped` rate against `interaction_closed`, the
`budget_denied` share of `interactions.summary.failed`, and the
`interaction_cap_utilization` close histogram, across the topologies v0.3.15
actually ships for — a multi-tenant group room, and the all-agent room under
`auth.mode: disabled` where one `local` principal meets N speakers.

## Related

- [ISSUE-0082 residuals — the close-path reserve re-size](ISSUE-0082-residuals-reserve-sizing.md) — the record this issue discharges the deferred half of.
- [ISSUE-0109](ISSUE-0109-rfc0052-autonomous-defaults-calibration.md) — the calibration idiom, and the soak that validated the unit against the old multiplier.
- [ISSUE-0123](ISSUE-0123-per-speaker-interaction-scope.md) · [ISSUE-0131](ISSUE-0131-derived-memory-has-no-speaker-attribution.md) — the re-key that changed the multiplier.
- [RFC 0052 §D](../rfcs/0052-autonomous-agent-channels.md) — the artifact guarantee the reserve exists to fund.
