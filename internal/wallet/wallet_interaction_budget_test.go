// Tests for RFC 0030 Layer 1 — the per-interaction cost ceiling on
// AcquireLease (governance-layers PR 2). The wallet tracks a per-
// interaction_id running token total alongside the existing per-workflow /
// per-agent / global scopes; once a lease would push that total past the
// request's interaction_budget_tokens, the lease is denied in-band with
// LeaseDeniedReason_LEASE_DENIED_REASON_INTERACTION_BUDGET_EXHAUSTED and
// fail-closed (no provisional charge, no LLM call).
//
// The ceiling is opt-in: a zero interaction_budget_tokens, or an empty
// interaction_id, is the untracked / uncapped pre-v0.3.8 case and never
// denies. See docs/rfcs/0030-multi-agent-conversation-governance.md §E and
// docs/rfcs/0030-governance-layers-pr-plan.md (PR 2).
package wallet

import (
	"strconv"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/generated/walletpb"
)

// interactionBudgetWalletCfg is a generous wallet config — the per-agent
// active-lease cap and TTL are not the thing under test here.
func interactionBudgetWalletCfg() Config {
	return Config{TTL: 90 * time.Second, ReaperInterval: time.Hour, MaxActiveLeases: 64}
}

// acquireForInteraction is a small helper: one cheap claude-haiku lease of
// (input+output) estimated tokens attributed to interactionID under
// budgetTokens. claude-haiku keeps the dollar cost trivially under the
// testCostConfig scopes so the interaction *token* ceiling is the only
// binding constraint.
func acquireForInteraction(t *testing.T, w *WalletService, interactionID string, budgetTokens, input, output int64) *walletpb.LeaseResponse {
	t.Helper()
	resp, err := w.AcquireLease(testContext(t), &walletpb.LeaseRequest{
		AgentId: "agent-a", Model: "claude-haiku",
		EstimatedInputTokens: input, EstimatedMaxOutputTokens: output,
		Cause:                   walletpb.Cause_CAUSE_CHANNEL_MESSAGE,
		InteractionId:           interactionID,
		InteractionBudgetTokens: budgetTokens,
	})
	require.NoError(t, err, "a budget denial is an in-band response, not a gRPC error")
	return resp
}

// TestAcquireLease_InteractionBudget_DeniesWhenExhausted is the PR-2
// acceptance core: with interaction_budget_tokens=3000, a first lease of
// 2000 estimated tokens grants; a second lease in the same interaction
// (another 2000 → 4000 total) crosses the ceiling and is denied — fail-
// closed (no provisional charge recorded) with the typed reason.
func TestAcquireLease_InteractionBudget_DeniesWhenExhausted(t *testing.T) {
	w, counter := newTestWallet(t, testCostConfig(), interactionBudgetWalletCfg())

	first := acquireForInteraction(t, w, "int-1", 3000, 1000, 1000)
	require.NotNil(t, first.GetGrant(), "first lease (2000 of 3000) must grant")

	// One haiku lease of 1000/1000 costs 0.0008 + 0.004 = 0.0048; after the
	// first grant the global counter holds exactly that.
	_, _, usdAfterFirst := counter.GlobalUsage()
	assert.InDelta(t, 0.0048, usdAfterFirst, 1e-9)

	second := acquireForInteraction(t, w, "int-1", 3000, 1000, 1000)
	denied := second.GetDenied()
	require.NotNil(t, denied, "second lease (would reach 4000 > 3000) must be denied")
	assert.Nil(t, second.GetGrant())
	assert.Equal(t, walletpb.LeaseDeniedReason_LEASE_DENIED_REASON_INTERACTION_BUDGET_EXHAUSTED,
		denied.GetReason(), "denial must carry the typed interaction-budget reason")
	assert.Equal(t, "interaction", denied.GetScope())
	assert.NotEmpty(t, denied.GetMessage())

	// Fail-closed: the denied lease recorded no provisional charge — the
	// global total is unchanged from after the first grant.
	_, _, usdAfterDenial := counter.GlobalUsage()
	assert.InDelta(t, usdAfterFirst, usdAfterDenial, 1e-9,
		"a denied lease must not be charged (fail-closed, no LLM call)")
}

// TestAcquireLease_InteractionBudget_ExactBoundaryGrants pins the boundary:
// reaching the ceiling exactly is allowed; only crossing it denies.
func TestAcquireLease_InteractionBudget_ExactBoundaryGrants(t *testing.T) {
	w, _ := newTestWallet(t, testCostConfig(), interactionBudgetWalletCfg())

	first := acquireForInteraction(t, w, "int-1", 4000, 1000, 1000) // → 2000
	require.NotNil(t, first.GetGrant())
	second := acquireForInteraction(t, w, "int-1", 4000, 1000, 1000) // → exactly 4000
	require.NotNil(t, second.GetGrant(), "reaching the ceiling exactly must grant")
	third := acquireForInteraction(t, w, "int-1", 4000, 1, 0) // → 4001 > 4000
	require.NotNil(t, third.GetDenied(), "the lease that crosses the ceiling must deny")
}

