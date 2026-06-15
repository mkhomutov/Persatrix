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
