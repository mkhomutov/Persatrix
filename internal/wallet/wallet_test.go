// Tests for the WalletService gRPC servicer (RFC 0023 PR 2 — real
// enforcement + reaper).
//
// PR 1 landed the always-grant skeleton; PR 2 makes the wallet enforce:
// AcquireLease composes BudgetEnforcer.CheckBudget under a coarse mutex and
// records a provisional charge, SettleLease / ReleaseLease reconcile that
// charge against actuals, and the reaper settles leases abandoned past TTL.
// These tests pin the real contract — they replace the skeleton tests.
//
// This file covers construction and AcquireLease (grant / deny / mutex
// atomicity / concurrency cap / collision) and holds the shared fixtures.
// SettleLease / ReleaseLease live in wallet_settle_test.go; the reaper in
// wallet_reaper_test.go.
package wallet

import (
	"context"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/oklog/ulid/v2"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/generated/walletpb"
)

// testCostConfig returns a CostConfig with known pricing and generous
// budgets. Individual tests tighten a budget scope to exercise denial.
func testCostConfig() *cost.CostConfig {
	return &cost.CostConfig{
		Pricing: map[string]cost.ModelPricing{
			"claude-sonnet": {InputPer1MTokens: 3.0, OutputPer1MTokens: 15.0},
			"claude-haiku":  {InputPer1MTokens: 0.8, OutputPer1MTokens: 4.0},
		},
		Budgets: cost.BudgetThresholds{
			Global:      cost.GlobalBudget{MaxDailyUSD: 100.0, OnExceed: "fail"},
			PerWorkflow: cost.PerWorkflowBudget{DefaultMaxUSD: 10.0},
			PerAgent:    cost.PerAgentBudget{DefaultMaxUSD: 5.0},
		},
	}
}

// newTestWallet builds a WalletService over a fresh TokenCounter +
// BudgetEnforcer for the given cost and wallet config, with a no-op logger.
func newTestWallet(t *testing.T, costCfg *cost.CostConfig, walletCfg Config, opts ...Option) (*WalletService, *cost.TokenCounter) {
	t.Helper()
	return newTestWalletWithLogger(t, costCfg, walletCfg, zap.NewNop(), opts...)
}

// newTestWalletWithLogger is newTestWallet with an explicit logger — for
// tests that attach a zaptest/observer core to assert on the wallet's own
// emitted logs (e.g. the reaper's terminal "lease reaped" record).
func newTestWalletWithLogger(t *testing.T, costCfg *cost.CostConfig, walletCfg Config, logger *zap.Logger, opts ...Option) (*WalletService, *cost.TokenCounter) {
	t.Helper()
	counter := cost.NewTokenCounter(costCfg, zap.NewNop())
	enforcer := cost.NewBudgetEnforcer(counter, costCfg, zap.NewNop())
	w := NewWalletService(counter, enforcer, walletCfg, logger, opts...)
	return w, counter
}

func testContext(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	t.Cleanup(cancel)
	return ctx
}

// leaseState reads a lease's settled flag and existence under w.mu so the
// race detector stays quiet when a reaper goroutine is also touching the
// map. It is a test-only accessor — production code never reads lease state
// from outside the package.
func leaseState(w *WalletService, leaseID string) (settled, exists bool) {
	w.mu.Lock()
	defer w.mu.Unlock()
	ls, ok := w.active[leaseID]
	if !ok {
		return false, false
	}
	return ls.settled, true
}

