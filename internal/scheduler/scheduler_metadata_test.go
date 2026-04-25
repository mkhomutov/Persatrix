package scheduler

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"

	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

func TestStepMetadata_PopulatedOnCompletion(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			TaskID:     "t1",
			Output:     "done",
			RetryCount: 2,
			WallTimeMs: 1500,
			Metadata: map[string]string{
				"tokens_used":    "800",
				"llm_call_count": "3",
			},
		}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir,
		WithPollInterval(50*time.Millisecond),
	)
	createPendingRun(t, store, "meta-run", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "meta-run", state.RunCompleted, 5*time.Second)

	step := run.Steps["step1"]
	require.NotNil(t, step.Metadata, "step should have execution metadata")
	assert.Equal(t, 800, step.Metadata.TokensUsed)
	assert.Equal(t, 3, step.Metadata.LLMCallCount)
	assert.Equal(t, 2, step.Metadata.RetryCount)
	assert.False(t, step.Metadata.CacheHit)
	assert.Equal(t, int64(1500), step.Metadata.WallTimeMs)
}

func TestStepMetadata_GracefulDegradation(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		// No metadata fields — simulates an agent that doesn't report observability data.
		return &executor.ExecuteResult{
			TaskID:     "t1",
			Output:     "done",
			WallTimeMs: 200,
		}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir,
		WithPollInterval(50*time.Millisecond),
	)
	createPendingRun(t, store, "meta-empty", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "meta-empty", state.RunCompleted, 5*time.Second)

	step := run.Steps["step1"]
	require.NotNil(t, step.Metadata, "step should have metadata even with no agent-reported fields")
	assert.Equal(t, 0, step.Metadata.TokensUsed)
	assert.Equal(t, 0, step.Metadata.LLMCallCount)
	assert.Equal(t, 0, step.Metadata.RetryCount)
	assert.False(t, step.Metadata.CacheHit)
	assert.Equal(t, int64(200), step.Metadata.WallTimeMs)
}

func TestStepMetadata_PerDirectionTokens(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			TaskID:     "t1",
			Output:     "done",
			WallTimeMs: 500,
			Metadata: map[string]string{
				"input_tokens":  "300",
				"output_tokens": "200",
			},
		}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir,
		WithPollInterval(50*time.Millisecond),
	)
	createPendingRun(t, store, "meta-dir", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "meta-dir", state.RunCompleted, 5*time.Second)

	step := run.Steps["step1"]
	require.NotNil(t, step.Metadata)
	assert.Equal(t, 500, step.Metadata.TokensUsed, "should sum input + output tokens")
}

func TestStepMetadata_InfoLogOnCompletion(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			TaskID:     "t1",
			Output:     "done",
			RetryCount: 1,
			WallTimeMs: 750,
			Metadata: map[string]string{
				"tokens_used": "500",
			},
		}, nil
	}}

	// Use observed logger to capture log output.
	core, logs := observer.New(zap.InfoLevel)
	logger := zap.New(core)
	plan := planner.NewYAMLPlanner(logger)
	sched := NewWorkflowScheduler(store, nil, plan, exec, logger, dir,
		WithPollInterval(50*time.Millisecond),
	)
	createPendingRun(t, store, "meta-log", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	waitForRunStatus(t, store, "meta-log", state.RunCompleted, 5*time.Second)

	var found bool
	for _, entry := range logs.All() {
		if entry.Message == "step completed" {
			found = true
			assert.Equal(t, "step1", entry.ContextMap()["step_id"])
			assert.Equal(t, int64(500), entry.ContextMap()["tokensUsed"])
			assert.Equal(t, int64(1), entry.ContextMap()["retryCount"])
			assert.Equal(t, int64(750), entry.ContextMap()["wallTimeMs"])
			break
		}
	}
	assert.True(t, found, "expected 'step completed' Info log with metadata fields")
}