// TestAcquireLease_InteractionBudget_DefaultZeroNeverDenies proves opt-in:
// interaction_budget_tokens=0 (the default) never denies, regardless of how
// much the interaction has already spent.
func TestAcquireLease_InteractionBudget_DefaultZeroNeverDenies(t *testing.T) {
	w, _ := newTestWallet(t, testCostConfig(), interactionBudgetWalletCfg())
	for i := 0; i < 20; i++ {
		resp := acquireForInteraction(t, w, "int-1", 0, 1000, 1000)
		require.NotNil(t, resp.GetGrant(), "uncapped (budget=0) lease %d must grant", i)
	}
}

// TestAcquireLease_InteractionBudget_EmptyInteractionIDUntracked proves an
// empty interaction_id is never gated even when a budget is supplied — the
// non-channel (chat / TICK / workflow) traffic stays uncapped.
func TestAcquireLease_InteractionBudget_EmptyInteractionIDUntracked(t *testing.T) {
	w, _ := newTestWallet(t, testCostConfig(), interactionBudgetWalletCfg())
	for i := 0; i < 5; i++ {
		resp := acquireForInteraction(t, w, "", 1000, 1000, 1000) // 2000 > 1000 each, but untracked
		require.NotNil(t, resp.GetGrant(), "empty interaction_id lease %d must grant (untracked)", i)
	}
}

// TestAcquireLease_InteractionBudget_DistinctInteractionsIndependent proves
// the running total is per-interaction: exhausting interaction A does not
// affect interaction B.
func TestAcquireLease_InteractionBudget_DistinctInteractionsIndependent(t *testing.T) {
	w, _ := newTestWallet(t, testCostConfig(), interactionBudgetWalletCfg())

	require.NotNil(t, acquireForInteraction(t, w, "int-A", 3000, 1000, 1000).GetGrant())
	require.NotNil(t, acquireForInteraction(t, w, "int-A", 3000, 1000, 1000).GetDenied(),
		"int-A is exhausted")
	require.NotNil(t, acquireForInteraction(t, w, "int-B", 3000, 1000, 1000).GetGrant(),
		"int-B has its own running total, unaffected by int-A")
}

// TestAcquireLease_InteractionBudget_ReleaseReturnsBudget proves the
// interaction running total reconciles on lease close: a granted lease that
// is released (its LLM call never happened) frees its tokens back to the
// interaction, so a later lease that would otherwise be denied now fits.
func TestAcquireLease_InteractionBudget_ReleaseReturnsBudget(t *testing.T) {
	w, _ := newTestWallet(t, testCostConfig(), interactionBudgetWalletCfg())

	first := acquireForInteraction(t, w, "int-1", 3000, 1000, 1000) // → 2000
	grant := first.GetGrant()
	require.NotNil(t, grant)

	// Release fully reverses the hold — the interaction total drops back to 0.
	ack, err := w.ReleaseLease(testContext(t), &walletpb.ReleaseRequest{LeaseId: grant.GetLeaseId(), Reason: "aborted"})
	require.NoError(t, err)
	require.True(t, ack.GetSuccess())

	// A fresh 2000-token lease again fits under the 3000 ceiling.
	second := acquireForInteraction(t, w, "int-1", 3000, 1000, 1000)
	require.NotNil(t, second.GetGrant(),
		"after release, the interaction budget is freed and the next lease fits")
}

// TestAcquireLease_InteractionBudget_SettleAdjustsRunningTotal proves the
// running total tracks *actual* usage: a lease granted at a worst-case
// estimate but settled lower frees the difference, so the interaction can
// admit more than the sum of pessimistic estimates would allow.
func TestAcquireLease_InteractionBudget_SettleAdjustsRunningTotal(t *testing.T) {
	w, _ := newTestWallet(t, testCostConfig(), interactionBudgetWalletCfg())

	first := acquireForInteraction(t, w, "int-1", 3000, 0, 2000) // estimate 2000 → running 2000
	grant := first.GetGrant()
	require.NotNil(t, grant)

	// Actual usage was only 500 output tokens — reconcile down to 500.
	ack, err := w.SettleLease(testContext(t), &walletpb.SettlementRequest{
		LeaseId: grant.GetLeaseId(), ActualInputTokens: 0, ActualOutputTokens: 500,
	})
	require.NoError(t, err)
	require.True(t, ack.GetSuccess())

	// Running total is now 500; a 2000-token lease (→ 2500) still fits under 3000.
	second := acquireForInteraction(t, w, "int-1", 3000, 0, 2000)
	require.NotNil(t, second.GetGrant(),
		"settling below the estimate frees the difference back to the interaction")
}

