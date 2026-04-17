package cost

import (
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// testConfig returns a CostConfig with known pricing and budget thresholds for testing.
func testConfig() *CostConfig {
	return &CostConfig{
		Pricing: map[string]ModelPricing{
			"claude-sonnet": {
				InputPer1MTokens:  3.00,
				OutputPer1MTokens: 15.00,
			},
			"claude-haiku": {
				InputPer1MTokens:  0.80,
				OutputPer1MTokens: 4.00,
			},
		},
		Budgets: BudgetThresholds{
			Global: GlobalBudget{
				MaxDailyUSD:    100.00,
				AlertAtPercent: []float64{50, 80, 95},
				OnExceed:       "fail",
			},
			PerWorkflow: PerWorkflowBudget{DefaultMaxUSD: 10.00},
			PerAgent:    PerAgentBudget{DefaultMaxUSD: 5.00},
		},
	}
}

// --- EstimateCost ---

func TestEstimateCost_KnownModel(t *testing.T) {
	cfg := testConfig()
	// 1000 input tokens of claude-sonnet: 1000/1M * 3.00 = 0.003
	// 500 output tokens of claude-sonnet: 500/1M * 15.00 = 0.0075
	cost := cfg.EstimateCost("claude-sonnet", 1000, 500)
	assert.InDelta(t, 0.0105, cost, 1e-9)
}

func TestEstimateCost_UnknownModel(t *testing.T) {
	cfg := testConfig()
	cost := cfg.EstimateCost("gpt-unknown", 1000, 500)
	assert.Equal(t, 0.0, cost)
}

// --- TokenCounter ---

func TestTokenCounter_RecordUsage_PerWorkflow(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 500,
	})

	input, output, usd := tc.WorkflowUsage("wf-1")
	assert.Equal(t, int64(1000), input)
	assert.Equal(t, int64(500), output)
	assert.InDelta(t, 0.0105, usd, 1e-9)
}

func TestTokenCounter_RecordUsage_PerAgent(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 2000, OutputTokens: 1000,
	})

	input, output, usd := tc.AgentUsage("agent-a")
	assert.Equal(t, int64(2000), input)
	assert.Equal(t, int64(1000), output)
	assert.InDelta(t, 0.021, usd, 1e-9)
}

func TestTokenCounter_RecordUsage_GlobalDaily(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 500,
	})
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-2", AgentID: "agent-b", Model: "claude-haiku",
		InputTokens: 2000, OutputTokens: 1000,
	})

	input, output, usd := tc.GlobalUsage()
	assert.Equal(t, int64(3000), input)
	assert.Equal(t, int64(1500), output)
	// claude-sonnet: 0.0105, claude-haiku: 2000/1M*0.80 + 1000/1M*4.00 = 0.0016 + 0.004 = 0.0056
	assert.InDelta(t, 0.0105+0.0056, usd, 1e-9)
}

func TestTokenCounter_RecordUsage_Accumulates(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 500,
	})
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 3000, OutputTokens: 1500,
	})

	input, output, _ := tc.WorkflowUsage("wf-1")
	assert.Equal(t, int64(4000), input)
	assert.Equal(t, int64(2000), output)
}

func TestTokenCounter_WorkflowUsage_NoRecords(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())
	input, output, usd := tc.WorkflowUsage("nonexistent")
	assert.Equal(t, int64(0), input)
	assert.Equal(t, int64(0), output)
	assert.Equal(t, 0.0, usd)
}

func TestTokenCounter_AgentUsage_NoRecords(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())
	input, output, usd := tc.AgentUsage("nonexistent")
	assert.Equal(t, int64(0), input)
	assert.Equal(t, int64(0), output)
	assert.Equal(t, 0.0, usd)
}

func TestTokenCounter_Concurrent(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())

	var wg sync.WaitGroup
	for i := range 100 {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			tc.RecordUsage(UsageRecord{
				WorkflowID:   fmt.Sprintf("wf-%d", i%5),
				AgentID:      fmt.Sprintf("agent-%d", i%3),
				Model:        "claude-sonnet",
				InputTokens:  100,
				OutputTokens: 50,
			})
		}(i)
	}
	wg.Wait()

	input, output, _ := tc.GlobalUsage()
	assert.Equal(t, int64(10000), input) // 100 * 100
	assert.Equal(t, int64(5000), output) // 100 * 50
}

func TestTokenCounter_ResetDaily(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 500,
	})

	tc.ResetDaily()

	input, output, usd := tc.GlobalUsage()
	assert.Equal(t, int64(0), input)
	assert.Equal(t, int64(0), output)
	assert.Equal(t, 0.0, usd)

	input, output, usd = tc.WorkflowUsage("wf-1")
	assert.Equal(t, int64(0), input)
	assert.Equal(t, int64(0), output)
	assert.Equal(t, 0.0, usd)
}

// --- BudgetEnforcer ---

