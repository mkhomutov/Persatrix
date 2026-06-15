package main

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/generated/walletpb"
	"github.com/mkhomutov/persatrix/internal/wallet"
)

// newBudgetWiringTestWallet builds a real WalletService over a generous cost
// config — the dollar scopes are deliberately slack so the per-interaction
// token ceiling (the thing under test) is the only binding constraint, and the
// active-lease cap is high enough that the two leases this test issues both fit.
func newBudgetWiringTestWallet(t *testing.T) *wallet.WalletService {
	t.Helper()
	costCfg := &cost.CostConfig{
		Pricing: map[string]cost.ModelPricing{
			"claude-haiku": {InputPer1MTokens: 0.8, OutputPer1MTokens: 4.0},
		},
		Budgets: cost.BudgetThresholds{
			Global:      cost.GlobalBudget{MaxDailyUSD: 100.0, OnExceed: "fail"},
			PerWorkflow: cost.PerWorkflowBudget{DefaultMaxUSD: 10.0},
			PerAgent:    cost.PerAgentBudget{DefaultMaxUSD: 5.0},
		},
	}
	counter := cost.NewTokenCounter(costCfg, zap.NewNop())
	enforcer := cost.NewBudgetEnforcer(counter, costCfg, zap.NewNop())
	return wallet.NewWalletService(counter, enforcer,
		wallet.Config{TTL: 90 * time.Second, ReaperInterval: time.Hour, MaxActiveLeases: 64},
		zap.NewNop())
}

// TestInitChannels_WiresInteractionBudgetResolver pins the RFC 0050 amendment's
// orchestrator wiring seam (channels.go): initChannels must inject the channel
// router's budget resolver into the wallet, so the wallet enforces the
// store-owned ceiling and IGNORES the agent-supplied LeaseRequest field.
//
// The channels-side snapshot (interaction_budget_snapshot_test.go) and the
// wallet-side resolver (wallet_interaction_budget_resolver_test.go) are each
// unit-tested in isolation with stand-ins. This test is the only one that
// exercises the single production line that connects them — a regression that
// drops the SetInteractionBudgetResolver call would silently leave the wallet on
// its legacy request-field path, enforcing an agent-supplied ceiling the
// amendment exists to stop trusting, and no other test would catch it.
//
// The proof is a behavioural flip on one fixed request: an over-budget lease for
// an interaction the router has never snapshotted is DENIED before wiring (the
// agent's request ceiling is authoritative) and GRANTED after wiring (the store
// resolver misses → uncapped → the request ceiling is ignored).
func TestInitChannels_WiresInteractionBudgetResolver(t *testing.T) {
	// A minimal channels.yaml so initChannels does not short-circuit on "config
	// absent" before it reaches the router construction + resolver wiring.
	cfgDir := t.TempDir()
	require.NoError(t, os.WriteFile(
		filepath.Join(cfgDir, "channels.yaml"),
		[]byte("max_channels: 50\n"),
		0o644,
	))
	dbPath := filepath.Join(t.TempDir(), "channels.db")
	logger := zap.NewNop()

	walletSvc := newBudgetWiringTestWallet(t)

	// One fixed request: 200 estimated tokens against an agent-supplied ceiling
	// of 100, for an interaction the router will never know (nothing published).
	overBudget := func() *walletpb.LeaseRequest {
		return &walletpb.LeaseRequest{
			AgentId: "agent-a", Model: "claude-haiku",
			EstimatedInputTokens: 100, EstimatedMaxOutputTokens: 100, // 200 > 100
			Cause:                   walletpb.Cause_CAUSE_CHANNEL_MESSAGE,
			InteractionId:           "int-unknown",
			InteractionBudgetTokens: 100,
		}
	}

	// Baseline (no resolver wired): the wallet is on its legacy path, so the
	// agent-supplied ceiling is authoritative and the over-budget lease denies.
	resp, err := walletSvc.AcquireLease(context.Background(), overBudget())
	require.NoError(t, err, "a budget denial is in-band, not a gRPC error")
	require.NotNil(t, resp.GetDenied(),
		"pre-condition: with no resolver, the agent-supplied ceiling denies the over-budget lease")

	// Wire the channel router into the wallet through the production seam.
	opts, cleanup, err := initChannels(cfgDir, dbPath, "", "", nil, nil, walletSvc, logger)
	t.Cleanup(cleanup)
	require.NoError(t, err)
	require.NotEmpty(t, opts, "channels must wire (else the resolver injection line never runs)")

	// After wiring, the store is authoritative. The router holds no snapshot for
	// this interaction, so the resolver MISSES → the lease is uncapped and the
	// agent-supplied 100 is ignored → GRANTED. The denied→granted flip on the
	// identical request is observable proof the resolver was injected.
	resp, err = walletSvc.AcquireLease(context.Background(), overBudget())
	require.NoError(t, err)
	require.NotNil(t, resp.GetGrant(),
		"post-wiring: the store resolver is authoritative; a resolver miss is uncapped, so the agent-supplied ceiling is ignored")
}
