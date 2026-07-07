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
// The `budget` is resolved by the caller (RFC 0050 amendment — server-side
// resolution via WalletService.resolveInteractionBudget), not read from the
// request: the channel store owns the ceiling, so an agent cannot widen it by
// under-reporting on the lease. Because the snapshot is fixed at interaction open
// (router-side), every lease of one interaction sees the same number; an operator
// editing the channel budget re-bases only the next interaction. The running
// total it is compared against is server-held.
func (w *WalletService) interactionCeilingDenialLocked(req *walletpb.LeaseRequest, estimatedTokens, budget int64) *walletpb.LeaseResponse {
	interactionID := req.GetInteractionId()
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

// SetInteractionBudgetResolver wires the server-side per-interaction cost-ceiling
// resolver (RFC 0050 amendment). The orchestrator calls it once at startup, after
// both the wallet and the channel router exist — the wallet is constructed first
// (no channels dependency), so this is a post-construction setter, not a
// NewWalletService option. A nil fn leaves the legacy request-field behaviour in
// place. Not concurrency-guarded by design: it runs in single-threaded startup
// wiring before the gRPC server accepts the first lease, so the lock-free read in
// AcquireLease is safe.
func (w *WalletService) SetInteractionBudgetResolver(fn func(interactionID string) (int64, bool)) {
	w.interactionBudgetResolver = fn
}

// resolveInteractionBudget returns the effective per-interaction cost ceiling for
// a lease (RFC 0050 amendment — server-side resolution). When the resolver is
// wired (the orchestrator injects one reading the channel router's snapshot) it
// is AUTHORITATIVE: a hit returns the snapshotted channel ceiling; a miss
// (uncapped channel, non-channel / TICK lease, an interaction already retired,
// or one whose first message has not yet committed — the snapshot is taken at
// first commit, router-side, so the lease that PRODUCES an interaction's opening
// message predates its snapshot and resolves uncapped) returns 0 = uncapped,
// deliberately IGNORING any agent-supplied request value —
// the store, not the agent, owns the ceiling. Only with no resolver wired (tests,
// or a wallet built without channels) does it fall back to the request field, the
// legacy pre-amendment behaviour. Lock-free: the resolver field is set once at
// startup before any lease (see SetInteractionBudgetResolver), and the router
// read it performs takes the router's own lock, not w.mu.
func (w *WalletService) resolveInteractionBudget(req *walletpb.LeaseRequest) int64 {
	if w.interactionBudgetResolver == nil {
		return req.GetInteractionBudgetTokens()
	}
	if budget, ok := w.interactionBudgetResolver(req.GetInteractionId()); ok {
		return budget
	}
	return 0
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
// eviction keyed on an actual interaction-closed signal is
// [WalletService.EvictInteraction] (RFC 0052 PR 4a, synthesis_reserve.go); the
// growth is now LIVE, not latent — RFC 0052 makes interaction_budget_tokens
// mandatory on an autonomous channel and the resolver is wired
// (cmd/orchestrator/channels.go), so every autonomous convening tracks a capped
// interaction that settles non-zero spend and leaks one entry. The bounded close
// does NOT yet evict (deferred to RFC 0052 PR 7, where a standing schedule makes
// the leak bite and the schedule timer supplies the settle point EvictInteraction's
// cross-process, fire-and-forget-close-summary precondition needs); until then
// the residue accumulates one entry per convening over the process lifetime.
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