func TestBudgetEnforcer_UnderBudget_Allow(t *testing.T) {
	cfg := testConfig()
	tc := NewTokenCounter(cfg, zap.NewNop())
	be := NewBudgetEnforcer(tc, cfg, zap.NewNop())

	result := be.CheckBudget("wf-1", "agent-a", "claude-sonnet", 1000)
	assert.Equal(t, BudgetAllow, result.Decision)
	assert.Empty(t, result.Reason)
}

func TestBudgetEnforcer_GlobalBudgetExceeded_Reject(t *testing.T) {
	cfg := testConfig()
	cfg.Budgets.Global.MaxDailyUSD = 0.01 // very small budget
	tc := NewTokenCounter(cfg, zap.NewNop())
	be := NewBudgetEnforcer(tc, cfg, zap.NewNop())

	// Estimated cost of 8192 output tokens at claude-sonnet rate:
	// 8192/1M * 15.00 = 0.12288 → exceeds 0.01 limit
	result := be.CheckBudget("wf-1", "agent-a", "claude-sonnet", 8192)
	assert.Equal(t, BudgetReject, result.Decision)
	assert.Contains(t, result.Reason, "global daily budget exceeded")
}

func TestBudgetEnforcer_PerWorkflowBudgetExceeded_Reject(t *testing.T) {
	cfg := testConfig()
	cfg.Budgets.PerWorkflow.DefaultMaxUSD = 0.01
	tc := NewTokenCounter(cfg, zap.NewNop())
	be := NewBudgetEnforcer(tc, cfg, zap.NewNop())

	// Record some usage to push the workflow close to budget.
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 500, OutputTokens: 500,
	})

	result := be.CheckBudget("wf-1", "agent-a", "claude-sonnet", 8192)
	assert.Equal(t, BudgetReject, result.Decision)
	assert.Contains(t, result.Reason, "per-workflow budget exceeded")
}

func TestBudgetEnforcer_PerAgentBudgetExceeded_Reject(t *testing.T) {
	cfg := testConfig()
	cfg.Budgets.PerAgent.DefaultMaxUSD = 0.01
	tc := NewTokenCounter(cfg, zap.NewNop())
	be := NewBudgetEnforcer(tc, cfg, zap.NewNop())

	// Record some usage to push the agent close to budget.
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 500, OutputTokens: 500,
	})

	result := be.CheckBudget("wf-1", "agent-a", "claude-sonnet", 8192)
	assert.Equal(t, BudgetReject, result.Decision)
	assert.Contains(t, result.Reason, "per-agent budget exceeded")
}

func TestBudgetEnforcer_PauseAndAlert_TreatedAsFail(t *testing.T) {
	cfg := testConfig()
	cfg.Budgets.Global.MaxDailyUSD = 0.01
	cfg.Budgets.Global.OnExceed = "pause_and_alert"
	tc := NewTokenCounter(cfg, zap.NewNop())
	be := NewBudgetEnforcer(tc, cfg, zap.NewNop())

	result := be.CheckBudget("wf-1", "agent-a", "claude-sonnet", 8192)
	assert.Equal(t, BudgetReject, result.Decision)
	assert.Contains(t, result.Reason, "global daily budget exceeded")
}

func TestBudgetEnforcer_PreDispatchGuard_EstimatedCostExceedsRemaining(t *testing.T) {
	cfg := testConfig()
	cfg.Budgets.Global.MaxDailyUSD = 0.05
	tc := NewTokenCounter(cfg, zap.NewNop())

	// Spend 0.04 already.
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-haiku",
		InputTokens: 0, OutputTokens: 10000, // 10000/1M * 4.00 = 0.04
	})

	be := NewBudgetEnforcer(tc, cfg, zap.NewNop())

	// Estimated max 5000 output tokens at haiku rate: 5000/1M * 4.00 = 0.02
	// Total would be 0.04 + 0.02 = 0.06 > 0.05 limit
	result := be.CheckBudget("wf-1", "agent-a", "claude-haiku", 5000)
	assert.Equal(t, BudgetReject, result.Decision)
}

func TestBudgetEnforcer_ZeroBudget_NoEnforcement(t *testing.T) {
	cfg := testConfig()
	cfg.Budgets.Global.MaxDailyUSD = 0
	cfg.Budgets.PerWorkflow.DefaultMaxUSD = 0
	cfg.Budgets.PerAgent.DefaultMaxUSD = 0
	tc := NewTokenCounter(cfg, zap.NewNop())
	be := NewBudgetEnforcer(tc, cfg, zap.NewNop())

	// Large token count should still be allowed when limits are zero (disabled).
	result := be.CheckBudget("wf-1", "agent-a", "claude-sonnet", 1_000_000)
	assert.Equal(t, BudgetAllow, result.Decision)
}

func TestBudgetDecision_String(t *testing.T) {
	assert.Equal(t, "allow", BudgetAllow.String())
	assert.Equal(t, "reject", BudgetReject.String())
	assert.Equal(t, "unknown", BudgetDecision(99).String())
}

// --- Config loading ---

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
