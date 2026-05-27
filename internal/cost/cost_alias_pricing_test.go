package cost

import (
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestLoadCostConfig_ShippedConfig_PricesAliasPhysicalModels pins the RFC 0033
// §F alias-derived pricing in the shipped *configured* artifacts. The
// cost.pricing.models block is the projection of the models.aliases map, so the
// *physical* model each alias resolves to must be priced.
//
// v0.3.4 "no default provider": the base config/optimization.yaml ships its
// aliases UNCONFIGURED, so the configured artifacts are the per-provider demo
// configs (config/demo/<provider>/optimization.yaml). This pins the anthropic
// demo (where `quality` → claude-sonnet-4-6) and the openai demo (the priced
// cloud peer). It is the PR 3 cost-regression guard: before PR 5 the table
// keyed the retired claude-sonnet-4-20250514, so EstimateCost("claude-sonnet-4-6", …)
// returned $0 and the RFC 0023 pre-call lease / budget gate silently disabled.
func TestLoadCostConfig_ShippedConfig_PricesAliasPhysicalModels(t *testing.T) {
	cfg, err := LoadCostConfig(filepath.Join("..", "..", "config", "demo", "anthropic"))
	require.NoError(t, err)

	// quality → claude-sonnet-4-6 (the PR 3 migration target) is priced.
	q, ok := cfg.Pricing["claude-sonnet-4-6"]
	require.True(t, ok, "quality's physical model must be in the pricing table")
	assert.Greater(t, q.InputPer1MTokens, 0.0)
	assert.Greater(t, q.OutputPer1MTokens, 0.0)

	// The retired raw id the old hand-maintained block keyed is gone — it no
	// longer matches any alias's physical model, so the derived table drops it.
	_, retired := cfg.Pricing["claude-sonnet-4-20250514"]
	assert.False(t, retired, "retired Sonnet 4 id must be dropped from the derived table")

	// The end-to-end gate property: an alias-routed call estimates non-zero.
	assert.Greater(t, cfg.EstimateCost("claude-sonnet-4-6", 1000, 500), 0.0,
		"alias-routed cost estimate must not be $0")

	// OpenAI peer (amendment 2026-05-24 item 2) ships priced in its own demo
	// config so the one-line provider swap resolves to a priced target.
	openaiCfg, err := LoadCostConfig(filepath.Join("..", "..", "config", "demo", "openai"))
	require.NoError(t, err)
	gpt, ok := openaiCfg.Pricing["gpt-4o"]
	require.True(t, ok, "OpenAI peer physical model must be priced")
	assert.Greater(t, gpt.InputPer1MTokens, 0.0)
	assert.Greater(t, gpt.OutputPer1MTokens, 0.0)
}
