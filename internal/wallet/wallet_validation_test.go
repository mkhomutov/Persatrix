// Token-count validation tests for the WalletService (RFC 0023 PR 2 —
// Security Considerations). AcquireLease and SettleLease range-check the
// agent-supplied int64 token fields at the RPC boundary; an out-of-range
// value is a malformed request, rejected with codes.InvalidArgument and
// kept clear of the cost counter. Shared fixtures live in wallet_test.go.
package wallet

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/mkhomutov/persatrix/internal/generated/walletpb"
)

// TestAcquireLease_RejectsInvalidTokenEstimates pins that AcquireLease
// rejects an agent-supplied token estimate outside [0, maxTokenCount] with
// codes.InvalidArgument, before any budget check or provisional charge.
//
// A negative estimate is the load-bearing case: cost.EstimateCost is
// unclamped arithmetic, so a negative count yields a negative charge that
// RecordProvisional would *subtract* from every budget scope — silently
// freeing budget and defeating the enforcement the wallet exists to apply
// (RFC 0023 Security Considerations). An oversized estimate is the mirror
// DoS. A malformed request is a gRPC error, distinct from the in-band
// LeaseDenied arm a budget denial returns.
func TestAcquireLease_RejectsInvalidTokenEstimates(t *testing.T) {
	tests := []struct {
		name    string
		req     *walletpb.LeaseRequest
		wantArg string
	}{
		{
			name: "negative input estimate",
			req: &walletpb.LeaseRequest{
				AgentId: "agent-a", Model: "claude-sonnet",
				EstimatedInputTokens: -1, EstimatedMaxOutputTokens: 100,
				Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
			},
			wantArg: "estimated_input_tokens",
		},
		{
			name: "negative output estimate",
			req: &walletpb.LeaseRequest{
				AgentId: "agent-a", Model: "claude-sonnet",
				EstimatedInputTokens: 100, EstimatedMaxOutputTokens: -1_000_000,
				Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
			},
			wantArg: "estimated_max_output_tokens",
		},
		{
			name: "oversized input estimate",
			req: &walletpb.LeaseRequest{
				AgentId: "agent-a", Model: "claude-sonnet",
				EstimatedInputTokens: maxTokenCount + 1, EstimatedMaxOutputTokens: 100,
				Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
			},
			wantArg: "estimated_input_tokens",
		},
		{
			name: "oversized output estimate",
			req: &walletpb.LeaseRequest{
				AgentId: "agent-a", Model: "claude-sonnet",
				EstimatedInputTokens: 100, EstimatedMaxOutputTokens: maxTokenCount + 1,
				Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
			},
			wantArg: "estimated_max_output_tokens",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			w, counter := newTestWallet(t, testCostConfig(), DefaultConfig())

			resp, err := w.AcquireLease(testContext(t), tc.req)
			require.Error(t, err, "a malformed token estimate must be rejected")
			assert.Nil(t, resp)
			assert.Equal(t, codes.InvalidArgument, status.Code(err))
			assert.Contains(t, status.Convert(err).Message(), tc.wantArg,
				"the error must name the offending field")

			// The rejected request must not have touched the budget counter
			// — in particular it must not have recorded a negative charge.
			in, out, usd := counter.GlobalUsage()
			assert.Equal(t, int64(0), in)
			assert.Equal(t, int64(0), out)
			assert.InDelta(t, 0.0, usd, 1e-9, "a rejected acquire must not charge the counter")
		})
	}
}

// TestAcquireLease_AcceptsBoundaryTokenEstimates pins that the validation
// bound is inclusive: an estimate of exactly maxTokenCount passes validation
// and reaches the budget check — here it trips the budget and returns the
// in-band Denied arm, not the codes.InvalidArgument a malformed request
// gets. It guards against an off-by-one (n >= maxTokenCount) in the bound.
// The lower bound 0 is exercised by the deny-scope tests in wallet_test.go.
func TestAcquireLease_AcceptsBoundaryTokenEstimates(t *testing.T) {
	w, _ := newTestWallet(t, testCostConfig(), DefaultConfig())

	resp, err := w.AcquireLease(testContext(t), &walletpb.LeaseRequest{
		AgentId: "agent-a", Model: "claude-sonnet",
		EstimatedInputTokens: maxTokenCount, EstimatedMaxOutputTokens: maxTokenCount,
		Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
	})
	require.NoError(t, err, "exactly maxTokenCount must pass validation, not error")
	assert.NotNil(t, resp.GetDenied(),
		"maxTokenCount tokens exceed the test budget — a budget denial, not a validation error")
}

// TestSettleLease_RejectsInvalidActuals pins that SettleLease rejects a
// negative or out-of-range actual token count with codes.InvalidArgument
// before reconciling. cost.Reconcile is unclamped arithmetic — a negative
// actual applies a negative delta to every budget scope, an oversized one a
// runaway positive delta. The lease is left unsettled so a corrected retry
// can still settle it (RFC 0023 Security Considerations).
func TestSettleLease_RejectsInvalidActuals(t *testing.T) {
	tests := []struct {
		name      string
		actualIn  int64
		actualOut int64
		wantArg   string
	}{
		{"negative input", -1, 100, "actual_input_tokens"},
		{"negative output", 100, -500_000, "actual_output_tokens"},
		{"oversized input", maxTokenCount + 1, 100, "actual_input_tokens"},
		{"oversized output", 100, maxTokenCount + 1, "actual_output_tokens"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			w, counter := newTestWallet(t, testCostConfig(), DefaultConfig())

			resp, err := w.AcquireLease(testContext(t), &walletpb.LeaseRequest{
				AgentId: "agent-a", Model: "claude-sonnet",
				EstimatedInputTokens: 1000, EstimatedMaxOutputTokens: 2000,
				Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
			})
			require.NoError(t, err)
			leaseID := resp.GetGrant().GetLeaseId()
			_, _, usdProvisional := counter.GlobalUsage()

			ack, err := w.SettleLease(testContext(t), &walletpb.SettlementRequest{
				LeaseId: leaseID, ActualInputTokens: tc.actualIn, ActualOutputTokens: tc.actualOut,
			})
			require.Error(t, err, "a malformed actual count must be rejected")
			assert.Nil(t, ack)
			assert.Equal(t, codes.InvalidArgument, status.Code(err))
			assert.Contains(t, status.Convert(err).Message(), tc.wantArg,
				"the error must name the offending field")

			// The rejected settle must not reconcile: the provisional charge
			// stands untouched and the lease stays settleable.
			_, _, usdAfter := counter.GlobalUsage()
			assert.InDelta(t, usdProvisional, usdAfter, 1e-9,
				"a rejected settle must not perturb the budget counter")
			settled, exists := leaseState(w, leaseID)
			require.True(t, exists, "the lease must still be tracked")
			assert.False(t, settled, "a rejected settle must leave the lease unsettled for retry")

			// A corrected settle still succeeds.
			ack, err = w.SettleLease(testContext(t), &walletpb.SettlementRequest{
				LeaseId: leaseID, ActualInputTokens: 800, ActualOutputTokens: 1500,
			})
			require.NoError(t, err)
			assert.True(t, ack.GetSuccess(), "a corrected settle after a rejected one must succeed")
		})
	}
}
