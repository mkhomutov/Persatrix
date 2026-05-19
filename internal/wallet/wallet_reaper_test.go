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

// TestReaper_ReapedLeaseLogCarriesContext pins that the reaper's one-shot
// "lease reaped" WARN record carries the abandoned lease's workflow and
// model, not just its agent and cause. A reaped lease is an abnormal
// lifecycle event — an agent left a lease unsettled past its TTL, typically
// a crash or hang — so an operator triaging it needs the workflow and model
// in that same always-on record; the "lease granted" line that also carries
// them is DEBUG, off in production.
func TestReaper_ReapedLeaseLogCarriesContext(t *testing.T) {
	walletCfg := Config{TTL: 30 * time.Second, ReaperInterval: time.Hour, MaxActiveLeases: 16}
	core, logs := observer.New(zap.WarnLevel)
	w, _ := newTestWalletWithLogger(t, testCostConfig(), walletCfg, zap.New(core))

	_, err := w.AcquireLease(testContext(t), &walletpb.LeaseRequest{
		WorkflowId: "wf-reaped", AgentId: "agent-a", Model: "claude-sonnet",
		EstimatedInputTokens: 1000, EstimatedMaxOutputTokens: 2000,
		Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
	})
	require.NoError(t, err)

	w.reapExpired(time.Now().Add(walletCfg.TTL + time.Second))

	reaped := logs.FilterMessage("wallet: lease reaped — settled at granted amount on TTL expiry")
	require.Equal(t, 1, reaped.Len(), "the reaper must log the reaped lease exactly once")
	fields := reaped.All()[0].ContextMap()
	assert.Equal(t, "wf-reaped", fields["workflow_id"], "reaped-lease log must carry the workflow id")
	assert.Equal(t, "claude-sonnet", fields["model"], "reaped-lease log must carry the model")
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

// TestReaper_ReconcileMissAfterResetDailyStillSettles pins the reaper's
// reconcile-miss branch: a midnight ResetDaily clears a live lease's
// provisional charge out from under it, so when the reaper later expires
// that lease there is no provisional left to reconcile. The reaper must
// still mark the lease settled — otherwise it would re-reap the same lease
// on every pass forever — log the miss for the operator, and resurrect no
// spend onto the freshly-reset counter.
func TestReaper_ReconcileMissAfterResetDailyStillSettles(t *testing.T) {
	walletCfg := Config{TTL: 30 * time.Second, ReaperInterval: time.Hour, MaxActiveLeases: 16}
	core, logs := observer.New(zap.WarnLevel)
	w, counter := newTestWalletWithLogger(t, testCostConfig(), walletCfg, zap.New(core))

	resp, err := w.AcquireLease(testContext(t), &walletpb.LeaseRequest{
		WorkflowId: "wf-1", AgentId: "agent-a", Model: "claude-sonnet",
		EstimatedInputTokens: 1000, EstimatedMaxOutputTokens: 2000,
		Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
	})
	require.NoError(t, err)
	leaseID := resp.GetGrant().GetLeaseId()

	// A midnight reset drops the provisional while the lease is in flight.
	counter.ResetDaily()

	// The reaper expires the lease; its Reconcile finds no provisional.
	w.reapExpired(time.Now().Add(walletCfg.TTL + time.Second))

	settled, exists := leaseState(w, leaseID)
	require.True(t, exists, "a reaped lease stays tracked until the purge horizon")
	assert.True(t, settled,
		"the reaper must mark the lease settled even when its provisional was already cleared")
	assert.Equal(t, 1, logs.FilterMessage("wallet: reaper reconcile miss").Len(),
		"a reconcile miss must be logged for the operator")

	// The freshly-reset counter is not driven negative or otherwise perturbed.
	in, out, usd := counter.GlobalUsage()
	assert.Equal(t, int64(0), in)
	assert.Equal(t, int64(0), out)
	assert.InDelta(t, 0.0, usd, 1e-9, "a reconcile miss must resurrect no spend")

	// A later pass is a no-op: the lease is settled, not re-reaped.
	w.reapExpired(time.Now().Add(walletCfg.TTL + time.Second))
	assert.Equal(t, 1, logs.FilterMessage("wallet: reaper reconcile miss").Len(),
		"the settled lease must not be re-reaped on a later pass")
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
//
// The assertion targets a terminal, purge-immune signal — the reaper's
// one-shot "lease reaped" log record — rather than the lease's transient
// settled-and-still-present map state. The reaper has two stages keyed off
// issuedAt: it settles an expired lease at issuedAt+TTL and purges it at
// issuedAt+2*TTL, so settled-and-present holds for only ~one TTL. Polling
// for that window races it — the poll phase can consistently sample either
// side of it — whereas the log record is emitted exactly once, when the
// lease is settled, and persists in the observer after the purge.
func TestRunReaper_ReapsExpiredLease(t *testing.T) {
	walletCfg := Config{
		TTL:             10 * time.Millisecond,
		ReaperInterval:  10 * time.Millisecond,
		MaxActiveLeases: 16,
	}
	core, logs := observer.New(zap.WarnLevel)
	w, _ := newTestWalletWithLogger(t, testCostConfig(), walletCfg, zap.New(core))

	resp, err := w.AcquireLease(testContext(t), &walletpb.LeaseRequest{
		AgentId: "agent-a", Model: "claude-sonnet",
		EstimatedInputTokens: 100, EstimatedMaxOutputTokens: 100,
		Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
	})
	require.NoError(t, err)
	require.NotNil(t, resp.GetGrant())

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go w.RunReaper(ctx)

	require.Eventually(t, func() bool {
		return logs.FilterMessage("wallet: lease reaped — settled at granted amount on TTL expiry").Len() == 1
	}, 2*time.Second, 10*time.Millisecond, "the running reaper must settle the expired lease")
}
