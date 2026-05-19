// SettleLease / ReleaseLease tests for the WalletService (RFC 0023 PR 2)
// plus the end-to-end gRPC wire surface. Shared fixtures live in
// wallet_test.go; the reaper lives in wallet_reaper_test.go; the
// token-count validation in wallet_validation_test.go.
package wallet

import (
	"testing"

	"github.com/oklog/ulid/v2"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/generated/walletpb"
)

// TestSettleLease_ReconcilesActuals pins that SettleLease replaces the
// provisional charge with the provider-reported actuals.
func TestSettleLease_ReconcilesActuals(t *testing.T) {
	w, counter := newTestWallet(t, testCostConfig(), DefaultConfig())

	resp, err := w.AcquireLease(testContext(t), &walletpb.LeaseRequest{
		AgentId: "agent-a", Model: "claude-sonnet",
		EstimatedInputTokens: 1000, EstimatedMaxOutputTokens: 2000,
		Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
	})
	require.NoError(t, err)
	leaseID := resp.GetGrant().GetLeaseId()

	ack, err := w.SettleLease(testContext(t), &walletpb.SettlementRequest{
		LeaseId: leaseID, ActualInputTokens: 800, ActualOutputTokens: 1500,
	})
	require.NoError(t, err)
	assert.True(t, ack.GetSuccess())

	// Totals reflect the actuals: 800/1M*3 + 1500/1M*15 = 0.0249.
	in, out, usd := counter.GlobalUsage()
	assert.Equal(t, int64(800), in)
	assert.Equal(t, int64(1500), out)
	assert.InDelta(t, 0.0249, usd, 1e-9)
}

// TestReleaseLease_ReversesProvisional pins that ReleaseLease fully reverses
// the provisional charge — the call never happened, so no spend lands.
func TestReleaseLease_ReversesProvisional(t *testing.T) {
	w, counter := newTestWallet(t, testCostConfig(), DefaultConfig())

	resp, err := w.AcquireLease(testContext(t), &walletpb.LeaseRequest{
		AgentId: "agent-a", Model: "claude-sonnet",
		EstimatedInputTokens: 1000, EstimatedMaxOutputTokens: 2000,
		Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
	})
	require.NoError(t, err)
	leaseID := resp.GetGrant().GetLeaseId()

	ack, err := w.ReleaseLease(testContext(t), &walletpb.ReleaseRequest{
		LeaseId: leaseID, Reason: "provider_error",
	})
	require.NoError(t, err)
	assert.True(t, ack.GetSuccess())

	_, _, usd := counter.GlobalUsage()
	assert.InDelta(t, 0.0, usd, 1e-9, "a released lease must leave no spend")
}

// TestSettleLease_UnknownLeaseRejected pins that settling a lease_id the
// wallet never issued fails — the wallet rejects unknown IDs.
func TestSettleLease_UnknownLeaseRejected(t *testing.T) {
	w, _ := newTestWallet(t, testCostConfig(), DefaultConfig())

	for _, leaseID := range []string{ulid.Make().String(), "not-a-real-lease", ""} {
		ack, err := w.SettleLease(testContext(t), &walletpb.SettlementRequest{LeaseId: leaseID})
		require.NoError(t, err)
		assert.False(t, ack.GetSuccess(), "unknown lease_id %q must not settle", leaseID)
		assert.Contains(t, ack.GetErrorMessage(), "unknown lease")
	}
}

// TestSettleLease_DoubleSettleIsNoop pins replay safety: settling an
// already-settled lease returns success with a noop indicator rather than
// reconciling — and so charging — a second time.
func TestSettleLease_DoubleSettleIsNoop(t *testing.T) {
	w, counter := newTestWallet(t, testCostConfig(), DefaultConfig())

	resp, err := w.AcquireLease(testContext(t), &walletpb.LeaseRequest{
		AgentId: "agent-a", Model: "claude-sonnet",
		EstimatedInputTokens: 1000, EstimatedMaxOutputTokens: 2000,
		Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
	})
	require.NoError(t, err)
	leaseID := resp.GetGrant().GetLeaseId()

	settle := &walletpb.SettlementRequest{
		LeaseId: leaseID, ActualInputTokens: 800, ActualOutputTokens: 1500,
	}
	ack, err := w.SettleLease(testContext(t), settle)
	require.NoError(t, err)
	require.True(t, ack.GetSuccess())
	_, _, usdAfterFirst := counter.GlobalUsage()

	ack, err = w.SettleLease(testContext(t), settle)
	require.NoError(t, err)
	assert.True(t, ack.GetSuccess(), "a double settle is a successful no-op")
	assert.Contains(t, ack.GetErrorMessage(), "noop")

	_, _, usdAfterSecond := counter.GlobalUsage()
	assert.InDelta(t, usdAfterFirst, usdAfterSecond, 1e-9,
		"a double settle must not reconcile the lease twice")
}

// TestSettleLease_AfterResetDailyIsNoop pins the reconcile-miss path: a
// daily counter reset clears the provisional charge out from under a live
// lease, so its later Settle finds nothing to reconcile. The wallet treats
// that as a benign no-op rather than a failure.
func TestSettleLease_AfterResetDailyIsNoop(t *testing.T) {
	w, counter := newTestWallet(t, testCostConfig(), DefaultConfig())

	resp, err := w.AcquireLease(testContext(t), &walletpb.LeaseRequest{
		AgentId: "agent-a", Model: "claude-sonnet",
		EstimatedInputTokens: 1000, EstimatedMaxOutputTokens: 2000,
		Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
	})
	require.NoError(t, err)
	leaseID := resp.GetGrant().GetLeaseId()

	// A midnight reset drops the provisional while the lease is in flight.
	counter.ResetDaily()

	ack, err := w.SettleLease(testContext(t), &walletpb.SettlementRequest{
		LeaseId: leaseID, ActualInputTokens: 800, ActualOutputTokens: 1500,
	})
	require.NoError(t, err)
	assert.True(t, ack.GetSuccess(), "a settle after the provisional was reset is a no-op success")
	assert.Contains(t, ack.GetErrorMessage(), "noop")
}

// TestWalletService_BufconnRoundTrip pins that the real servicer still
// satisfies the gRPC contract end-to-end over a connection: acquire then
// settle through a generated client.
func TestWalletService_BufconnRoundTrip(t *testing.T) {
	w, _ := newTestWallet(t, testCostConfig(), DefaultConfig())
	client := startBufconnWalletService(t, w)

	resp, err := client.AcquireLease(testContext(t), &walletpb.LeaseRequest{
		AgentId: "agent-a", Model: "claude-sonnet",
		EstimatedInputTokens: 1000, EstimatedMaxOutputTokens: 2000,
		Cause: walletpb.Cause_CAUSE_SUB_AGENT,
	})
	require.NoError(t, err)
	leaseID := resp.GetGrant().GetLeaseId()
	require.NotEmpty(t, leaseID)

	ack, err := client.SettleLease(testContext(t), &walletpb.SettlementRequest{
		LeaseId: leaseID, ActualInputTokens: 900, ActualOutputTokens: 1800,
	})
	require.NoError(t, err)
	assert.True(t, ack.GetSuccess())
}
