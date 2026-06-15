package channels

import (
	"context"
	"fmt"
)

// interaction_budget.go holds the RFC 0030 Layer 1 (v0.3.8) per-channel
// interaction cost ceiling (`interaction_budget_tokens`) as router-held state,
// the Layer 1 sibling of reply_budget.go. Split out of router.go to keep that
// file under the 500-line review cap; the `budgetMu` mutex + its maps are
// declared on [ChannelRouter] in router.go (a struct field must live with the
// type), only the methods move here.
//
// RFC 0050 amendment (interaction-budget enforcement, server-side resolution):
// this PR makes the budget router-held so an operator's per-channel override
// becomes live state and the GET /config inherited value resolves (no longer
// null). It does NOT yet enforce — the wallet-side resolution that consumes this
// value lands in the amendment's PR 2. So this layer is, for now, resolved and
// surfaced but not yet acted on.
//
// Like the reply budget, zero is a MEANINGFUL value ("uncapped"), not a
// "use the default" sentinel: a runtime-created channel cannot inherit the
// fleet default by passing 0, so the inherit path goes through
// [ChannelRouter.ApplyDefaultInteractionBudget] rather than a bare
// SetInteractionBudgetTokens(_, 0).

// SetInteractionBudgetTokens resolves the RFC 0030 Layer 1 per-interaction cost
// ceiling (in tokens) for `channelID`; `budget <= 0` means uncapped (the opt-in
// default) and is stored verbatim as 0. Driven at startup by
// [ChannelRouter.ResolveInteractionBudgets] (per-channel resolved value) and on
// the live apply path ([ChannelRouter.applyOverridesToRouter]) when an operator
// sets an override. The mutex makes the runtime call safe concurrently with
// traffic.
func (r *ChannelRouter) SetInteractionBudgetTokens(channelID string, budget int64) {
	if budget < 0 {
		budget = 0
	}
	r.budgetMu.Lock()
	defer r.budgetMu.Unlock()
	r.channelBudgets[channelID] = budget
}

// ApplyDefaultInteractionBudget stamps the fleet-wide
// `default_interaction_budget_tokens` (captured at
// [ChannelRouter.ResolveInteractionBudgets]) onto a channel — the inherit path
// for the apply seam when an override is absent. It is a distinct method because
// interaction-budget zero is uncapped-as-a-value rather than a "use the default"
// sentinel: a channel cannot inherit the default by passing 0, so the seam
// delegates the lookup here instead of duplicating the fleet-default field. The
// reply-budget sibling is [ChannelRouter.ApplyDefaultReplyBudget]. No-op-safe
// before ResolveInteractionBudgets has run (the captured default is then 0 =
// uncapped, the opt-in default).
func (r *ChannelRouter) ApplyDefaultInteractionBudget(channelID string) {
	r.budgetMu.Lock()
	d := r.defaultInteractionBudget
	r.budgetMu.Unlock()
	r.SetInteractionBudgetTokens(channelID, d)
}

// InteractionBudgetTokensFor returns the resolved Layer 1 interaction budget for
// `channelID` (0 = uncapped). Exposed for the GET /config effective-value read
// ([Server.buildChannelConfigResponse]) and ops introspection, mirroring
// [ChannelRouter.ReplyBudgetFor]; the amendment's PR 2 enforcement resolver
// reads the same map under the lock.
func (r *ChannelRouter) InteractionBudgetTokensFor(channelID string) int64 {
	r.budgetMu.Lock()
	defer r.budgetMu.Unlock()
	return r.channelBudgets[channelID]
}

// ResolveInteractionBudgets applies the RFC 0030 Layer 1 (v0.3.8) per-channel
// interaction budget to every group channel known at startup — the Layer 1
// sibling of [ChannelRouter.ResolveReplyBudgets], and with the same store-
// enumeration: each config-declared channel uses its resolved
// `interaction_budget_tokens` (channel-over-fleet precedence via
// [ChannelConfig.ResolveInteractionBudgetTokens]); every other group channel
// present in the store — e.g. a runtime-created channel that survived a restart
// — inherits the fleet `default_interaction_budget_tokens`. The store
// enumeration is required (unlike end-vote / idle, which fall back at read time)
// precisely because budget zero is a meaningful value: a store-resident channel
// left unseeded would read 0 (uncapped) instead of inheriting a non-zero fleet
// default.
//
// DM and thread channels are skipped: the budget governs open-floor group
// traffic, like the reply budget. Call once after [ChannelRouter.ReconcileConfig];
// idempotent.
func (r *ChannelRouter) ResolveInteractionBudgets(ctx context.Context, cfg *Config) error {
	var fleetDefault int64
	if cfg != nil {
		fleetDefault = cfg.DefaultInteractionBudgetTokens
	}
	// Capture the fleet default so a runtime-created channel can inherit it via
	// [ChannelRouter.ApplyDefaultInteractionBudget] (zero is meaningful, so it
	// cannot ride a Set(_, 0) sentinel).
	r.budgetMu.Lock()
	r.defaultInteractionBudget = fleetDefault
	r.budgetMu.Unlock()
	configured := make(map[string]bool)
	if cfg != nil {
		for _, decl := range cfg.Channels {
			id := decl.CanonicalID()
			configured[id] = true
			r.SetInteractionBudgetTokens(id, decl.ResolveInteractionBudgetTokens(fleetDefault))
		}
	}
	all, err := r.store.ListChannels(ctx, 0, "")
	if err != nil {
		return fmt.Errorf("channels: resolve interaction budgets: list channels: %w", err)
	}
	for _, ch := range all {
		if ch.Type != ChannelTypeGroup || configured[ch.ID] {
			continue
		}
		r.SetInteractionBudgetTokens(ch.ID, fleetDefault)
	}
	return nil
}

