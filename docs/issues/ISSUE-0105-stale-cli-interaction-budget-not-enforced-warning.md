---
id: ISSUE-0105
summary: "Rust CLI `channel config` prints a stale `interaction_budget_tokens … ⚠ not yet enforced (RFC 0050 Open item 4)` warning; the budget is router-held and enforced server-side since the RFC 0050 interaction-budget amendment (#657/#658)"
status: open
severity: low
area: cli
created: 2026-06-16
refs:
  - docs/rfcs/0050-amendment-interaction-budget-enforcement.md
  - docs/manual-tests/MT-CHANNEL-CONFIG-003.md
---

## Summary

The Rust CLI's `channel config get`/`set` output appends a deferral note to an
overridden `interaction_budget_tokens` row reading `⚠ not yet enforced (RFC 0050
Open item 4)`. That label is stale: the interaction-budget amendment (#657, #658)
made the budget router-held and the wallet now enforces the channel ceiling
server-side. The note tells operators the value they set is inert when it is in
fact enforced.

## Context

The note comes from `knob_note` in
[`cli/src/commands/channel_config.rs:290`](../../cli/src/commands/channel_config.rs);
the function's own doc comment (lines ~282–287) also still asserts the knob "is
not router-held". Both predate the amendment.

Enforcement is wired and verified:
- [`cmd/orchestrator/channels.go:175`](../../cmd/orchestrator/channels.go) injects
  the resolver (`walletSvc.SetInteractionBudgetResolver(router.ResolveInteractionBudgetForInteraction)`).
- The wallet denies over-budget leases via `interactionCeilingDenialLocked`
  ([`internal/wallet/interaction_budget.go`](../../internal/wallet/interaction_budget.go))
  with `LEASE_DENIED_REASON_INTERACTION_BUDGET_EXHAUSTED`.
- Demonstrated live on build `015149a` in
  [MT-CHANNEL-CONFIG-003](../manual-tests/MT-CHANNEL-CONFIG-003.md): a 500-token
  ceiling denied both mentioned personas' leases server-side (estimated ~5926 >
  500, `spent=0`, fail-closed); raising it relieved the next interaction.

## Impact

Cosmetic / messaging only — the Go enforcement is correct and unaffected. The
risk is operator confusion: an operator who sets a tight budget and reads the CLI
row would conclude it does nothing, when it is enforced. No behavioral defect.

## Proposed fix / investigation path

In [`channel_config.rs`](../../cli/src/commands/channel_config.rs): drop the
`knob_note` deferral for `interaction_budget_tokens` (and the matching stale
clause in the doc comment), or replace it with an accurate note (e.g. that `0`
means uncapped and the ceiling is enforced at interaction open). The CLI has no
other consumer of this string — it is render-only. No Go or proto change.

## Notes

> 2026-06-16 — captured during the MT-CHANNEL-CONFIG-003 live run and the
> RFC 0050 close-out (#661 review). The PR close-out references this as the one
> cosmetic follow-up surfaced by the live arcs.
