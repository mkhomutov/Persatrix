package channels

// convening_counter.go — RFC 0052 §E standing/scheduled discussions, the
// aggregate convening ceiling (v0.3.11 PR 7b).
//
// PR 7a landed the standing config backend and the §E gate that makes an armed
// STANDING channel un-creatable without an aggregate bound
// ([ErrAutonomousStandingBoundRequired]) — but the bound was DARK: nothing
// tracked or consulted the convening count. This file activates the
// `autonomous.max_convenings` half of that bound as a live runtime ceiling. Each
// SUCCESSFUL [ChannelRouter.ConveneChannel] (manual today, timer-fired in a later
// PR 7b slice) reserves and holds one slot; the (max+1)th convening is refused
// with [ErrAutonomousConveningBoundReached]. This lands BEFORE the timer seam so
// auto-convening is never wired ahead of the ceiling that bounds it — the §E
// mirror of PR 1's cap-required-before-convene ordering.
//
// The reserve/release split makes the count reflect openers that ACTUALLY
// dispatched: [ChannelRouter.reserveConvening] takes a slot atomically before the
// dispatch, and a dispatch MISS returns it via [ChannelRouter.releaseConvening],
// so a flapping convener endpoint whose opener DISPATCH fails never silently
// exhausts the aggregate budget. The release covers a dispatch miss ONLY: an
// opener that lands but whose interaction never commits — a reachable convener
// that returns silence — still consumes its slot, since this layer has no
// interaction-commit signal to release on (that signal is the eviction seam the
// `standing_budget_tokens` slice rides). Doing the check-and-increment under one
// lock (rather than a check() then a separate increment()) also closes the
// count's half of the idle convene race convene.go's header defers to "PR 7"
// (two concurrent convenes both slipping past a plain read at count == max-1).
//
// Two scope limits below are DELIBERATE, not oversights — both fail SAFE (the
// count can only ever refuse early, never exceed `max`), so neither is a
// budget-evasion hole; both are honest gaps between §E's framing and this slice:
//
//   - Per-process, NOT per-window. The count is in-memory and a restart resets it
//     to zero, so it bounds the recurring total within ONE process lifetime — not
//     across the standing window §E's safety framing targets (the aggregate bound
//     exists precisely because per-interaction capping leaves the recurring total
//     unbounded; RFC 0052 §E / Security). A long-lived standing channel outlives
//     many restarts (deploys/crashes), each refilling the budget from zero. This
//     is NOT the benign sibling of the wallet's per-interaction reset a naive
//     analogy suggests: that cap is legitimately per-interaction, whereas this
//     bound exists to span interactions. A durable count would need persistence,
//     which RFC 0052 rules OUT ("no new store migration"); the across-restart
//     bound is a tracked follow-up (0052 PR plan). Latent today (manual convene
//     only; the timer is a later slice) — it becomes load-bearing the moment the
//     schedule fires unattended.
//   - Counts opener DISPATCHES, not distinct discussions. A slot is reserved per
//     landed opener. The two-convenes-before-first-commit idle race is NOT yet
//     closed (the force-fresh slice owns it — convene.go's header), and the
//     convene REST path is not per-channel serialized, so a raced burst can each
//     burn a slot while their openers fold into ONE interaction — exhausting
//     `max_convenings` on FEWER real discussions than the count. The safe
//     direction for a ceiling (refuse early); an exact discussion count is not
//     promised.
//
// The `standing_budget_tokens` half of the aggregate bound (an aggregate SPEND
// ceiling across the standing window) is its own slice, now landed in
// standing_budget.go — it folds each interaction's settled discussion spend into a
// per-channel running total at close rather than tracking a simple count. A
// standing channel that declares ONLY a token budget (no `max_convenings`) is
// therefore spend-gated there, not count-gated here; the config gate requires it
// declare one of the two.

import "errors"

// ErrAutonomousConveningBoundReached — [ChannelRouter.ConveneChannel] against a
// channel that has already been convened `autonomous.max_convenings` times. The
// aggregate count ceiling (RFC 0052 §E) is exhausted: a standing channel opens a
// fresh, separately-capped interaction each fire, so the per-interaction cost cap
// leaves the recurring TOTAL unbounded — `max_convenings` is that total's ceiling
// ([ErrAutonomousStandingBoundRequired] required it be declared; this enforces
// it). The REST layer maps it to 429 Too Many Requests, the sibling of the RFC
// 0030 Layer-2 [ErrParticipantBudgetExhausted] quota-exhausted case — an
// aggregate allowance is spent, not a malformed request or a state conflict.
var ErrAutonomousConveningBoundReached = errors.New("channels: autonomous channel reached its aggregate convening bound (autonomous.max_convenings)")

// reserveConvening atomically claims one convening slot for channelID against the
// aggregate ceiling `max`, returning false (claiming nothing) when the count has
// already reached a POSITIVE `max`. A non-positive `max` is unbounded — the count
// is still tracked (for the web readout a later slice surfaces) but never gated.
//
// The claim is taken BEFORE the dispatch and released ([releaseConvening]) if the
// dispatch misses, so the count reflects openers that actually dispatched (not
// convenings that committed an interaction — see the file header's scope limits).
// Check and increment run under one lock so two concurrent convenes at `max-1`
// cannot both slip through (the count's half of the idle convene race).
func (r *ChannelRouter) reserveConvening(channelID string, max int) bool {
	r.conveningMu.Lock()
	defer r.conveningMu.Unlock()
	if max > 0 && r.convenings[channelID] >= max {
		return false
	}
	r.convenings[channelID]++
	return true
}

// releaseConvening returns a slot claimed by [reserveConvening] whose convening
// did not actually open (the opener dispatch missed). Underflow-guarded so a
// double release or a release without a prior reserve cannot drive the count
// negative.
func (r *ChannelRouter) releaseConvening(channelID string) {
	r.conveningMu.Lock()
	defer r.conveningMu.Unlock()
	if r.convenings[channelID] > 0 {
		r.convenings[channelID]--
	}
}

// ConveningCount reports how many openers channelID has dispatched this process
// lifetime (per-process, not durable — see the file header) — the value the §E
// aggregate bound is measured against and the web convening-count readout (a
// later PR 7b slice) renders. Zero for a channel never convened.
func (r *ChannelRouter) ConveningCount(channelID string) int {
	r.conveningMu.Lock()
	defer r.conveningMu.Unlock()
	return r.convenings[channelID]
}

// clearConvening forgets channelID's convening count — called from
// [ChannelRouter.PurgeChannelInteraction] on channel delete so the map does not
// leak one entry per deleted standing channel. NOT called on disarm/re-arm: the
// aggregate ceiling is deliberately not refilled by re-arming within a process
// (the conservative safety posture); an operator wanting a true reset deletes and
// recreates the channel.
func (r *ChannelRouter) clearConvening(channelID string) {
	r.conveningMu.Lock()
	defer r.conveningMu.Unlock()
	delete(r.convenings, channelID)
}
