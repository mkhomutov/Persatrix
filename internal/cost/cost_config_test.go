package cost

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

func TestLoadCostConfig_ValidFile(t *testing.T) {
	dir := t.TempDir()
	data := `
schema_version: "0.1"
cost:
  pricing:
    models:
      "claude-sonnet":
        input_per_1m_tokens: 3.00
        output_per_1m_tokens: 15.00
  budgets:
    global:
      max_daily_usd: 100.00
      alert_at_percent: [50, 80]
      on_exceed: "fail"
    per_workflow:
      default_max_usd: 10.00
    per_agent:
      default_max_usd: 5.00
`
	require.NoError(t, os.WriteFile(filepath.Join(dir, "optimization.yaml"), []byte(data), 0o644))

	cfg, err := LoadCostConfig(dir)
	require.NoError(t, err)

	assert.Len(t, cfg.Pricing, 1)
	assert.InDelta(t, 3.00, cfg.Pricing["claude-sonnet"].InputPer1MTokens, 1e-9)
	assert.InDelta(t, 15.00, cfg.Pricing["claude-sonnet"].OutputPer1MTokens, 1e-9)
	assert.InDelta(t, 100.00, cfg.Budgets.Global.MaxDailyUSD, 1e-9)
	assert.InDelta(t, 10.00, cfg.Budgets.PerWorkflow.DefaultMaxUSD, 1e-9)
	assert.InDelta(t, 5.00, cfg.Budgets.PerAgent.DefaultMaxUSD, 1e-9)
	assert.Equal(t, "fail", cfg.Budgets.Global.OnExceed)
}

func TestLoadCostConfig_MissingFile(t *testing.T) {
	_, err := LoadCostConfig(t.TempDir())
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "read optimization config")
}

func TestLoadCostConfig_InvalidYAML(t *testing.T) {
	dir := t.TempDir()
	require.NoError(t, os.WriteFile(filepath.Join(dir, "optimization.yaml"), []byte(":::invalid"), 0o644))

	_, err := LoadCostConfig(dir)
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "parse optimization config")
}

func TestLoadCostConfig_EmptyCostSection(t *testing.T) {
	dir := t.TempDir()
	data := `schema_version: "0.1"
`
	require.NoError(t, os.WriteFile(filepath.Join(dir, "optimization.yaml"), []byte(data), 0o644))

	cfg, err := LoadCostConfig(dir)
	require.NoError(t, err)
	assert.NotNil(t, cfg.Pricing)
	assert.Empty(t, cfg.Pricing)
}

// --- BudgetError ---

func TestBudgetError_ErrorMessage(t *testing.T) {
	err := &BudgetError{
		Scope:     "global",
		Spent:     45.50,
		Limit:     50.00,
		Estimated: 10.00,
	}
	msg := err.Error()
	assert.Contains(t, msg, "global budget exceeded")
	assert.Contains(t, msg, "45.5")
	assert.Contains(t, msg, "50.0")
	assert.Contains(t, msg, "10.0")
}

func TestBudgetError_Fields(t *testing.T) {
	cfg := testConfig()
	cfg.Budgets.Global.MaxDailyUSD = 0.01
	tc := NewTokenCounter(cfg, zap.NewNop())
	be := NewBudgetEnforcer(tc, cfg, zap.NewNop())

	result := be.CheckBudget("wf-1", "agent-a", "claude-sonnet", 8192)
	require.Equal(t, BudgetReject, result.Decision)
	require.NotNil(t, result.Error)
	assert.Equal(t, "global", result.Error.Scope)
	assert.Equal(t, 0.0, result.Error.Spent) // no prior usage
	assert.Equal(t, 0.01, result.Error.Limit)
	assert.Greater(t, result.Error.Estimated, 0.0)
}

// --- Config validation tests ---

func TestLoadCostConfig_NegativeGlobalBudget(t *testing.T) {
	dir := t.TempDir()
	data := `
cost:
  budgets:
    global:
      max_daily_usd: -10.00
      on_exceed: "fail"
`
	require.NoError(t, os.WriteFile(filepath.Join(dir, "optimization.yaml"), []byte(data), 0o644))
	_, err := LoadCostConfig(dir)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "global max_daily_usd must be >= 0")
}

func TestLoadCostConfig_NegativePerWorkflowBudget(t *testing.T) {
	dir := t.TempDir()
	data := `
cost:
  budgets:
    per_workflow:
      default_max_usd: -5.00
`
	require.NoError(t, os.WriteFile(filepath.Join(dir, "optimization.yaml"), []byte(data), 0o644))
	_, err := LoadCostConfig(dir)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "per_workflow default_max_usd must be >= 0")
}

func TestLoadCostConfig_NegativePerAgentBudget(t *testing.T) {
	dir := t.TempDir()
	data := `
cost:
  budgets:
    per_agent:
      default_max_usd: -1.00
`
	require.NoError(t, os.WriteFile(filepath.Join(dir, "optimization.yaml"), []byte(data), 0o644))
	_, err := LoadCostConfig(dir)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "per_agent default_max_usd must be >= 0")
}

func TestLoadCostConfig_UnknownOnExceed(t *testing.T) {
	dir := t.TempDir()
	data := `
cost:
  budgets:
    global:
      max_daily_usd: 100.00
      on_exceed: "shutdown"
`
	require.NoError(t, os.WriteFile(filepath.Join(dir, "optimization.yaml"), []byte(data), 0o644))
	_, err := LoadCostConfig(dir)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "on_exceed must be")
	assert.Contains(t, err.Error(), "shutdown")
}

func TestLoadCostConfig_ValidOnExceedValues(t *testing.T) {
	for _, onExceed := range []string{"fail", "pause_and_alert", ""} {
		t.Run(onExceed, func(t *testing.T) {
			dir := t.TempDir()
			data := fmt.Sprintf(`
cost:
  budgets:
    global:
      max_daily_usd: 100.00
      on_exceed: %q
`, onExceed)
			require.NoError(t, os.WriteFile(filepath.Join(dir, "optimization.yaml"), []byte(data), 0o644))
			_, err := LoadCostConfig(dir)
			assert.NoError(t, err)
		})
	}
}
