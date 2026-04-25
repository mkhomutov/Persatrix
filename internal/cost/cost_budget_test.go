package cost

import (
	"fmt"
	"sync"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"
)

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

	result := be.CheckBudget("wf-1", "agent-a", "claude-sonnet", 1_000_000)
	assert.Equal(t, BudgetAllow, result.Decision)
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

	constructorWarns := logs.FilterMessage("on_exceed: pause_and_alert is not implemented, treating as fail")
	assert.Equal(t, 1, constructorWarns.Len(), "expected constructor warning for pause_and_alert")

	result := be.CheckBudget("wf-1", "agent-a", "claude-sonnet", 8192)
	assert.Equal(t, BudgetReject, result.Decision)

	enforcementWarns := logs.FilterMessage("pause_and_alert not implemented, rejecting dispatch")
	assert.Equal(t, 1, enforcementWarns.Len(), "expected enforcement-time warning for pause_and_alert")
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
			_ = be.CheckBudget(
				fmt.Sprintf("wf-%d", i%5),
				fmt.Sprintf("agent-%d", i%3),
				"claude-sonnet",
				1000,
			)
		}(i)
	}

	wg.Wait()

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
	cfg.Budgets.Global.MaxDailyUSD = 0.10
	tc := NewTokenCounter(cfg, zap.NewNop())
	be := NewBudgetEnforcer(tc, cfg, zap.NewNop())

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

	allowCount := 0
	for _, r := range results {
		if r.Decision == BudgetAllow {
			allowCount++
		}
	}
	assert.Equal(t, parallelSteps, allowCount,
		"all parallel checks should pass (optimistic check, no prior usage)")

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