// TestStepMetadata_CostParity_TokensUsedOnly verifies that buildStepMetadata and
// recordStepUsage produce the same estimated cost when only tokens_used is
// reported (no input_tokens/output_tokens). This is the M-01 regression test:
// before the fix, buildStepMetadata called EstimateCost(model, 0, 0) → $0.00
// while recordStepUsage correctly mapped tokens_used → outputTokens.
func TestStepMetadata_CostParity_TokensUsedOnly(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := &cost.CostConfig{
		Pricing: map[string]cost.ModelPricing{
			"claude-sonnet": {InputPer1MTokens: 3.00, OutputPer1MTokens: 15.00},
		},
		Budgets: cost.BudgetThresholds{
			Global:      cost.GlobalBudget{MaxDailyUSD: 100, OnExceed: "fail"},
			PerWorkflow: cost.PerWorkflowBudget{DefaultMaxUSD: 50},
			PerAgent:    cost.PerAgentBudget{DefaultMaxUSD: 25},
		},
	}
	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())
	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			TaskID:     "t1",
			Output:     "done",
			WallTimeMs: 500,
			Metadata: map[string]string{
				"tokens_used": "800",
				"model":       "claude-sonnet",
			},
		}, nil
	}}

	plan := planner.NewYAMLPlanner(zap.NewNop())
	sched := NewWorkflowScheduler(store, nil, plan, exec, zap.NewNop(), dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "cost-parity", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "cost-parity", state.RunCompleted, 5*time.Second)

	step := run.Steps["step1"]
	require.NotNil(t, step.Metadata, "step should have execution metadata")

	// The metadata cost should be non-zero: 800 tokens_used mapped to outputTokens,
	// priced at 15.00/1M = 800 * 15.00 / 1_000_000 = 0.012.
	assert.Greater(t, step.Metadata.EstimatedCostUSD, 0.0,
		"M-01: estimated cost should be non-zero when tokens_used > 0 and model has pricing")
	expectedCost := 800.0 * 15.00 / 1_000_000.0
	assert.InDelta(t, expectedCost, step.Metadata.EstimatedCostUSD, 0.0001,
		"metadata cost should match tokens_used → outputTokens pessimistic estimate")

	// Verify CostReporter recorded the same cost (parity check).
	report := cr.WorkflowSummary("test-wf")
	require.Len(t, report.Steps, 1, "should have one step cost entry")
	assert.InDelta(t, expectedCost, report.Steps[0].EstimatedUSD, 0.0001,
		"reporter cost should match metadata cost (parity)")
}

// TestResolveStepLimits_NegativeAgentLimits verifies that negative agent-level
// limits produce a warning log and fall through to system defaults. (PR 5a, F-04)
func TestResolveStepLimits_NegativeAgentLimits(t *testing.T) {
	core, logs := observer.New(zap.WarnLevel)
	logger := zap.New(core)

	reg := registry.NewInMemoryRegistry(zap.NewNop())
	require.NoError(t, reg.Register(context.Background(), registry.AgentInfo{
		ID:             "neg-agent",
		Name:           "neg-agent",
		Address:        "localhost:0",
		Status:         registry.StatusHealthy,
		MaxLLMCalls:    -5,
		MaxTokens:      -1000,
		TimeoutSeconds: -30,
	}))

	plan := planner.NewYAMLPlanner(zap.NewNop())
	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{TaskID: "t1", Output: "ok"}, nil
	}}
	sched := NewWorkflowScheduler(store, reg, plan, exec, logger, t.TempDir())

	limits := sched.resolveStepLimits(context.Background(), planner.Step{
		ID:      "s1",
		AgentID: "neg-agent",
	})

	// Negative values should fall through to defaults.
	assert.Equal(t, 5, limits.MaxLLMCalls, "should use default MaxLLMCalls")
	assert.Equal(t, 8192, limits.MaxTokens, "should use default MaxTokens")
	assert.Equal(t, 60, limits.TimeoutSeconds, "should use default TimeoutSeconds")

	// Verify warning logs were emitted.
	warnMessages := make(map[string]bool)
	for _, entry := range logs.All() {
		warnMessages[entry.Message] = true
	}
	assert.True(t, warnMessages["negative agent-level MaxLLMCalls, using default"],
		"expected warning for negative MaxLLMCalls")
	assert.True(t, warnMessages["negative agent-level MaxTokens, using default"],
		"expected warning for negative MaxTokens")
	assert.True(t, warnMessages["negative agent-level TimeoutSeconds, using default"],
		"expected warning for negative TimeoutSeconds")
}
