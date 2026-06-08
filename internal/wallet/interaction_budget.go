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
// close. A no-op for the untracked (empty interaction_id) case.
func (w *WalletService) recordInteractionGrantLocked(interactionID string, estimatedTokens int64) {
	if interactionID == "" {
		return
	}
	w.interactionTokens[interactionID] += estimatedTokens
}

// adjustInteractionTokensLocked applies delta to interactionID's running
// total and prunes the entry once it falls to zero or below, so a fully-
// released interaction leaves no residue in the map. A no-op for an empty id
// or a zero delta.
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
