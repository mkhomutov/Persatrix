// TTL reaper tests for the WalletService (RFC 0023 PR 2 § B / § D / § F).
// Shared fixtures live in wallet_test.go.
package wallet

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/mkhomutov/persatrix/internal/generated/walletpb"
)

// TestReaper_SettlesExpiredLeaseAtGranted pins the core reaper contract: a
// lease left unsettled past its TTL is settled at the granted (worst-case)
// amount — an agent crash mid-call must not free budget.
func TestReaper_SettlesExpiredLeaseAtGranted(t *testing.T) {
	walletCfg := Config{TTL: 30 * time.Second, ReaperInterval: time.Hour, MaxActiveLeases: 16}
	w, counter := newTestWallet(t, testCostConfig(), walletCfg)

	resp, err := w.AcquireLease(testContext(t), &walletpb.LeaseRequest{
		AgentId: "agent-a", Model: "claude-sonnet",
		EstimatedInputTokens: 1000, EstimatedMaxOutputTokens: 2000,
		Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
	})
	require.NoError(t, err)
	leaseID := resp.GetGrant().GetLeaseId()

	// Drive the reaper past the lease's TTL.
	w.reapExpired(time.Now().Add(walletCfg.TTL + time.Second))

	settled, exists := leaseState(w, leaseID)
	require.True(t, exists)
	assert.True(t, settled, "the reaper must settle a lease past its TTL")

	// Settled at the granted amount — the provisional estimate stands.
	_, _, usd := counter.GlobalUsage()
	assert.InDelta(t, 0.033, usd, 1e-9, "reaped lease keeps its worst-case charge")
}

// TestReaper_Idempotent pins that re-running the reaper over an
// already-settled lease is a no-op — it must not reconcile (and so charge)
// a second time.
func TestReaper_Idempotent(t *testing.T) {
	walletCfg := Config{TTL: 30 * time.Second, ReaperInterval: time.Hour, MaxActiveLeases: 16}
	w, counter := newTestWallet(t, testCostConfig(), walletCfg)

	_, err := w.AcquireLease(testContext(t), &walletpb.LeaseRequest{
		AgentId: "agent-a", Model: "claude-sonnet",
		EstimatedInputTokens: 1000, EstimatedMaxOutputTokens: 2000,
		Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
	})
	require.NoError(t, err)

	future := time.Now().Add(walletCfg.TTL + time.Second)
	w.reapExpired(future)
	_, _, usdAfterFirst := counter.GlobalUsage()
	w.reapExpired(future)
	_, _, usdAfterSecond := counter.GlobalUsage()

	assert.InDelta(t, usdAfterFirst, usdAfterSecond, 1e-9,
		"a second reaper pass must not re-charge an already-reaped lease")
}

// TestReaper_LateSettleAfterReapIsNoop pins RFC 0023 § F: a Settle arriving
// after the reaper already closed the lease is a monotone-safe no-op — the
// reaper-applied granted charge stands and is not adjusted to the actuals.
func TestReaper_LateSettleAfterReapIsNoop(t *testing.T) {
	walletCfg := Config{TTL: 30 * time.Second, ReaperInterval: time.Hour, MaxActiveLeases: 16}
	w, counter := newTestWallet(t, testCostConfig(), walletCfg)

	resp, err := w.AcquireLease(testContext(t), &walletpb.LeaseRequest{
		AgentId: "agent-a", Model: "claude-sonnet",
		EstimatedInputTokens: 1000, EstimatedMaxOutputTokens: 2000,
		Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
	})
	require.NoError(t, err)
	leaseID := resp.GetGrant().GetLeaseId()

	w.reapExpired(time.Now().Add(walletCfg.TTL + time.Second))

	// A late settle reporting lower actuals than the granted amount.
	ack, err := w.SettleLease(testContext(t), &walletpb.SettlementRequest{
		LeaseId: leaseID, ActualInputTokens: 10, ActualOutputTokens: 10,
	})
	require.NoError(t, err)
	assert.True(t, ack.GetSuccess(), "a late settle after reap succeeds")
	assert.Contains(t, ack.GetErrorMessage(), "noop")

	// The reaper-applied granted charge (0.033) stands, not the actuals.
	_, _, usd := counter.GlobalUsage()
	assert.InDelta(t, 0.033, usd, 1e-9, "the reaper charge is not revised downward")
}

