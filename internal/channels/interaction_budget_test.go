package channels

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestInteractionBudget_SetGetAndClamp pins the basic router-held state: a set
// value reads back, a negative is clamped to 0 (uncapped), and an unknown channel
// reads 0 (the opt-in default).
func TestInteractionBudget_SetGetAndClamp(t *testing.T) {
	router, _, _ := newRouterTest(t)
	router.SetInteractionBudgetTokens("group:x", 1200)
	assert.EqualValues(t, 1200, router.InteractionBudgetTokensFor("group:x"))

	router.SetInteractionBudgetTokens("group:x", -5)
	assert.EqualValues(t, 0, router.InteractionBudgetTokensFor("group:x"), "negative clamps to uncapped")
	assert.EqualValues(t, 0, router.InteractionBudgetTokensFor("group:unknown"), "unknown channel is uncapped")
}

// TestResolveInteractionBudgets_ChannelOverFleetAndStoreInherit pins the boot
// precedence (the Layer 1 sibling of ResolveReplyBudgets): a config-declared
// channel uses its declared budget; a store-resident channel not in config
// inherits the fleet default. The store enumeration is what makes the second case
// work — without it the store-only channel would read 0 instead of the non-zero
// fleet default.
func TestResolveInteractionBudgets_ChannelOverFleetAndStoreInherit(t *testing.T) {
	router, _, store := newRouterTest(t)
	declared := mustCreateGroup(t, store, "planning", "alice", "bob")
	storeOnly := mustCreateGroup(t, store, "adhoc", "carol")
	cfg := &Config{
		DefaultInteractionBudgetTokens: 500,
		Channels: []ChannelConfig{
			{Name: "planning", InteractionBudgetTokens: 1000},
		},
	}
	require.NoError(t, router.ResolveInteractionBudgets(context.Background(), cfg))

	assert.EqualValues(t, 1000, router.InteractionBudgetTokensFor(declared), "config channel uses its declared budget")
	assert.EqualValues(t, 500, router.InteractionBudgetTokensFor(storeOnly), "store-resident channel inherits the fleet default")
}

// TestApplyDefaultInteractionBudget_StampsCapturedFleetDefault pins that the
// inherit path stamps the fleet default captured at resolve time (zero being a
// meaningful value, the inherit cannot ride a Set(_, 0) sentinel).
func TestApplyDefaultInteractionBudget_StampsCapturedFleetDefault(t *testing.T) {
	router, _, _ := newRouterTest(t)
	require.NoError(t, router.ResolveInteractionBudgets(context.Background(), &Config{DefaultInteractionBudgetTokens: 750}))

	router.ApplyDefaultInteractionBudget("group:new")
	assert.EqualValues(t, 750, router.InteractionBudgetTokensFor("group:new"))
}

// TestApplyOverridesToRouter_InteractionBudget pins the apply seam both ways: a
// present override stamps its value; an absent override re-inherits the captured
// fleet default rather than leaving the prior value stuck (the shadow-the-block
// semantics ResolveFromStore / ApplyChannelConfig rely on).
func TestApplyOverridesToRouter_InteractionBudget(t *testing.T) {
	router, _, _ := newRouterTest(t)
	require.NoError(t, router.ResolveInteractionBudgets(context.Background(), &Config{DefaultInteractionBudgetTokens: 600}))

	v := int64(2500)
	router.applyOverridesToRouter("group:c", ChannelConfigOverrides{InteractionBudgetTokens: &v})
	assert.EqualValues(t, 2500, router.InteractionBudgetTokensFor("group:c"), "present override stamps its value")

	router.applyOverridesToRouter("group:c", ChannelConfigOverrides{})
	assert.EqualValues(t, 600, router.InteractionBudgetTokensFor("group:c"), "absent override re-inherits the fleet default")
}
