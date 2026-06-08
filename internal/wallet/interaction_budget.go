package wallet

import (
	"fmt"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/generated/walletpb"
)

// RFC 0030 Layer 1 — the per-interaction cost ceiling (§E). Carved out of
// wallet.go to keep that file under the review-friendly size cap; the state
// (WalletService.interactionTokens) lives there, the policy lives here. All
// three functions assume the caller holds w.mu (AcquireLease / finalize do).

// interactionCeilingDenialLocked returns a populated LeaseResponse_Denied
// when the request would push its interaction's running token total past the
// request's interaction_budget_tokens, or nil when the lease is admitted.
// Opt-in and fail-closed: an empty interaction_id or a zero budget never
// denies (the uncapped pre-v0.3.8 default); a denial means the LLM call does
// not happen (GL5), and the depth cap (Layer 0) remains the always-on net.
//
// The ceiling is read from the request, not stored: every lease of one
// interaction is expected to carry the same resolved channel budget (PR 5
// stamps it from config), so enforcement is last-writer-wins if the budget
// were ever to differ across leases of one interaction — an operator changing
// a channel's interaction_budget_tokens mid-conversation re-bases the cap for
// subsequent leases. The running total it is compared against is server-held.
func (w *WalletService) interactionCeilingDenialLocked(req *walletpb.LeaseRequest, estimatedTokens int64) *walletpb.LeaseResponse {
	interactionID := req.GetInteractionId()
	budget := req.GetInteractionBudgetTokens()
	if interactionID == "" || budget <= 0 {
		return nil
	}
	spent := w.interactionTokens[interactionID]
	if spent+estimatedTokens <= budget {
		return nil
	}
	w.logger.Warn("wallet: lease denied — interaction cost ceiling exceeded",
		zap.String("layer", "cost"),
		zap.String("interaction_id", interactionID),
		zap.String("agent_id", req.GetAgentId()),
		zap.String("cause", req.GetCause().String()),
		zap.Int64("spent_tokens", spent),
		zap.Int64("estimated_tokens", estimatedTokens),
		zap.Int64("interaction_budget_tokens", budget),
	)
	return &walletpb.LeaseResponse{
		Outcome: &walletpb.LeaseResponse_Denied{
			Denied: &walletpb.LeaseDenied{
				Scope:   "interaction",
				Message: interactionBudgetMessage(interactionID, spent, estimatedTokens, budget),
				Reason:  walletpb.LeaseDeniedReason_LEASE_DENIED_REASON_INTERACTION_BUDGET_EXHAUSTED,
			},
		},
	}
}

// recordInteractionGrantLocked folds a granted lease's worst-case estimate
// into its interaction's running total. finalize reconciles it to actuals on
// close. A no-op for the untracked case (empty interactionID) — the caller
// (AcquireLease) passes a non-empty id only when a ceiling is in effect
// (interaction_id set AND budget > 0), so an uncapped lease is never tracked.
//
// The total persists for the interaction's life: the ceiling bounds cumulative
// spend across every lease of one interaction, so a lease arriving after its
// siblings settled must still count against what they already spent. There is
// no "interaction closed" signal at this layer, so a capped interaction that
// retains real (settled) spend keeps its entry for the orchestrator's process
// lifetime — nothing currently evicts it. The only prune path is
// adjustInteractionTokensLocked dropping an entry that reconciles to <= 0
// (a fully-released interaction); a capped interaction that settled non-zero
// spend never reaches zero and so is never pruned. (interactionTokens is the
// wallet's own map, deliberately NOT a cost.TokenCounter scope, so the cost
// counter's ResetDaily — which turns over the dollar scopes — does not touch
// it.) The per-entry residue is bounded in VALUE by the budget, but the entry
// COUNT grows by one per distinct capped interaction. An interaction-lifecycle
// eviction keyed on an actual interaction-closed signal belongs with the PR 5
// composition wiring; until PR 5 stamps a positive budget no production lease
// is tracked, so this map stays empty and the growth is latent, not live.
func (w *WalletService) recordInteractionGrantLocked(interactionID string, estimatedTokens int64) {
	if interactionID == "" {
		return
	}
	w.interactionTokens[interactionID] += estimatedTokens
}

// adjustInteractionTokensLocked applies delta to interactionID's running
// total and prunes the entry once it falls to zero or below, so a fully-
// released interaction (every lease reversed to zero actuals) leaves no
// residue in the map. A no-op for an empty id or a zero delta. Note this
// prunes only when the total reaches <= 0: an interaction that settled real
// spend keeps a positive residue by design (see recordInteractionGrantLocked
// — the total must outlive its leases to bound cumulative interaction spend).
//
// Clamp-at-zero is defensive: actual usage never exceeds the granted estimate
// the provisional was held at, so the reconcile delta is non-positive and the
// total cannot legitimately go negative — but a double finalize that slipped
// past the settled guard, or a reaper/settle race, must not leave a negative
// residue that would silently inflate the next lease's remaining budget.
func (w *WalletService) adjustInteractionTokensLocked(interactionID string, delta int64) {
	if interactionID == "" || delta == 0 {
		return
	}
	if total := w.interactionTokens[interactionID] + delta; total > 0 {
		w.interactionTokens[interactionID] = total
	} else {
		delete(w.interactionTokens, interactionID)
	}
}

// interactionBudgetMessage formats the human-readable reason on an RFC 0030
// Layer 1 denial. The machine-readable discriminator is LeaseDenied.reason;
// this string is for logs and operator-facing surfaces.
func interactionBudgetMessage(interactionID string, spent, estimated, budget int64) string {
	return fmt.Sprintf(
		"interaction %q cost ceiling exceeded: %d spent + %d estimated > %d budget tokens",
		interactionID, spent, estimated, budget)
}