// startBufconnWalletService stands up an in-process gRPC server hosting w
// over a bufconn listener, returning a connected client and registering
// cleanup. It proves the real servicer still satisfies the gRPC surface.
func startBufconnWalletService(t *testing.T, w *WalletService) walletpb.WalletServiceClient {
	t.Helper()
	lis := bufconn.Listen(1 << 16)
	srv := grpc.NewServer()
	walletpb.RegisterWalletServiceServer(srv, w)
	go func() { _ = srv.Serve(lis) }()

	conn, err := grpc.NewClient("passthrough:///bufnet",
		grpc.WithContextDialer(func(_ context.Context, _ string) (net.Conn, error) {
			return lis.Dial()
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	require.NoError(t, err)
	t.Cleanup(func() {
		_ = conn.Close()
		srv.Stop()
	})
	return walletpb.NewWalletServiceClient(conn)
}

// --- Constructor ---

// TestNewWalletService_NilLoggerSafe pins the nil-logger fallback carried
// over from the skeleton: NewWalletService(nil logger) substitutes a no-op
// logger so the RPC handlers — all of which log — cannot nil-panic.
func TestNewWalletService_NilLoggerSafe(t *testing.T) {
	counter := cost.NewTokenCounter(testCostConfig(), zap.NewNop())
	enforcer := cost.NewBudgetEnforcer(counter, testCostConfig(), zap.NewNop())
	w := NewWalletService(counter, enforcer, DefaultConfig(), nil)
	require.NotNil(t, w)

	resp, err := w.AcquireLease(context.Background(), &walletpb.LeaseRequest{
		AgentId: "agent-1", Model: "claude-sonnet",
		EstimatedInputTokens: 100, EstimatedMaxOutputTokens: 100,
		Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
	})
	require.NoError(t, err)
	require.NotNil(t, resp.GetGrant(), "wallet must grant within budget even with the fallback logger")
}

// TestNewWalletService_PanicsOnNilDependency pins the nil-required-dependency
// guard: counter and enforcer are load-bearing — AcquireLease nil-derefs both
// on the first inbound lease — so a nil either is a programming error caught
// at construction, not as an obscure panic on the first RPC. Matches the
// NewCostReporter / NewLogServiceServer nil-guard convention.
func TestNewWalletService_PanicsOnNilDependency(t *testing.T) {
	counter := cost.NewTokenCounter(testCostConfig(), zap.NewNop())
	enforcer := cost.NewBudgetEnforcer(counter, testCostConfig(), zap.NewNop())

	assert.PanicsWithValue(t,
		"wallet: NewWalletService requires a non-nil TokenCounter",
		func() { NewWalletService(nil, enforcer, DefaultConfig(), zap.NewNop()) },
		"a nil TokenCounter must panic at construction")

	assert.PanicsWithValue(t,
		"wallet: NewWalletService requires a non-nil BudgetEnforcer",
		func() { NewWalletService(counter, nil, DefaultConfig(), zap.NewNop()) },
		"a nil BudgetEnforcer must panic at construction")
}

// TestNewWalletService_PanicsOnUnusableConfig pins that the constructor
// also rejects a Config whose lease-lifecycle tuning is unusable — extending
// the nil-dependency fail-fast guard to the zero-value Config{}. Each field
// breaks the wallet in a distinct way: a non-positive MaxActiveLeases denies
// every lease (the cap check is n >= max, so 0 >= 0 rejects the first
// acquire); a non-positive ReaperInterval panics time.NewTicker inside
// RunReaper; a non-positive TTL makes the reaper settle a lease the instant
// it issues. The sole production caller sources Config from DefaultConfig /
// LoadConfig, both of which satisfy this — so the guard closes the asymmetry
// with the nil-dependency checks rather than fixing a reachable bug.
func TestNewWalletService_PanicsOnUnusableConfig(t *testing.T) {
	counter := cost.NewTokenCounter(testCostConfig(), zap.NewNop())
	enforcer := cost.NewBudgetEnforcer(counter, testCostConfig(), zap.NewNop())

	tests := []struct {
		name string
		cfg  Config
		want string
	}{
		{
			name: "non-positive TTL",
			cfg:  Config{TTL: 0, ReaperInterval: time.Second, MaxActiveLeases: 1},
			want: "wallet: NewWalletService requires a positive Config.TTL",
		},
		{
			name: "non-positive ReaperInterval",
			cfg:  Config{TTL: time.Second, ReaperInterval: 0, MaxActiveLeases: 1},
			want: "wallet: NewWalletService requires a positive Config.ReaperInterval",
		},
		{
			name: "non-positive MaxActiveLeases",
			cfg:  Config{TTL: time.Second, ReaperInterval: time.Second, MaxActiveLeases: 0},
			want: "wallet: NewWalletService requires a positive Config.MaxActiveLeases",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			assert.PanicsWithValue(t, tc.want,
				func() { NewWalletService(counter, enforcer, tc.cfg, zap.NewNop()) },
				"an unusable Config must panic at construction")
		})
	}

	// The realistic misuse — a forgotten Config, left at its zero value —
	// must panic rather than silently produce a deny-all wallet.
	assert.Panics(t, func() {
		NewWalletService(counter, enforcer, Config{}, zap.NewNop())
	}, "a zero-value Config must panic at construction")
}

// --- AcquireLease: grant ---

// TestAcquireLease_GrantsWithinBudget pins the grant path: a request within
// every budget scope returns the grant arm with a server-issued ULID
// lease_id, the TTL from config, granted tokens echoing the estimates — and
// the estimate is provisionally charged against the TokenCounter.
func TestAcquireLease_GrantsWithinBudget(t *testing.T) {
	walletCfg := Config{TTL: 90 * time.Second, ReaperInterval: time.Hour, MaxActiveLeases: 16}
	w, counter := newTestWallet(t, testCostConfig(), walletCfg)

	resp, err := w.AcquireLease(testContext(t), &walletpb.LeaseRequest{
		WorkflowId: "wf-1", AgentId: "agent-a", Model: "claude-sonnet",
		EstimatedInputTokens: 1000, EstimatedMaxOutputTokens: 2000,
		Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
	})
	require.NoError(t, err)

	grant := resp.GetGrant()
	require.NotNil(t, grant, "request within budget must return the grant arm")
	assert.Nil(t, resp.GetDenied())

	_, parseErr := ulid.Parse(grant.GetLeaseId())
	assert.NoError(t, parseErr, "lease_id %q must parse as a ULID", grant.GetLeaseId())
	assert.Equal(t, int64(1000), grant.GetGrantedInputTokens())
	assert.Equal(t, int64(2000), grant.GetGrantedOutputTokens())
	assert.Equal(t, int32(90), grant.GetTtlSeconds(), "ttl_seconds must come from wallet config")

	// The estimate is provisionally charged: claude-sonnet
	// 1000/1M*3.00 + 2000/1M*15.00 = 0.033.
	_, _, usd := counter.GlobalUsage()
	assert.InDelta(t, 0.033, usd, 1e-9, "AcquireLease must record a provisional charge")
}

// --- AcquireLease: deny across all three scopes ---

func TestAcquireLease_DeniesAcrossScopes(t *testing.T) {
	// estMaxOutput 8192 at claude-sonnet output rate: 8192/1M*15 = 0.12288,
	// which exceeds the 0.01 limit set on whichever scope the case tightens.
	tests := []struct {
		name      string
		tighten   func(*cost.CostConfig)
		wantScope string
	}{
		{
			name:      "global",
			tighten:   func(c *cost.CostConfig) { c.Budgets.Global.MaxDailyUSD = 0.01 },
			wantScope: "global",
		},
		{
			name:      "per_workflow",
			tighten:   func(c *cost.CostConfig) { c.Budgets.PerWorkflow.DefaultMaxUSD = 0.01 },
			wantScope: "per_workflow",
		},
		{
			name:      "per_agent",
			tighten:   func(c *cost.CostConfig) { c.Budgets.PerAgent.DefaultMaxUSD = 0.01 },
			wantScope: "per_agent",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			costCfg := testCostConfig()
			tc.tighten(costCfg)
			w, counter := newTestWallet(t, costCfg, DefaultConfig())

			resp, err := w.AcquireLease(testContext(t), &walletpb.LeaseRequest{
				WorkflowId: "wf-1", AgentId: "agent-a", Model: "claude-sonnet",
				EstimatedInputTokens: 0, EstimatedMaxOutputTokens: 8192,
				Cause: walletpb.Cause_CAUSE_CHAT,
			})
			require.NoError(t, err, "a budget denial is a normal response, not a gRPC error")

			denied := resp.GetDenied()
			require.NotNil(t, denied, "over-budget request must return the denied arm")
			assert.Nil(t, resp.GetGrant())
			assert.Equal(t, tc.wantScope, denied.GetScope())
			assert.InDelta(t, 0.01, denied.GetLimitUsd(), 1e-9)
			assert.Greater(t, denied.GetEstimatedUsd(), 0.0)
			assert.NotEmpty(t, denied.GetMessage())

			// A denied lease records no provisional charge.
			_, _, usd := counter.GlobalUsage()
			assert.InDelta(t, 0.0, usd, 1e-9, "denied lease must not be charged")
		})
	}
}

// --- AcquireLease: mutex atomicity ---

// TestAcquireLease_ConcurrentAcquires_NoOverProvision pins the coarse-mutex
// guarantee: with a budget that admits exactly one lease, two concurrent
// AcquireLease calls cannot both pass CheckBudget and both provision past
// the limit — exactly one grants, the other is denied.
func TestAcquireLease_ConcurrentAcquires_NoOverProvision(t *testing.T) {
	costCfg := testCostConfig()
	// One claude-sonnet call of 100000 output tokens costs 1.50; the
	// per-agent budget of 2.00 admits exactly one such lease.
	costCfg.Budgets.PerAgent.DefaultMaxUSD = 2.00
	w, _ := newTestWallet(t, costCfg, DefaultConfig())

	const n = 2
	results := make([]*walletpb.LeaseResponse, n)
	var wg sync.WaitGroup
	wg.Add(n)
	for i := range n {
		go func(i int) {
			defer wg.Done()
			resp, err := w.AcquireLease(context.Background(), &walletpb.LeaseRequest{
				AgentId: "agent-a", Model: "claude-sonnet",
				EstimatedInputTokens: 0, EstimatedMaxOutputTokens: 100000,
				Cause: walletpb.Cause_CAUSE_CHAT,
			})
			require.NoError(t, err)
			results[i] = resp
		}(i)
	}
	wg.Wait()

	grants, denials := 0, 0
	for _, r := range results {
		if r.GetGrant() != nil {
			grants++
		}
		if d := r.GetDenied(); d != nil {
			denials++
			assert.Equal(t, "per_agent", d.GetScope())
		}
	}
	assert.Equal(t, 1, grants, "exactly one concurrent acquire may grant")
	assert.Equal(t, 1, denials, "the second concurrent acquire must be denied")
}

// --- AcquireLease: per-agent concurrency cap ---

// TestAcquireLease_MaxActiveLeasesCap pins the per-agent DoS ceiling: an
// agent may hold at most MaxActiveLeases unsettled leases; the next
// acquisition is rejected with codes.ResourceExhausted. Settling a lease
// frees a slot.
func TestAcquireLease_MaxActiveLeasesCap(t *testing.T) {
	walletCfg := Config{TTL: time.Hour, ReaperInterval: time.Hour, MaxActiveLeases: 3}
	w, _ := newTestWallet(t, testCostConfig(), walletCfg)

	req := &walletpb.LeaseRequest{
		AgentId: "agent-cap", Model: "claude-sonnet",
		EstimatedInputTokens: 10, EstimatedMaxOutputTokens: 10,
		Cause: walletpb.Cause_CAUSE_AUTONOMOUS_TICK,
	}

	var leaseIDs []string
	for range 3 {
		resp, err := w.AcquireLease(testContext(t), req)
		require.NoError(t, err)
		require.NotNil(t, resp.GetGrant())
		leaseIDs = append(leaseIDs, resp.GetGrant().GetLeaseId())
	}

	// Fourth acquisition is over the cap.
	_, err := w.AcquireLease(testContext(t), req)
	require.Error(t, err, "acquisition over the cap must be rejected")
	assert.Equal(t, codes.ResourceExhausted, status.Code(err))

	// A different agent is unaffected by agent-cap's leases.
	resp, err := w.AcquireLease(testContext(t), &walletpb.LeaseRequest{
		AgentId: "agent-other", Model: "claude-sonnet",
		EstimatedInputTokens: 10, EstimatedMaxOutputTokens: 10,
		Cause: walletpb.Cause_CAUSE_AUTONOMOUS_TICK,
	})
	require.NoError(t, err)
	assert.NotNil(t, resp.GetGrant(), "the cap is per-agent, not global")

	// Settling one of agent-cap's leases frees a slot.
	ack, err := w.SettleLease(testContext(t), &walletpb.SettlementRequest{
		LeaseId: leaseIDs[0], ActualInputTokens: 10, ActualOutputTokens: 10,
	})
	require.NoError(t, err)
	require.True(t, ack.GetSuccess())

	resp, err = w.AcquireLease(testContext(t), req)
	require.NoError(t, err)
	assert.NotNil(t, resp.GetGrant(), "settling a lease must free a cap slot")
}

// --- AcquireLease: lease-ID collision ---

// TestAcquireLease_LeaseIDCollisionRejected pins that a server-issued
// lease_id colliding with an in-flight lease is rejected with codes.Internal
// rather than overwriting live lease state. The ID generator is injected so
// the (astronomically unlikely) collision is deterministic.
func TestAcquireLease_LeaseIDCollisionRejected(t *testing.T) {
	w, counter := newTestWallet(t, testCostConfig(), DefaultConfig(),
		WithIDGenerator(func() string { return "DUPLICATE-LEASE-ID" }))

	req := &walletpb.LeaseRequest{
		AgentId: "agent-a", Model: "claude-sonnet",
		EstimatedInputTokens: 100, EstimatedMaxOutputTokens: 100,
		Cause: walletpb.Cause_CAUSE_WORKFLOW_TASK,
	}

	resp, err := w.AcquireLease(testContext(t), req)
	require.NoError(t, err)
	require.Equal(t, "DUPLICATE-LEASE-ID", resp.GetGrant().GetLeaseId())

	_, _, usdAfterFirst := counter.GlobalUsage()

	_, err = w.AcquireLease(testContext(t), req)
	require.Error(t, err, "a colliding lease_id must be rejected")
	assert.Equal(t, codes.Internal, status.Code(err))

	// The rejected acquisition must not have recorded a provisional charge.
	_, _, usdAfterCollision := counter.GlobalUsage()
	assert.InDelta(t, usdAfterFirst, usdAfterCollision, 1e-9,
		"a collision-rejected acquire must not charge the counter")
}
