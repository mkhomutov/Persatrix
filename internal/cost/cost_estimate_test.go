package cost

import (
	"testing"

	"github.com/stretchr/testify/assert"
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

func TestBudgetDecision_String(t *testing.T) {
	assert.Equal(t, "allow", BudgetAllow.String())
	assert.Equal(t, "reject", BudgetReject.String())
	assert.Equal(t, "unknown", BudgetDecision(99).String())
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
