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
	"go.uber.org/zap/zaptest/observer"
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
	assert.Nil(t, result.Error)
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
	require.NotNil(t, result.Error)
	assert.Equal(t, "global", result.Error.Scope)
	assert.Contains(t, result.Error.Error(), "global budget exceeded")
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
	require.NotNil(t, result.Error)
	assert.Equal(t, "per_workflow", result.Error.Scope)
	assert.Contains(t, result.Error.Error(), "per_workflow budget exceeded")
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
	require.NotNil(t, result.Error)
	assert.Equal(t, "per_agent", result.Error.Scope)
	assert.Contains(t, result.Error.Error(), "per_agent budget exceeded")
}

func TestBudgetEnforcer_PauseAndAlert_TreatedAsFail(t *testing.T) {
	cfg := testConfig()
	cfg.Budgets.Global.MaxDailyUSD = 0.01
	cfg.Budgets.Global.OnExceed = "pause_and_alert"
	tc := NewTokenCounter(cfg, zap.NewNop())
	be := NewBudgetEnforcer(tc, cfg, zap.NewNop())

	result := be.CheckBudget("wf-1", "agent-a", "claude-sonnet", 8192)
	assert.Equal(t, BudgetReject, result.Decision)
	require.NotNil(t, result.Error)
	assert.Equal(t, "global", result.Error.Scope)
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

// --- Atomic snapshot ---

func TestUsageSnapshot_Atomic(t *testing.T) {
	cfg := testConfig()
	tc := NewTokenCounter(cfg, zap.NewNop())

	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 500,
	})

	globalUSD, wfUSD, agUSD := tc.usageSnapshot("wf-1", "agent-a")
	assert.Greater(t, globalUSD, 0.0)
	assert.InDelta(t, globalUSD, wfUSD, 1e-9)
	assert.InDelta(t, globalUSD, agUSD, 1e-9)
}

func TestUsageSnapshot_MissingScopes(t *testing.T) {
	cfg := testConfig()
	tc := NewTokenCounter(cfg, zap.NewNop())

	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 500,
	})

	globalUSD, wfUSD, agUSD := tc.usageSnapshot("wf-nonexistent", "agent-nonexistent")
	assert.Greater(t, globalUSD, 0.0)
	assert.Equal(t, 0.0, wfUSD)
	assert.Equal(t, 0.0, agUSD)
}

// --- C2: PauseAndAlert warning log verification ---

func TestBudgetEnforcer_PauseAndAlert_WarningLogged(t *testing.T) {
	cfg := testConfig()
	cfg.Budgets.Global.MaxDailyUSD = 0.01
	cfg.Budgets.Global.OnExceed = "pause_and_alert"
	tc := NewTokenCounter(cfg, zap.NewNop())

	core, logs := observer.New(zap.WarnLevel)
	enforcerLogger := zap.New(core)
	be := NewBudgetEnforcer(tc, cfg, enforcerLogger)

	// Constructor should have emitted a warning.
	constructorWarns := logs.FilterMessage("on_exceed: pause_and_alert is not implemented, treating as fail")
	assert.Equal(t, 1, constructorWarns.Len(), "expected constructor warning for pause_and_alert")

	// Trigger budget rejection.
	result := be.CheckBudget("wf-1", "agent-a", "claude-sonnet", 8192)
	assert.Equal(t, BudgetReject, result.Decision)

	// Enforcement-time warning should also be emitted.
	enforcementWarns := logs.FilterMessage("pause_and_alert not implemented, rejecting dispatch")
	assert.Equal(t, 1, enforcementWarns.Len(), "expected enforcement-time warning for pause_and_alert")
}

// --- C5: ResetDaily agent scope ---

func TestTokenCounter_ResetDaily_AgentScope(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 500,
	})

	// Verify data exists before reset.
	input, output, usd := tc.AgentUsage("agent-a")
	require.Greater(t, input, int64(0))
	require.Greater(t, output, int64(0))
	require.Greater(t, usd, 0.0)

	tc.ResetDaily()

	// Explicit assertion that per-agent data is cleared.
	input, output, usd = tc.AgentUsage("agent-a")
	assert.Equal(t, int64(0), input)
	assert.Equal(t, int64(0), output)
	assert.Equal(t, 0.0, usd)
}

