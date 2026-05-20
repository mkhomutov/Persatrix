// finalize log-shape tests for the WalletService (RFC 0023 PR 7 — review
// follow-ups, from the PR 2 review).
//
// PR 2 review surfaced that the SettleLease / ReleaseLease shared finalize
// path built its log messages by string concatenation —
// `"wallet: " + op + " rejected — unknown lease"` and three siblings —
// baking the settle / release discriminator into the message text. Log
// aggregators group on the message; the four resulting strings each split
// into two unrelated messages, so a dashboard built around message-text
// grouping cannot collapse them. This file pins the constant-message +
// zap.String("op", op) shape PR 7 introduces — the same shape every other
// wallet log line already uses for its discriminators.
package wallet

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"

	"github.com/mkhomutov/persatrix/internal/generated/walletpb"
)

// Message constants the production wallet must emit. The tests pin them as
// constants to make grouping-on-message a regression target — if a future
// edit re-introduces interpolation, the FilterMessage(...) calls below stop
// matching and the dashboard contract fails loudly.
const (
	finalizeMsgFinalized      = "wallet: lease finalized"
	finalizeMsgUnknownLease   = "wallet: finalize rejected — unknown lease"
	finalizeMsgAlreadySettled = "wallet: finalize is a no-op — lease already settled"
	finalizeMsgReconcileMiss  = "wallet: finalize reconcile miss — provisional already cleared"
)

// acquireForFinalizeLog issues a workflow-task lease for the finalize-log
// tests below. The fixture is sized so it neither denies on the budget check
// (well under testCostConfig's per-agent / per-workflow / global limits) nor
// requires per-test tuning — the tests assert on log shape, not on cost.
func acquireForFinalizeLog(t *testing.T, w *WalletService, agentID string) string {
	t.Helper()
	resp, err := w.AcquireLease(testContext(t), &walletpb.LeaseRequest{
		AgentId: agentID, Model: "claude-sonnet",
		EstimatedInputTokens: 1000, EstimatedMaxOutputTokens: 2000,
		Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
	})
	require.NoError(t, err)
	require.NotNil(t, resp.GetGrant())
	return resp.GetGrant().GetLeaseId()
}

// finalizeOps returns the set of unique "op" field values across the given
// observer-captured log entries, failing the test if any entry lacks an
// "op" field — the load-bearing assertion that the discriminator is a
// structured field, not embedded in the message.
func finalizeOps(t *testing.T, entries []observer.LoggedEntry) map[string]struct{} {
	t.Helper()
	ops := map[string]struct{}{}
	for _, e := range entries {
		op, ok := e.ContextMap()["op"]
		require.True(t, ok,
			"finalize log entry %q must carry op as a zap field, not embed it in the message",
			e.Message)
		ops[op.(string)] = struct{}{}
	}
	return ops
}

// TestFinalize_SuccessLogShape pins that the happy-path settle and release
// log entries share the constant `wallet: lease finalized` message and
// carry the settle/release discriminator as a zap.String("op", op) field.
func TestFinalize_SuccessLogShape(t *testing.T) {
	core, logs := observer.New(zap.DebugLevel)
	w, _ := newTestWalletWithLogger(t, testCostConfig(), DefaultConfig(), zap.New(core))

	settleID := acquireForFinalizeLog(t, w, "agent-settle")
	releaseID := acquireForFinalizeLog(t, w, "agent-release")

	ack, err := w.SettleLease(testContext(t), &walletpb.SettlementRequest{
		LeaseId: settleID, ActualInputTokens: 10, ActualOutputTokens: 10,
	})
	require.NoError(t, err)
	require.True(t, ack.GetSuccess())

	ack, err = w.ReleaseLease(testContext(t), &walletpb.ReleaseRequest{
		LeaseId: releaseID, Reason: "provider_error",
	})
	require.NoError(t, err)
	require.True(t, ack.GetSuccess())

	finalized := logs.FilterMessage(finalizeMsgFinalized).All()
	require.Len(t, finalized, 2,
		"settle + release must share the constant %q message", finalizeMsgFinalized)
	assert.Equal(t,
		map[string]struct{}{"settle": {}, "release": {}},
		finalizeOps(t, finalized),
		"each finalized entry must carry its op as a structured field")
}

