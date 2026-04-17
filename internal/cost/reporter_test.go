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

func TestCostReporter_WorkflowSummary_WithSteps(t *testing.T) {
	cfg := testConfig()
	tc := NewTokenCounter(cfg, zap.NewNop())
	reporter := NewCostReporter(tc, cfg, zap.NewNop())

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
	reporter := NewCostReporter(tc, cfg, zap.NewNop())

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
	reporter := NewCostReporter(tc, cfg, zap.NewNop())

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
	reporter := NewCostReporter(tc, cfg, zap.NewNop())

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
	reporter := NewCostReporter(tc, cfg, zap.NewNop())

	summary := reporter.GlobalSummary()
	assert.Equal(t, int64(0), summary.DailyInputTokens)
	assert.Equal(t, int64(0), summary.DailyOutputTokens)
	assert.Equal(t, 0.0, summary.DailyEstimatedUSD)
	assert.Empty(t, summary.TopAgents)
}

func TestCostReporter_RecordStepCost_MultipleWorkflows(t *testing.T) {
	cfg := testConfig()
	tc := NewTokenCounter(cfg, zap.NewNop())
	reporter := NewCostReporter(tc, cfg, zap.NewNop())

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
	reporter := NewCostReporter(tc, cfg, zap.NewNop())

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
	reporter := NewCostReporter(tc, cfg, zap.NewNop())

	reporter.RecordStepCost("wf-1", StepCostEntry{StepID: "s1", AgentID: "a"})
	reporter.RecordStepCost("wf-2", StepCostEntry{StepID: "s2", AgentID: "b"})
	require.Len(t, reporter.WorkflowSummary("wf-1").Steps, 1)
	require.Len(t, reporter.WorkflowSummary("wf-2").Steps, 1)

	reporter.ResetDaily()

	assert.Empty(t, reporter.WorkflowSummary("wf-1").Steps)
	assert.Empty(t, reporter.WorkflowSummary("wf-2").Steps)
}

// TestCostReporter_ResetDaily_Concurrent validates that ResetDaily is safe
// to call concurrently with RecordStepCost and WorkflowSummary reads.
// Run with -race to detect data races.
func TestCostReporter_ResetDaily_Concurrent(t *testing.T) {
	cfg := testConfig()
	tc := NewTokenCounter(cfg, zap.NewNop())
	reporter := NewCostReporter(tc, cfg, zap.NewNop())

	const goroutines = 10
	const opsPerGoroutine = 50

	var wg sync.WaitGroup
	wg.Add(goroutines)

	for i := 0; i < goroutines; i++ {
		go func(id int) {
			defer wg.Done()
			for j := 0; j < opsPerGoroutine; j++ {
				// Mix writes, reads, and resets concurrently.
				reporter.RecordStepCost("wf-reset", StepCostEntry{
					StepID:  "step",
					AgentID: "agent",
				})
				_ = reporter.WorkflowSummary("wf-reset")
				_ = reporter.GlobalSummary()
				if j%10 == 0 {
					reporter.ResetDaily()
				}
			}
		}(i)
	}

	wg.Wait()
	// No data race detected = pass. Exact values don't matter.
}

// TestSortAgentsBySpend_EqualSpend verifies deterministic ordering when
// agents have identical spend (secondary sort by AgentID ascending).
func TestSortAgentsBySpend_EqualSpend(t *testing.T) {
	agents := []AgentCostEntry{
		{AgentID: "charlie", EstimatedUSD: 0.05},
		{AgentID: "alpha", EstimatedUSD: 0.05},
		{AgentID: "bravo", EstimatedUSD: 0.05},
	}
	sortAgentsBySpend(agents)
	assert.Equal(t, "alpha", agents[0].AgentID)
	assert.Equal(t, "bravo", agents[1].AgentID)
	assert.Equal(t, "charlie", agents[2].AgentID)
}