// --- C6/S-02: Concurrent CheckBudget + RecordUsage ---

func TestConcurrent_CheckBudget_RecordUsage(t *testing.T) {
	cfg := testConfig()
	cfg.Budgets.Global.MaxDailyUSD = 1000.00 // large budget to avoid rejections
	tc := NewTokenCounter(cfg, zap.NewNop())
	be := NewBudgetEnforcer(tc, cfg, zap.NewNop())

	const goroutines = 50
	var wg sync.WaitGroup
	wg.Add(goroutines * 2)

	// Half goroutines record usage, half check budget.
	for i := range goroutines {
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
		go func(i int) {
			defer wg.Done()
			// CheckBudget should not panic or race.
			_ = be.CheckBudget(
				fmt.Sprintf("wf-%d", i%5),
				fmt.Sprintf("agent-%d", i%3),
				"claude-sonnet",
				1000,
			)
		}(i)
	}

	wg.Wait()

	// Verify no data corruption — exact values don't matter, absence of race is the goal.
	input, output, _ := tc.GlobalUsage()
	assert.Equal(t, int64(goroutines*100), input)
	assert.Equal(t, int64(goroutines*50), output)
}

// --- S-01: Parallel steps collective budget overspend ---

func TestParallelSteps_CollectiveBudgetOverspend(t *testing.T) {
	// This test documents the known optimistic-check behavior: N parallel steps
	// that each pass individual budget checks can collectively exceed the budget.
	// This is an accepted TOCTOU characteristic of the non-atomic check-then-act
	// pattern (see RFC 0006 TOCTOU note).
	cfg := testConfig()
	// Budget allows ~6.67 dispatches of 1000 output tokens at sonnet rate.
	// (1000/1M * 15.00 = 0.015 per dispatch; 0.10/0.015 ≈ 6.67)
	cfg.Budgets.Global.MaxDailyUSD = 0.10
	tc := NewTokenCounter(cfg, zap.NewNop())
	be := NewBudgetEnforcer(tc, cfg, zap.NewNop())

	// 10 parallel "steps" each check budget before any usage is recorded.
	// All 10 should pass since no usage has been recorded yet.
	const parallelSteps = 10
	var wg sync.WaitGroup
	results := make([]BudgetCheckResult, parallelSteps)
	wg.Add(parallelSteps)
	for i := range parallelSteps {
		go func(i int) {
			defer wg.Done()
			results[i] = be.CheckBudget("wf-1", "agent-a", "claude-sonnet", 1000)
		}(i)
	}
	wg.Wait()

	// All should be allowed — optimistic check, no usage recorded yet.
	allowCount := 0
	for _, r := range results {
		if r.Decision == BudgetAllow {
			allowCount++
		}
	}
	assert.Equal(t, parallelSteps, allowCount,
		"all parallel checks should pass (optimistic check, no prior usage)")

	// If all 10 dispatches proceed and each uses 1000 output tokens:
	// total = 10 * 0.015 = 0.15 > 0.10 budget.
	// This documents the known overspend behavior.
	for range parallelSteps {
		tc.RecordUsage(UsageRecord{
			WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
			InputTokens: 0, OutputTokens: 1000,
		})
	}
	_, _, totalSpent := tc.GlobalUsage()
	assert.Greater(t, totalSpent, cfg.Budgets.Global.MaxDailyUSD,
		"collective spend should exceed budget (documented optimistic-check behavior)")
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

// --- C3: Unknown model debug log ---

func TestEstimateCost_UnknownModel_DebugLog(t *testing.T) {
	core, logs := observer.New(zap.DebugLevel)
	logger := zap.New(core)

	cfg := &CostConfig{
		Pricing: map[string]ModelPricing{
			"claude-sonnet": {InputPer1MTokens: 3.00, OutputPer1MTokens: 15.00},
		},
		logger: logger,
	}

	cost := cfg.EstimateCost("unknown-model", 1000, 500)
	assert.Equal(t, 0.0, cost)

	debugLogs := logs.FilterMessage("model not found in pricing table, cost estimate is $0")
	assert.Equal(t, 1, debugLogs.Len(), "expected debug log for unknown model")
}
