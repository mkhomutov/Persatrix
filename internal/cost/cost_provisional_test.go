package cost

import (
	"fmt"
	"sync"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// RFC 0023 PR 2 — provisional-charge / reconcile semantics on TokenCounter.
//
// AcquireLease records a worst-case provisional charge against all three
// scopes; SettleLease / ReleaseLease later reconcile that provisional with
// the actual usage the provider reported. These tests pin the primitives
// the WalletService composes — they exercise TokenCounter in isolation.

// TestRecordProvisional_AppliesToAllScopes pins that a provisional charge
// lands on the global, per-workflow, and per-agent totals exactly like a
// settled RecordUsage would — the wallet must be able to deny a second
// lease on the strength of the first lease's still-provisional spend.
func TestRecordProvisional_AppliesToAllScopes(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())

	tc.RecordProvisional("lease-1", UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 2000,
	})

	// claude-sonnet: 1000/1M*3.00 + 2000/1M*15.00 = 0.003 + 0.030 = 0.033
	const wantUSD = 0.033
	assertScope := func(name string, in, out int64, usd float64) {
		t.Helper()
		assert.Equal(t, int64(1000), in, "%s input tokens", name)
		assert.Equal(t, int64(2000), out, "%s output tokens", name)
		assert.InDelta(t, wantUSD, usd, 1e-9, "%s estimated USD", name)
	}

	wIn, wOut, wUSD := tc.WorkflowUsage("wf-1")
	assertScope("workflow", wIn, wOut, wUSD)
	aIn, aOut, aUSD := tc.AgentUsage("agent-a")
	assertScope("agent", aIn, aOut, aUSD)
	gIn, gOut, gUSD := tc.GlobalUsage()
	assertScope("global", gIn, gOut, gUSD)
}

// TestReconcile_ReplacesProvisionalWithActual pins the settle path: after
// Reconcile the three scopes reflect the provider-reported actuals, not the
// worst-case estimate. The delta (here negative — the call came in under
// estimate) is applied atomically to every scope.
func TestReconcile_ReplacesProvisionalWithActual(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())

	tc.RecordProvisional("lease-1", UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 2000,
	})

	require.NoError(t, tc.Reconcile("lease-1", 800, 1500))

	// actual claude-sonnet: 800/1M*3.00 + 1500/1M*15.00 = 0.0024 + 0.0225 = 0.0249
	const wantUSD = 0.0249
	in, out, usd := tc.GlobalUsage()
	assert.Equal(t, int64(800), in)
	assert.Equal(t, int64(1500), out)
	assert.InDelta(t, wantUSD, usd, 1e-9)

	wIn, wOut, wUSD := tc.WorkflowUsage("wf-1")
	assert.Equal(t, int64(800), wIn)
	assert.Equal(t, int64(1500), wOut)
	assert.InDelta(t, wantUSD, wUSD, 1e-9)

	aIn, aOut, aUSD := tc.AgentUsage("agent-a")
	assert.Equal(t, int64(800), aIn)
	assert.Equal(t, int64(1500), aOut)
	assert.InDelta(t, wantUSD, aUSD, 1e-9)
}

// TestReconcile_OverEstimateAppliesPositiveDelta pins the rarer over-run
// case: the actual usage exceeds the estimate, so Reconcile applies a
// positive delta and the totals end higher than the provisional charge.
func TestReconcile_OverEstimateAppliesPositiveDelta(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())

	tc.RecordProvisional("lease-1", UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-haiku",
		InputTokens: 100, OutputTokens: 100,
	})
	require.NoError(t, tc.Reconcile("lease-1", 500, 900))

	// actual claude-haiku: 500/1M*0.80 + 900/1M*4.00 = 0.0004 + 0.0036 = 0.0040
	in, out, usd := tc.GlobalUsage()
	assert.Equal(t, int64(500), in)
	assert.Equal(t, int64(900), out)
	assert.InDelta(t, 0.0040, usd, 1e-9)
}

// TestReconcile_ReleaseReversesProvisional pins Release semantics: a
// Reconcile with zero actuals (ReleaseLease — the call never happened)
// fully reverses the provisional charge, leaving every scope back at zero.
func TestReconcile_ReleaseReversesProvisional(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())

	tc.RecordProvisional("lease-1", UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 2000,
	})
	require.NoError(t, tc.Reconcile("lease-1", 0, 0))

	in, out, usd := tc.GlobalUsage()
	assert.Equal(t, int64(0), in)
	assert.Equal(t, int64(0), out)
	assert.InDelta(t, 0.0, usd, 1e-9)
}

// TestReconcile_DoubleReconcileRejected pins idempotency safety: a lease can
// be reconciled exactly once. The second Reconcile must error rather than
// apply the delta a second time — the WalletService relies on this so a
// late Settle racing the reaper cannot double-charge.
func TestReconcile_DoubleReconcileRejected(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())

	tc.RecordProvisional("lease-1", UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 2000,
	})
	require.NoError(t, tc.Reconcile("lease-1", 800, 1500))

	err := tc.Reconcile("lease-1", 800, 1500)
	require.Error(t, err, "second Reconcile of the same lease must be rejected")
	assert.Contains(t, err.Error(), "lease-1")

	// The rejected second Reconcile must not perturb the totals.
	in, out, _ := tc.GlobalUsage()
	assert.Equal(t, int64(800), in)
	assert.Equal(t, int64(1500), out)
}

// TestReconcile_UnknownLeaseRejected pins that reconciling a lease that was
// never provisioned errors — there is no provisional charge to replace.
func TestReconcile_UnknownLeaseRejected(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())

	err := tc.Reconcile("never-provisioned", 100, 200)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "never-provisioned")
}

// TestRecordProvisional_ResetDailyClearsProvisionals pins that ResetDaily
// clears outstanding provisional charges along with the scope totals — a
// reconcile after the daily reset then surfaces as an unknown lease rather
// than driving a scope total negative.
func TestRecordProvisional_ResetDailyClearsProvisionals(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())

	tc.RecordProvisional("lease-1", UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 2000,
	})
	tc.ResetDaily()

	err := tc.Reconcile("lease-1", 800, 1500)
	require.Error(t, err, "ResetDaily must drop the provisional so a stale reconcile is rejected")

	in, out, usd := tc.GlobalUsage()
	assert.Equal(t, int64(0), in)
	assert.Equal(t, int64(0), out)
	assert.InDelta(t, 0.0, usd, 1e-9)
}

// TestRecordProvisional_Reconcile_Concurrent is the race-detector guard:
// many goroutines provision and reconcile distinct leases concurrently;
// the run must be race-clean and the final totals must net to zero once
// every lease has been released.
func TestRecordProvisional_Reconcile_Concurrent(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())

	const leases = 100
	var wg sync.WaitGroup
	wg.Add(leases)
	for i := range leases {
		go func(i int) {
			defer wg.Done()
			id := fmt.Sprintf("lease-%d", i)
			tc.RecordProvisional(id, UsageRecord{
				WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
				InputTokens: 1000, OutputTokens: 2000,
			})
			// Release every lease — net effect must be zero.
			_ = tc.Reconcile(id, 0, 0)
		}(i)
	}
	wg.Wait()

	in, out, usd := tc.GlobalUsage()
	assert.Equal(t, int64(0), in)
	assert.Equal(t, int64(0), out)
	assert.InDelta(t, 0.0, usd, 1e-9)
}