// TestAcquireLease_InteractionBudget_UncappedDoesNotAccumulate is the
// memory-bound guard: an uncapped lease (interaction_budget_tokens=0) carries
// an interaction_id in production today (PR 1 stamps it on every channel
// lease) but has no ceiling to be checked against, so the wallet must NOT
// fold it into the per-interaction running total. Tracking it would grow
// interactionTokens by one permanent entry per conversation — an unbounded
// leak on a long-lived orchestrator — for zero enforcement benefit. After
// many uncapped acquire/settle cycles across distinct interactions the
// running-total map stays empty.
func TestAcquireLease_InteractionBudget_UncappedDoesNotAccumulate(t *testing.T) {
	w, _ := newTestWallet(t, testCostConfig(), interactionBudgetWalletCfg())

	for i := 0; i < 32; i++ {
		id := "conv-" + strconv.Itoa(i)
		resp := acquireForInteraction(t, w, id, 0 /* uncapped */, 1000, 1000)
		grant := resp.GetGrant()
		require.NotNil(t, grant, "uncapped lease %d must grant", i)
		ack, err := w.SettleLease(testContext(t), &walletpb.SettlementRequest{
			LeaseId: grant.GetLeaseId(), ActualInputTokens: 1000, ActualOutputTokens: 1000,
		})
		require.NoError(t, err)
		require.True(t, ack.GetSuccess())
	}

	w.mu.Lock()
	residual := len(w.interactionTokens)
	w.mu.Unlock()
	assert.Zero(t, residual,
		"uncapped (budget=0) interaction traffic must leave no per-interaction running-total residue")
}

// TestAcquireLease_InteractionBudget_TrackedInteractionFreedOnRelease proves
// the converse for a capped interaction: once every lease in it is released
// (its LLM call never happened), the running total returns to zero and the
// entry is pruned — a fully-reversed interaction leaves no residue.
func TestAcquireLease_InteractionBudget_TrackedInteractionFreedOnRelease(t *testing.T) {
	w, _ := newTestWallet(t, testCostConfig(), interactionBudgetWalletCfg())

	first := acquireForInteraction(t, w, "int-1", 8000, 1000, 1000)
	second := acquireForInteraction(t, w, "int-1", 8000, 1000, 1000)
	g1, g2 := first.GetGrant(), second.GetGrant()
	require.NotNil(t, g1)
	require.NotNil(t, g2)

	w.mu.Lock()
	tracked := len(w.interactionTokens)
	w.mu.Unlock()
	require.Equal(t, 1, tracked, "a capped interaction with live leases is tracked")

	for _, id := range []string{g1.GetLeaseId(), g2.GetLeaseId()} {
		ack, err := w.ReleaseLease(testContext(t), &walletpb.ReleaseRequest{LeaseId: id, Reason: "aborted"})
		require.NoError(t, err)
		require.True(t, ack.GetSuccess())
	}

	w.mu.Lock()
	residual := len(w.interactionTokens)
	w.mu.Unlock()
	assert.Zero(t, residual,
		"a fully-released capped interaction must leave no running-total residue")
}

// TestAcquireLease_ScopeDenialCarriesBudgetReason proves the existing RFC
// 0023 per-scope budget denial now carries the typed
// LEASE_DENIED_REASON_BUDGET, so consumers can machine-distinguish it from
// the interaction-budget denial.
func TestAcquireLease_ScopeDenialCarriesBudgetReason(t *testing.T) {
	costCfg := testCostConfig()
	costCfg.Budgets.Global.MaxDailyUSD = 0.01
	w, _ := newTestWallet(t, costCfg, DefaultConfig())

	resp, err := w.AcquireLease(testContext(t), &walletpb.LeaseRequest{
		WorkflowId: "wf-1", AgentId: "agent-a", Model: "claude-sonnet",
		EstimatedInputTokens: 0, EstimatedMaxOutputTokens: 8192,
		Cause: walletpb.Cause_CAUSE_CHAT,
	})
	require.NoError(t, err)
	denied := resp.GetDenied()
	require.NotNil(t, denied)
	assert.Equal(t, walletpb.LeaseDeniedReason_LEASE_DENIED_REASON_BUDGET, denied.GetReason(),
		"a per-scope budget denial must carry the typed BUDGET reason")
}