// TestFinalize_UnknownLeaseLogShape pins the warn-level unknown-lease
// rejection path shares the same constant-message + op-field shape for
// both settle and release.
func TestFinalize_UnknownLeaseLogShape(t *testing.T) {
	core, logs := observer.New(zap.WarnLevel)
	w, _ := newTestWalletWithLogger(t, testCostConfig(), DefaultConfig(), zap.New(core))

	_, err := w.SettleLease(testContext(t), &walletpb.SettlementRequest{LeaseId: "no-such-lease"})
	require.NoError(t, err)
	_, err = w.ReleaseLease(testContext(t), &walletpb.ReleaseRequest{LeaseId: "no-such-lease"})
	require.NoError(t, err)

	rejected := logs.FilterMessage(finalizeMsgUnknownLease).All()
	require.Len(t, rejected, 2,
		"settle + release on an unknown lease must share the constant %q message",
		finalizeMsgUnknownLease)
	assert.Equal(t,
		map[string]struct{}{"settle": {}, "release": {}},
		finalizeOps(t, rejected))
}

// TestFinalize_AlreadySettledLogShape pins the debug-level no-op log on
// the double-settle and settle-then-release paths.
func TestFinalize_AlreadySettledLogShape(t *testing.T) {
	core, logs := observer.New(zap.DebugLevel)
	w, _ := newTestWalletWithLogger(t, testCostConfig(), DefaultConfig(), zap.New(core))

	settleAgainID := acquireForFinalizeLog(t, w, "agent-double-settle")
	releaseAfterID := acquireForFinalizeLog(t, w, "agent-release-after-settle")

	// First settle the two leases so both are in the settled state.
	for _, leaseID := range []string{settleAgainID, releaseAfterID} {
		_, err := w.SettleLease(testContext(t), &walletpb.SettlementRequest{
			LeaseId: leaseID, ActualInputTokens: 10, ActualOutputTokens: 10,
		})
		require.NoError(t, err)
	}

	// Now exercise the already-settled paths: a second settle, and a
	// release after settle. Each must log the same constant message.
	_, err := w.SettleLease(testContext(t), &walletpb.SettlementRequest{
		LeaseId: settleAgainID, ActualInputTokens: 10, ActualOutputTokens: 10,
	})
	require.NoError(t, err)
	_, err = w.ReleaseLease(testContext(t), &walletpb.ReleaseRequest{LeaseId: releaseAfterID})
	require.NoError(t, err)

	noop := logs.FilterMessage(finalizeMsgAlreadySettled).All()
	require.Len(t, noop, 2,
		"double-settle + release-after-settle must each log the constant %q message",
		finalizeMsgAlreadySettled)
	assert.Equal(t,
		map[string]struct{}{"settle": {}, "release": {}},
		finalizeOps(t, noop))
}

// TestFinalize_ReconcileMissLogShape pins the reconcile-miss log emitted
// when ResetDaily clears the provisional out from under a live lease —
// same constant-message + op-field shape.
func TestFinalize_ReconcileMissLogShape(t *testing.T) {
	core, logs := observer.New(zap.WarnLevel)
	w, counter := newTestWalletWithLogger(t, testCostConfig(), DefaultConfig(), zap.New(core))

	leaseID := acquireForFinalizeLog(t, w, "agent-reset")
	counter.ResetDaily()

	ack, err := w.SettleLease(testContext(t), &walletpb.SettlementRequest{
		LeaseId: leaseID, ActualInputTokens: 10, ActualOutputTokens: 10,
	})
	require.NoError(t, err)
	require.True(t, ack.GetSuccess())

	miss := logs.FilterMessage(finalizeMsgReconcileMiss).All()
	require.Len(t, miss, 1,
		"a settle whose provisional was reset must log the constant %q message",
		finalizeMsgReconcileMiss)
	op, ok := miss[0].ContextMap()["op"]
	require.True(t, ok, "the op discriminator must be a zap field, not embedded in the message")
	assert.Equal(t, "settle", op)
}
