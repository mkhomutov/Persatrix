// Tests for the RFC 0050 amendment — server-side resolution of the RFC 0030
// Layer 1 per-interaction cost ceiling. With a resolver wired (the orchestrator
// injects one reading the channel router's snapshot), the ceiling comes from the
// store, not the agent-supplied LeaseRequest field: the store is authoritative,
// so an agent can neither widen nor invent a ceiling. With no resolver wired, the
// pre-amendment request-field behaviour is preserved (exercised by the sibling
// wallet_interaction_budget_test.go).
package wallet

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/generated/walletpb"
)

// resolverFromMap is the test stand-in for the channel router's snapshot: a hit
// returns the snapshotted ceiling, a miss reads as uncapped.
func resolverFromMap(m map[string]int64) func(string) (int64, bool) {
	return func(id string) (int64, bool) { v, ok := m[id]; return v, ok }
}

// TestAcquireLease_BudgetResolver_ServerValueEnforcedWithZeroRequest is the
// amendment's acceptance core: the real agent posture stamps NO ceiling on the
// request (budget 0), yet a resolver-supplied ceiling is enforced — closing the
// "nobody stamps the ceiling" gap that made the knob inert.
func TestAcquireLease_BudgetResolver_ServerValueEnforcedWithZeroRequest(t *testing.T) {
	w, _ := newTestWallet(t, testCostConfig(), interactionBudgetWalletCfg())
	w.SetInteractionBudgetResolver(resolverFromMap(map[string]int64{"int-1": 3000}))

	first := acquireForInteraction(t, w, "int-1", 0, 1000, 1000) // 2000 of 3000
	require.NotNil(t, first.GetGrant(), "first lease (2000 of 3000) grants under the resolved ceiling")

	second := acquireForInteraction(t, w, "int-1", 0, 1000, 1000) // → 4000 > 3000
	denied := second.GetDenied()
	require.NotNil(t, denied, "the resolved ceiling enforces even though the request budget is 0")
	assert.Equal(t, walletpb.LeaseDeniedReason_LEASE_DENIED_REASON_INTERACTION_BUDGET_EXHAUSTED, denied.GetReason())
}

// TestAcquireLease_BudgetResolver_IgnoresAgentSuppliedCeiling proves the trust
// boundary: an agent cannot WIDEN its ceiling by sending a large request value —
// the resolver's (smaller) snapshot wins.
func TestAcquireLease_BudgetResolver_IgnoresAgentSuppliedCeiling(t *testing.T) {
	w, _ := newTestWallet(t, testCostConfig(), interactionBudgetWalletCfg())
	w.SetInteractionBudgetResolver(resolverFromMap(map[string]int64{"int-1": 3000}))

	require.NotNil(t, acquireForInteraction(t, w, "int-1", 1_000_000, 1000, 1000).GetGrant())
	assert.NotNil(t, acquireForInteraction(t, w, "int-1", 1_000_000, 1000, 1000).GetDenied(),
		"the agent-supplied ceiling is ignored; the server snapshot is authoritative")
}

// TestAcquireLease_BudgetResolver_MissIsUncapped proves a resolver MISS (uncapped
// channel, or a non-channel / TICK lease with no snapshot) is uncapped, ignoring
// any agent-supplied value — the store, not the agent, decides there is no ceiling.
func TestAcquireLease_BudgetResolver_MissIsUncapped(t *testing.T) {
	w, _ := newTestWallet(t, testCostConfig(), interactionBudgetWalletCfg())
	w.SetInteractionBudgetResolver(resolverFromMap(map[string]int64{})) // every lookup misses

	for i := 0; i < 5; i++ {
		require.NotNil(t, acquireForInteraction(t, w, "int-1", 100, 1000, 1000).GetGrant(),
			"a resolver miss is uncapped, ignoring the positive request field")
	}
}
