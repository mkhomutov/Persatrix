package cost

import (
	"sync"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

func TestCostReporter_WorkflowSummary_WithSteps(t *testing.T) {
	cfg := testConfig()
	tc := NewTokenCounter(cfg, zap.NewNop())
	reporter := NewCostReporter(tc, cfg)

	// Record usage via counter (aggregated totals).
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 500,
	})
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-b", Model: "claude-haiku",
		InputTokens: 2000, OutputTokens: 1000,
	})

	// Record per-step entries via reporter.
	reporter.RecordStepCost("wf-1", StepCostEntry{
		StepID: "step-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 500, EstimatedUSD: 0.0105,
	})
	reporter.RecordStepCost("wf-1", StepCostEntry{
		StepID: "step-2", AgentID: "agent-b", Model: "claude-haiku",
		InputTokens: 2000, OutputTokens: 1000, EstimatedUSD: 0.0056,
	})

	summary := reporter.WorkflowSummary("wf-1")
	assert.Equal(t, "wf-1", summary.WorkflowID)
	assert.Equal(t, int64(3000), summary.TotalInput)
	assert.Equal(t, int64(1500), summary.TotalOutput)
	assert.InDelta(t, 0.0105+0.0056, summary.TotalEstimated, 1e-9)
	require.Len(t, summary.Steps, 2)
	assert.Equal(t, "step-1", summary.Steps[0].StepID)
	assert.Equal(t, "step-2", summary.Steps[1].StepID)
}

func TestCostReporter_WorkflowSummary_NoSteps(t *testing.T) {
	cfg := testConfig()
	tc := NewTokenCounter(cfg, zap.NewNop())
	reporter := NewCostReporter(tc, cfg)

	summary := reporter.WorkflowSummary("nonexistent")
	assert.Equal(t, "nonexistent", summary.WorkflowID)
	assert.Equal(t, int64(0), summary.TotalInput)
	assert.Equal(t, int64(0), summary.TotalOutput)
	assert.Equal(t, 0.0, summary.TotalEstimated)
	assert.Empty(t, summary.Steps)
}

func TestCostReporter_GlobalSummary_DailyTotals(t *testing.T) {
	cfg := testConfig()
	tc := NewTokenCounter(cfg, zap.NewNop())
	reporter := NewCostReporter(tc, cfg)

	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 500,
	})
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-2", AgentID: "agent-b", Model: "claude-haiku",
		InputTokens: 2000, OutputTokens: 1000,
	})

	summary := reporter.GlobalSummary()
	assert.Equal(t, int64(3000), summary.DailyInputTokens)
	assert.Equal(t, int64(1500), summary.DailyOutputTokens)
	assert.InDelta(t, 0.0105+0.0056, summary.DailyEstimatedUSD, 1e-9)
	assert.False(t, summary.ReportedAt.IsZero())
}

func TestCostReporter_GlobalSummary_TopAgents(t *testing.T) {
	cfg := testConfig()
	tc := NewTokenCounter(cfg, zap.NewNop())
	reporter := NewCostReporter(tc, cfg)

	// agent-a spends more (sonnet pricing) than agent-b (haiku pricing).
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 5000, OutputTokens: 2000,
	})
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-b", Model: "claude-haiku",
		InputTokens: 5000, OutputTokens: 2000,
	})

	summary := reporter.GlobalSummary()
	require.Len(t, summary.TopAgents, 2)
	// Agent-a (sonnet) should be first — higher spend.
	assert.Equal(t, "agent-a", summary.TopAgents[0].AgentID)
	assert.Equal(t, "agent-b", summary.TopAgents[1].AgentID)
	assert.Greater(t, summary.TopAgents[0].EstimatedUSD, summary.TopAgents[1].EstimatedUSD)
}

func TestCostReporter_GlobalSummary_Empty(t *testing.T) {
	cfg := testConfig()
	tc := NewTokenCounter(cfg, zap.NewNop())
	reporter := NewCostReporter(tc, cfg)

	summary := reporter.GlobalSummary()
	assert.Equal(t, int64(0), summary.DailyInputTokens)
	assert.Equal(t, int64(0), summary.DailyOutputTokens)
	assert.Equal(t, 0.0, summary.DailyEstimatedUSD)
	assert.Empty(t, summary.TopAgents)
}