// snapshotInteractionBudget records the channel's effective interaction budget
// against `interactionID` the moment that interaction first commits (RFC 0050
// amendment — server-side resolution, "snapshot at interaction open"). The
// snapshot is what the wallet's resolver reads, so the ceiling is fixed for the
// interaction's life: an operator editing the channel budget mid-interaction
// re-bases only the NEXT interaction, not an in-flight one. Called from
// [ChannelRouter.settleInteraction] on the first commit, after interactionMu is
// released (so the only lock here is budgetMu — no nesting).
//
// Only a CAPPED channel (budget > 0) gets an entry: an uncapped interaction needs
// no snapshot (the resolver's miss already means "uncapped"), so an uncapped
// fleet leaves interactionBudgetSnapshots empty — the latent-until-configured
// property the wallet's running-total map also has. A re-snapshot of an already-
// recorded id is harmless (same channel → same value) but the caller gates on the
// first commit so it does not happen.
//
// Boundary (snapshot-at-FIRST-COMMIT, not at lease): the lease that produces an
// interaction's opening message is acquired before that message is published, so
// it resolves before this snapshot exists and is therefore UNGOVERNED by the
// interaction's own ceiling — every lease after the opening commit is governed.
// In the dominant flow this is a non-issue: the opening message is an inbound
// (human / external) publish carrying no lease, so the snapshot is already in
// place before any agent reply leases. Only a fully agent-initiated opening turn
// (a TICK-driven first post to a channel with no open interaction) escapes the
// ceiling for that one turn, and the Layer 0 depth cap plus the RFC 0023 dollar
// budget still bound it.
func (r *ChannelRouter) snapshotInteractionBudget(interactionID, channelID string) {
	if interactionID == "" {
		return
	}
	r.budgetMu.Lock()
	defer r.budgetMu.Unlock()
	if b := r.channelBudgets[channelID]; b > 0 {
		r.interactionBudgetSnapshots[interactionID] = b
	}
}

// DiscardInteractionBudget evicts a closed interaction's budget snapshot — the
// Layer 1 sibling of [ChannelRouter.DiscardInteractionEndVotes], fired from the
// same deferred resolver seams (rotation / Layer 4 close) so the snapshot lives
// exactly as long as the end-vote tombstone: through the post-close suppression
// window, covering any lease racing the close, then gone. Idempotent — discarding
// an unknown or uncapped (never-snapshotted) interaction is a no-op, so it bounds
// the map the same way the end-vote accumulator is bounded.
func (r *ChannelRouter) DiscardInteractionBudget(interactionID string) {
	if interactionID == "" {
		return
	}
	r.budgetMu.Lock()
	delete(r.interactionBudgetSnapshots, interactionID)
	r.budgetMu.Unlock()
}

// ResolveInteractionBudgetForInteraction returns the snapshotted cost ceiling for
// `interactionID` and whether one exists. It is the server-side resolver the
// wallet calls at lease time (wired via [wallet.WalletService.SetInteractionBudgetResolver]):
// `ok` is true only for a capped interaction that has already committed its first
// message, so a miss (uncapped channel, non-channel/TICK lease, an interaction
// already fully retired, or one still in its opening turn — see
// [ChannelRouter.snapshotInteractionBudget] for why the first lease predates the
// snapshot) reads as "no ceiling". This is the seam that makes the store — not the
// agent-supplied request field — authoritative over the Layer 1 budget.
func (r *ChannelRouter) ResolveInteractionBudgetForInteraction(interactionID string) (int64, bool) {
	r.budgetMu.Lock()
	defer r.budgetMu.Unlock()
	v, ok := r.interactionBudgetSnapshots[interactionID]
	return v, ok
}