// TestReaper_PurgesLongClosedLeases pins that the reaper drops leases closed
// long enough ago that a retrying agent's late settle is no longer expected
// — without the purge the in-flight map would grow unbounded.
func TestReaper_PurgesLongClosedLeases(t *testing.T) {
	walletCfg := Config{TTL: 30 * time.Second, ReaperInterval: time.Hour, MaxActiveLeases: 16}
	w, _ := newTestWallet(t, testCostConfig(), walletCfg)

	resp, err := w.AcquireLease(testContext(t), &walletpb.LeaseRequest{
		AgentId: "agent-a", Model: "claude-sonnet",
		EstimatedInputTokens: 100, EstimatedMaxOutputTokens: 100,
		Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
	})
	require.NoError(t, err)
	leaseID := resp.GetGrant().GetLeaseId()

	ack, err := w.SettleLease(testContext(t), &walletpb.SettlementRequest{
		LeaseId: leaseID, ActualInputTokens: 100, ActualOutputTokens: 100,
	})
	require.NoError(t, err)
	require.True(t, ack.GetSuccess())

	// Still tracked just after settlement (inside the late-settle window).
	_, exists := leaseState(w, leaseID)
	require.True(t, exists)

	// Long past the purge horizon, the closed lease is dropped.
	w.reapExpired(time.Now().Add(2*walletCfg.TTL + time.Second))
	_, exists = leaseState(w, leaseID)
	assert.False(t, exists, "a long-closed lease must be purged from the in-flight map")
}

// TestReaper_FreesConcurrencyCapSlot pins that a reaped lease no longer
// counts toward the per-agent concurrency cap: settling a lease on TTL
// expiry marks it settled, and activeLeasesForLocked counts only unsettled
// leases. Reaping keeps the spend (the granted charge stands) but must not
// also permanently consume the agent's DoS-ceiling slots — an agent whose
// leases were reaped after a crash is not locked out of acquiring again.
func TestReaper_FreesConcurrencyCapSlot(t *testing.T) {
	walletCfg := Config{TTL: 30 * time.Second, ReaperInterval: time.Hour, MaxActiveLeases: 2}
	w, _ := newTestWallet(t, testCostConfig(), walletCfg)

	req := &walletpb.LeaseRequest{
		AgentId: "agent-crashed", Model: "claude-sonnet",
		EstimatedInputTokens: 10, EstimatedMaxOutputTokens: 10,
		Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
	}

	// Fill the agent's cap with leases it then abandons.
	for range walletCfg.MaxActiveLeases {
		resp, err := w.AcquireLease(testContext(t), req)
		require.NoError(t, err)
		require.NotNil(t, resp.GetGrant())
	}

	// At the cap, the next acquisition is rejected.
	_, err := w.AcquireLease(testContext(t), req)
	require.Error(t, err, "acquisition at the cap must be rejected")
	assert.Equal(t, codes.ResourceExhausted, status.Code(err))

	// The reaper settles the abandoned leases past their TTL.
	w.reapExpired(time.Now().Add(walletCfg.TTL + time.Second))

	// Reaping freed the slots — the agent may acquire again.
	resp, err := w.AcquireLease(testContext(t), req)
	require.NoError(t, err, "a reaped lease must free a per-agent cap slot")
	assert.NotNil(t, resp.GetGrant())
}

// --- Reaper goroutine ---

// TestGuardReap_RecoversPanic pins ISSUE-0059 piece 2: a panic in a reaper
// pass is recovered and logged rather than escaping the goroutine — a gRPC
// server interceptor never wraps a background goroutine.
func TestGuardReap_RecoversPanic(t *testing.T) {
	core, logs := observer.New(zap.ErrorLevel)
	logger := zap.New(core)

	// Must not propagate the panic — reaching the assertions proves it.
	guardReap(logger, func() { panic("reaper exploded") })

	recovered := logs.FilterMessage("wallet: reaper pass panicked, recovered")
	assert.Equal(t, 1, recovered.Len(), "the recovered panic must be logged")
}

// TestRunReaper_StopsOnContextCancel pins that the reaper goroutine exits
// promptly when its context is cancelled.
func TestRunReaper_StopsOnContextCancel(t *testing.T) {
	walletCfg := Config{TTL: time.Hour, ReaperInterval: time.Hour, MaxActiveLeases: 16}
	w, _ := newTestWallet(t, testCostConfig(), walletCfg)

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		w.RunReaper(ctx)
		close(done)
	}()

	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("RunReaper did not return after context cancellation")
	}
}

// TestRunReaper_ReapsExpiredLease pins the live goroutine path: a lease
// abandoned past a short TTL is settled by the running reaper loop.
func TestRunReaper_ReapsExpiredLease(t *testing.T) {
	walletCfg := Config{
		TTL:             10 * time.Millisecond,
		ReaperInterval:  10 * time.Millisecond,
		MaxActiveLeases: 16,
	}
	w, _ := newTestWallet(t, testCostConfig(), walletCfg)

	resp, err := w.AcquireLease(testContext(t), &walletpb.LeaseRequest{
		AgentId: "agent-a", Model: "claude-sonnet",
		EstimatedInputTokens: 100, EstimatedMaxOutputTokens: 100,
		Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
	})
	require.NoError(t, err)
	leaseID := resp.GetGrant().GetLeaseId()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go w.RunReaper(ctx)

	require.Eventually(t, func() bool {
		settled, exists := leaseState(w, leaseID)
		return exists && settled
	}, 2*time.Second, 10*time.Millisecond, "the running reaper must settle the expired lease")
}