func TestCostReporter_RecordStepCost_MultipleWorkflows(t *testing.T) {
	cfg := testConfig()
	tc := NewTokenCounter(cfg, zap.NewNop())
	reporter := NewCostReporter(tc, cfg)

	reporter.RecordStepCost("wf-1", StepCostEntry{StepID: "s1", AgentID: "a"})
	reporter.RecordStepCost("wf-2", StepCostEntry{StepID: "s2", AgentID: "b"})
	reporter.RecordStepCost("wf-1", StepCostEntry{StepID: "s3", AgentID: "c"})

	wf1 := reporter.WorkflowSummary("wf-1")
	wf2 := reporter.WorkflowSummary("wf-2")
	assert.Len(t, wf1.Steps, 2)
	assert.Len(t, wf2.Steps, 1)
}

func TestSortAgentsBySpend(t *testing.T) {
	agents := []AgentCostEntry{
		{AgentID: "low", EstimatedUSD: 0.01},
		{AgentID: "high", EstimatedUSD: 0.10},
		{AgentID: "mid", EstimatedUSD: 0.05},
	}
	sortAgentsBySpend(agents)
	assert.Equal(t, "high", agents[0].AgentID)
	assert.Equal(t, "mid", agents[1].AgentID)
	assert.Equal(t, "low", agents[2].AgentID)
}

// TestCostReporter_Concurrent validates that CostReporter's mutex protection
// is correct under concurrent access. Run with -race to detect data races.
func TestCostReporter_Concurrent(t *testing.T) {
	cfg := testConfig()
	tc := NewTokenCounter(cfg, zap.NewNop())
	reporter := NewCostReporter(tc, cfg)

	const goroutines = 20
	const opsPerGoroutine = 50

	var wg sync.WaitGroup
	wg.Add(goroutines)

	for i := 0; i < goroutines; i++ {
		go func(id int) {
			defer wg.Done()
			for j := 0; j < opsPerGoroutine; j++ {
				// Mix writes and reads concurrently.
				tc.RecordUsage(UsageRecord{
					WorkflowID:   "wf-concurrent",
					AgentID:      "agent-concurrent",
					Model:        "claude-sonnet",
					InputTokens:  10,
					OutputTokens: 5,
				})
				reporter.RecordStepCost("wf-concurrent", StepCostEntry{
					StepID:       "step",
					AgentID:      "agent-concurrent",
					Model:        "claude-sonnet",
					InputTokens:  10,
					OutputTokens: 5,
					EstimatedUSD: 0.001,
				})
				_ = reporter.WorkflowSummary("wf-concurrent")
				_ = reporter.GlobalSummary()
			}
		}(i)
	}

	wg.Wait()

	// Verify data is present (exact values don't matter — the test validates
	// the absence of data races under -race).
	summary := reporter.GlobalSummary()
	assert.Greater(t, summary.DailyInputTokens, int64(0))
	assert.Greater(t, summary.DailyOutputTokens, int64(0))

	wfSummary := reporter.WorkflowSummary("wf-concurrent")
	assert.Greater(t, len(wfSummary.Steps), 0)
}

func TestCostReporter_ResetDaily(t *testing.T) {
	cfg := testConfig()
	tc := NewTokenCounter(cfg, zap.NewNop())
	reporter := NewCostReporter(tc, cfg)

	reporter.RecordStepCost("wf-1", StepCostEntry{StepID: "s1", AgentID: "a"})
	reporter.RecordStepCost("wf-2", StepCostEntry{StepID: "s2", AgentID: "b"})
	require.Len(t, reporter.WorkflowSummary("wf-1").Steps, 1)
	require.Len(t, reporter.WorkflowSummary("wf-2").Steps, 1)

	reporter.ResetDaily()

	assert.Empty(t, reporter.WorkflowSummary("wf-1").Steps)
	assert.Empty(t, reporter.WorkflowSummary("wf-2").Steps)
}
