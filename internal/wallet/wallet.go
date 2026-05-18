// Package wallet implements the orchestrator-side WalletService — the
// in-line gatekeeper every LLM call acquires a lease from before issuing.
//
// RFC 0023 PR 1 lands the always-grant skeleton: it proves the proto
// contract compiles, the stubs generate, and the servicer registers on the
// orchestrator's gRPC listener. It carries no enforcement semantics —
// AcquireLease always grants, SettleLease / ReleaseLease always succeed.
// Real BudgetEnforcer wiring, provisional charges, and the TTL reaper land
// in PR 2 (RFC 0023 § D); call-site wiring lands in PRs 3–6.
//
// See docs/rfcs/0023-llm-call-leasing.md and docs/rfcs/0023-pr-plan.md.
package wallet

import (
	"context"

	"github.com/oklog/ulid/v2"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/generated/walletpb"
)

// skeletonTTLSeconds is the placeholder lease TTL the PR 1 skeleton stamps on
// every grant. PR 2 replaces it with the configurable `wallet.ttl_seconds`
// key (default 2× the max per-call timeout, capped at 120 s — RFC 0023 Open
// Question §2). It is a positive constant here only so the skeleton already
// exercises a non-zero TTL field on the wire.
const skeletonTTLSeconds int32 = 60

// WalletService is the gRPC WalletService implementation registered on the
// orchestrator's agent-facing gRPC server (the listener that already hosts
// LogService — RFC 0023 Open Question §1).
//
// PR 1 is the inert skeleton: it holds no lease state and composes no
// BudgetEnforcer. PR 2 adds the in-flight lease map, the coarse mutex
// guarding check-then-provision, and the reaper goroutine.
type WalletService struct {
	walletpb.UnimplementedWalletServiceServer

	logger *zap.Logger
}

// NewWalletService constructs a WalletService skeleton. A nil logger is
// replaced with a no-op logger, matching NewLogServiceServer.
func NewWalletService(logger *zap.Logger) *WalletService {
	if logger == nil {
		logger = zap.NewNop()
	}
	return &WalletService{logger: logger}
}

// AcquireLease issues a lease for an LLM call. The PR 1 skeleton always
// grants: it returns a LeaseGrant with a server-issued ULID lease_id, echoes
// the request's token estimates as the granted amounts, and stamps the
// skeleton TTL. PR 2 composes BudgetEnforcer.CheckBudget here and may instead
// return the LeaseDenied arm of the oneof.
func (w *WalletService) AcquireLease(_ context.Context, req *walletpb.LeaseRequest) (*walletpb.LeaseResponse, error) {
	leaseID := ulid.Make().String()
	w.logger.Debug("wallet: lease granted (skeleton — no enforcement)",
		zap.String("lease_id", leaseID),
		zap.String("workflow_id", req.GetWorkflowId()),
		zap.String("agent_id", req.GetAgentId()),
		zap.String("model", req.GetModel()),
		zap.String("cause", req.GetCause().String()),
	)
	return &walletpb.LeaseResponse{
		Outcome: &walletpb.LeaseResponse_Grant{
			Grant: &walletpb.LeaseGrant{
				LeaseId:             leaseID,
				GrantedInputTokens:  req.GetEstimatedInputTokens(),
				GrantedOutputTokens: req.GetEstimatedMaxOutputTokens(),
				TtlSeconds:          skeletonTTLSeconds,
			},
		},
	}, nil
}

// SettleLease records actual usage for a lease. The PR 1 skeleton always
// acknowledges success. PR 2 reconciles the provisional charge against the
// reported actuals and rejects unknown lease IDs.
func (w *WalletService) SettleLease(_ context.Context, req *walletpb.SettlementRequest) (*walletpb.SettlementAck, error) {
	w.logger.Debug("wallet: lease settled (skeleton — no reconciliation)",
		zap.String("lease_id", req.GetLeaseId()),
		zap.Int64("actual_input_tokens", req.GetActualInputTokens()),
		zap.Int64("actual_output_tokens", req.GetActualOutputTokens()),
	)
	return &walletpb.SettlementAck{Success: true}, nil
}

// ReleaseLease reverses a lease whose call did not happen. The PR 1 skeleton
// always acknowledges success. PR 2 reverses the provisional charge and
// rejects unknown lease IDs.
func (w *WalletService) ReleaseLease(_ context.Context, req *walletpb.ReleaseRequest) (*walletpb.SettlementAck, error) {
	w.logger.Debug("wallet: lease released (skeleton — no reversal)",
		zap.String("lease_id", req.GetLeaseId()),
		zap.String("reason", req.GetReason()),
	)
	return &walletpb.SettlementAck{Success: true}, nil
}