// TestCostReporter_ResetDaily_ResetsCounter verifies that CostReporter.ResetDaily()
// also resets the underlying TokenCounter, preventing state divergence where
// per-step data is cleared but aggregated totals remain stale.
// (PR #86 review S-05: coordinate ResetDaily calls)
func TestCostReporter_ResetDaily_ResetsCounter(t *testing.T) {
	cfg := testConfig()
	tc := NewTokenCounter(cfg, zap.NewNop())
	reporter := NewCostReporter(tc, cfg, zap.NewNop())

	// Record usage in both components.
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 500,
	})
	reporter.RecordStepCost("wf-1", StepCostEntry{
		StepID: "step-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 500, EstimatedUSD: 0.0105,
	})

	// Verify data exists before reset.
	input, output, _ := tc.GlobalUsage()
	require.Greater(t, input, int64(0))
	require.Greater(t, output, int64(0))
	require.Len(t, reporter.WorkflowSummary("wf-1").Steps, 1)

	// Single ResetDaily call should clear both components.
	reporter.ResetDaily()

	// TokenCounter should be reset.
	input, output, usd := tc.GlobalUsage()
	assert.Equal(t, int64(0), input)
	assert.Equal(t, int64(0), output)
	assert.Equal(t, 0.0, usd)

	// Reporter step data should be reset.
	assert.Empty(t, reporter.WorkflowSummary("wf-1").Steps)
}

// TestNewCostReporter_NilCounter_Panics verifies that NewCostReporter panics
// when given a nil TokenCounter, since a nil counter would cause nil-pointer
// panics in WorkflowSummary, GlobalSummary, and ResetDaily.
// (PR #86 review: must-fix nil-guard)
func TestNewCostReporter_NilCounter_Panics(t *testing.T) {
	cfg := testConfig()
	assert.Panics(t, func() {
		NewCostReporter(nil, cfg, zap.NewNop())
	}, "NewCostReporter should panic with nil counter")
}

// TestCostReporter_WorkflowCountWarning verifies that RecordStepCost emits
// a warning when the number of tracked workflows exceeds the threshold.
// This tests the operational safety guard for unbounded perWorkflowSteps growth.
// (PR #86 review: perWorkflowSteps growth concern)
func TestCostReporter_WorkflowCountWarning(t *testing.T) {
	cfg := testConfig()
	tc := NewTokenCounter(cfg, zap.NewNop())

	core, logs := observer.New(zap.WarnLevel)
	reporterLogger := zap.New(core)
	reporter := NewCostReporter(tc, cfg, reporterLogger)

	// Record step costs for more than 10000 unique workflows.
	for i := 0; i <= 10000; i++ {
		wfID := fmt.Sprintf("wf-%d", i)
		reporter.RecordStepCost(wfID, StepCostEntry{StepID: "s1", AgentID: "a"})
	}

	// Verify warning was emitted.
	var found bool
	for _, entry := range logs.All() {
		if entry.Message == "high workflow count in cost reporter, consider increasing ResetDaily frequency" {
			found = true
			assert.Equal(t, zap.WarnLevel, entry.Level)
			break
		}
	}
	assert.True(t, found, "expected warning log for high workflow count")

	// After reset, the warning should be re-armed.
	reporter.ResetDaily()
	initialLogCount := logs.Len()

	for i := 0; i <= 10000; i++ {
		wfID := fmt.Sprintf("wf-post-reset-%d", i)
		reporter.RecordStepCost(wfID, StepCostEntry{StepID: "s1", AgentID: "a"})
	}

	assert.Greater(t, logs.Len(), initialLogCount, "warning should fire again after reset")
}

// --- S-06: NewCostReporter with nil config ---

// TestNewCostReporter_NilConfig verifies that the constructor handles nil config
// gracefully. The config parameter is retained for future use (e.g., per-workflow
// budget display in summaries) but is not currently referenced.
func TestNewCostReporter_NilConfig(t *testing.T) {
	cfg := testConfig()
	tc := NewTokenCounter(cfg, zap.NewNop())

	// Should not panic with nil config.
	reporter := NewCostReporter(tc, nil, zap.NewNop())
	require.NotNil(t, reporter)

	// Basic operations should work normally.
	reporter.RecordStepCost("wf-1", StepCostEntry{
		StepID: "step-1", AgentID: "agent-a",
	})

	summary := reporter.WorkflowSummary("wf-1")
	assert.Len(t, summary.Steps, 1)

	global := reporter.GlobalSummary()
	assert.NotNil(t, global)
}

// --- S-07: sortAgentsBySpend with empty slice ---

func TestSortAgentsBySpend_EmptySlice(t *testing.T) {
	agents := []AgentCostEntry{}
	sortAgentsBySpend(agents)
	assert.Empty(t, agents)
}
