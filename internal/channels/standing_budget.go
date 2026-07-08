package channels

// standing_budget.go — RFC 0052 §E standing/scheduled discussions, the aggregate
// SPEND ceiling (v0.3.11 PR 7b). The twin of convening_counter.go's aggregate
// COUNT ceiling: PR 7a made an armed STANDING channel un-creatable without an
// aggregate bound (`autonomous.max_convenings` AND/OR `autonomous.standing_budget_tokens`,
// [ErrAutonomousStandingBoundRequired]) and PR 7b-i activated the count half as a
// live ceiling; this file activates the SPEND half. A standing channel opens a
// fresh, SEPARATELY-capped interaction each fire, so the per-interaction cost cap
// leaves the recurring TOTAL spend unbounded — `standing_budget_tokens` is that
// total's ceiling. Each interaction CLOSE folds its settled discussion spend into
// a per-channel running total ([ChannelRouter.foldStandingSpendOnClose], wired at
// the [ChannelRouter.markInteractionClosed] close-notification seam), and
// [ChannelRouter.ConveneChannel] refuses a fresh convening once that total reaches
// the budget ([ErrAutonomousStandingBudgetExhausted], 429 — the sibling of the
// count ceiling's [ErrAutonomousConveningBoundReached]).
//
// SCOPE LIMITS (deliberate, all fail SAFE — the gate can only ever refuse a
// convening the budget would allow or overrun by a bounded tail, never admit an
// unbounded runaway; honest gaps between §E's framing and this slice, the
// siblings of convening_counter.go's):
//
//   - Per-process, NOT per-window. The running total is in-memory and a restart
//     resets it to zero, so it bounds spend within ONE process lifetime — not
//     across the standing window §E targets. A durable total needs persistence,
//     which RFC 0052 rules OUT ("no new store migration"); the across-restart
//     bound is a tracked follow-up. Latent today (manual convene only; the timer
//     is a later slice), load-bearing once the schedule fires unattended. The
//     count ceiling shares this exact limit (convening_counter.go).
//   - Discussion spend only; the async close-path tail is folded best-effort. The
//     fold reads [wallet.WalletService.InteractionSpend] at the close NOTIFICATION
//     (markInteractionClosed), by which point the DISCUSSION leases have settled
//     but the per-persona RFC 0020 close summaries (OQ #6-metered, fire-and-forget
//     CROSS-PROCESS, close_path.py) may not have — so the folded total can
//     UNDER-count by up to the close-path reserve (1+N calls, synthesis_reserve.go).
//     The gate therefore refuses at-or-slightly-late, and the per-window over-run
//     is bounded by that reserve per convening; the co-declared count ceiling (the
//     common standing config) caps the number of convenings, bounding the total
//     over-run. A tight settle barrier is the eviction slice's concern (the same
//     cross-process precondition [wallet.WalletService.EvictInteraction] documents);
//     this slice deliberately does NOT evict — it leaves the wallet residue exactly
//     as PR 4b-i left it (bounded_close.go), because folding a router-side running
//     total is orthogonal to pruning the wallet's per-interaction map.
//   - Only interactions that reach the deliberate-close notification are folded;
//     one that idle-rotates WITHOUT a bounded/end-vote close (interaction_resolver.go)
//     is not — its id never enters this seam. The autonomous norm is a bounded
//     close (max_rounds/cost) or an end-vote quorum, both routed through
//     markInteractionClosed, so a fizzled-then-rotated discussion escaping the fold
//     is the off-normal case, and it fails SAFE (under-count).

import "errors"

// ErrAutonomousStandingBudgetExhausted — [ChannelRouter.ConveneChannel] against a
// channel whose folded across-window spend has reached its
// `autonomous.standing_budget_tokens`. The aggregate SPEND ceiling (RFC 0052 §E)
// is exhausted: a standing channel opens a fresh, separately-capped interaction
// each fire, so the per-interaction cost cap leaves the recurring TOTAL unbounded
// — `standing_budget_tokens` is that total's ceiling ([ErrAutonomousStandingBoundRequired]
// required it (or `max_convenings`) be declared; this enforces it). The REST layer
// maps it to 429 Too Many Requests, the sibling of the count ceiling's
// [ErrAutonomousConveningBoundReached] — an aggregate allowance is spent, not a
// malformed request or a state conflict.
var ErrAutonomousStandingBudgetExhausted = errors.New("channels: autonomous channel reached its aggregate standing spend budget (autonomous.standing_budget_tokens)")

// recordStandingSpend folds `tokens` of settled spend into channelID's
// process-lifetime running total against the §E `standing_budget_tokens` ceiling.
// Additive; a non-positive `tokens` (an untracked/uncapped/zero-spend interaction,
// or a nil-wallet read) is a no-op, so the total only ever grows by real spend.
// Its own lock — never held across the wallet read that produces `tokens`
// ([foldStandingSpendOnClose] reads InteractionSpend FIRST, then folds) nor across
// any dispatch RPC — so a fold on one channel never blocks traffic on another.
func (r *ChannelRouter) recordStandingSpend(channelID string, tokens int64) {
	if tokens <= 0 {
		return
	}
	r.standingMu.Lock()
	r.standingSpend[channelID] += tokens
	r.standingMu.Unlock()
}

// foldStandingSpendOnClose reads the closing interaction's settled running total
// from the wallet ([interactionSpender.InteractionSpend]) and folds it into
// channelID's standing spend. Called from [ChannelRouter.markInteractionClosed] on
// the open→retired transition (exactly once per closed interaction), OUTSIDE
// interactionMu so the wallet read never inverts the router→wallet lock order the
// budget resolver relies on. A nil spender (a $0/mock fleet, or a unit test with
// no wallet wired) folds nothing — the standing gate is then inert, the same
// posture as the bounded-close soft-budget trigger (bounded_close.go). The
// InteractionSpend read is fully evaluated before [recordStandingSpend] takes
// standingMu, so the wallet lock and standingMu are never held simultaneously.
func (r *ChannelRouter) foldStandingSpendOnClose(channelID, interactionID string) {
	if r.spend == nil {
		return
	}
	r.recordStandingSpend(channelID, r.spend.InteractionSpend(interactionID))
}

// StandingSpend reports channelID's process-lifetime folded spend — the value the
// §E `standing_budget_tokens` gate measures against and the web standing-budget
// readout (a later slice) will render. Zero for a channel that has folded no
// close.
func (r *ChannelRouter) StandingSpend(channelID string) int64 {
	r.standingMu.Lock()
	defer r.standingMu.Unlock()
	return r.standingSpend[channelID]
}

// standingBudgetReached reports whether channelID's folded spend has reached a
// POSITIVE `budget`. A non-positive budget is unbounded (the count-only or
// one-shot case) — the spend is tracked but never gated, mirroring
// [ChannelRouter.reserveConvening]'s non-positive-max posture.
func (r *ChannelRouter) standingBudgetReached(channelID string, budget int64) bool {
	return budget > 0 && r.StandingSpend(channelID) >= budget
}

// clearStandingSpend forgets channelID's standing spend — called from
// [ChannelRouter.PurgeChannelInteraction] on channel delete so the map does not
// leak one entry per deleted standing channel. NOT called on disarm/re-arm: the
// aggregate budget is deliberately not refilled by re-arming within a process (the
// conservative safety posture the convening count shares); an operator wanting a
// true reset deletes and recreates the channel.
func (r *ChannelRouter) clearStandingSpend(channelID string) {
	r.standingMu.Lock()
	defer r.standingMu.Unlock()
	delete(r.standingSpend, channelID)
}
