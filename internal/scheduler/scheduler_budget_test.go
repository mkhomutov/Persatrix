package scheduler

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
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

func testCostConfig() *cost.CostConfig {
	return &cost.CostConfig{
		Pricing: map[string]cost.ModelPricing{
			"claude-sonnet": {
				InputPer1MTokens:  3.00,
				OutputPer1MTokens: 15.00,
			},
		},
		Budgets: cost.BudgetThresholds{
			Global:      cost.GlobalBudget{MaxDailyUSD: 100.00, OnExceed: "fail"},
			PerWorkflow: cost.PerWorkflowBudget{DefaultMaxUSD: 10.00},
			PerAgent:    cost.PerAgentBudget{DefaultMaxUSD: 5.00},
		},
	}
}

func TestBudgetCheck_UnderBudget_DispatchProceeds(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := testCostConfig()
	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())
	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			TaskID: "t1",
			Output: "ok",
			Metadata: map[string]string{
				"input_tokens":  "100",
				"output_tokens": "50",
				"model":         "claude-sonnet",
			},
		}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "budget-ok", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "budget-ok", state.RunCompleted, 5*time.Second)
	assert.Equal(t, state.RunCompleted, run.Status)

	// RFC 0023 PR 3 — the scheduler no longer feeds the budget TokenCounter
	// post-dispatch (the agent-side wallet owns counter recording); per-step
	// usage still reaches the CostReporter.
	summary := cr.WorkflowSummary("test-wf")
	require.Len(t, summary.Steps, 1)
	assert.Equal(t, int64(100), summary.Steps[0].InputTokens)
	assert.Equal(t, int64(50), summary.Steps[0].OutputTokens)
}

func TestBudgetCheck_Rejected_StepFails(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := testCostConfig()
	// Set a tiny per-agent budget that will be exceeded by the estimated cost.
	cfg.Budgets.PerAgent.DefaultMaxUSD = 0.00001

	// Verify the budget check rejects directly (sanity check).
	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())
	directResult := be.CheckBudget("test-wf", "test-agent", "claude-sonnet", 8192)
	require.Equal(t, cost.BudgetReject, directResult.Decision, "sanity: direct budget check should reject")

	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	var executorCalled atomic.Bool
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		executorCalled.Store(true)
		return &executor.ExecuteResult{TaskID: "t1", Output: "ok"}, nil
	}}

	reg := newTestRegistry(t)
	err := reg.Register(context.Background(), registry.AgentInfo{
		ID:      "test-agent",
		Name:    "test-agent",
		Address: "passthrough:///test",
		Status:  registry.StatusHealthy,
		Model:   "claude-sonnet",
	})
	require.NoError(t, err)

	sched := newTestSchedulerWithRegistry(t, store, exec, reg, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "budget-fail", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "budget-fail", state.RunFailed, 5*time.Second)
	assert.Equal(t, state.RunFailed, run.Status)
	assert.Contains(t, run.Error, "budget exceeded")
	assert.False(t, executorCalled.Load(), "executor should not be called when budget is rejected")

	// Step should also be marked as failed.
	step, ok := run.Steps["step1"]
	require.True(t, ok)
	assert.Equal(t, state.RunFailed, step.Status)
	assert.Contains(t, step.Error, "budget exceeded")
}

func TestBudgetCheck_ErrorWrapping(t *testing.T) {
	// Verify that budget rejection errors wrap ErrBudgetExceeded, enabling
	// programmatic detection via errors.Is without string matching.
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := testCostConfig()
	cfg.Budgets.PerAgent.DefaultMaxUSD = 0.00001

	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())
	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{TaskID: "t1", Output: "ok"}, nil
	}}

	reg := newTestRegistry(t)
	require.NoError(t, reg.Register(context.Background(), registry.AgentInfo{
		ID: "test-agent", Name: "test-agent", Address: "passthrough:///test",
		Status: registry.StatusHealthy, Model: "claude-sonnet",
	}))

	sched := newTestSchedulerWithRegistry(t, store, exec, reg, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)

	// Call executeStep directly to inspect the returned error.
	createPendingRun(t, store, "sentinel-run", "test-wf", nil)
	step := planner.Step{ID: "step1", AgentID: "test-agent", Input: "do something"}
	var mu sync.Mutex
	outputs := map[string]string{}
	vars := map[string]string{}

	_, err := sched.executeStep(context.Background(), "sentinel-run", "test-wf", step, outputs, vars, &mu, nil)
	require.Error(t, err)
	assert.True(t, errors.Is(err, ErrBudgetExceeded), "error should wrap ErrBudgetExceeded sentinel")
	assert.Contains(t, err.Error(), "budget exceeded")
	assert.Contains(t, err.Error(), "per_agent budget exceeded")
}

func TestTokenRecording_MissingMetadata_GracefulDegradation(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := testCostConfig()
	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())
	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		// No token metadata in response.
		return &executor.ExecuteResult{TaskID: "t1", Output: "ok"}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "no-meta", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "no-meta", state.RunCompleted, 5*time.Second)
	assert.Equal(t, state.RunCompleted, run.Status)

	// Tokens should be zero (graceful degradation, no panic).
	input, output, _ := tc.GlobalUsage()
	assert.Equal(t, int64(0), input)
	assert.Equal(t, int64(0), output)
}

func TestTokenRecording_TokensUsedFallback(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := testCostConfig()
	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())
	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			TaskID: "t1",
			Output: "ok",
			Metadata: map[string]string{
				"tokens_used": "500",
			},
		}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "fallback-meta", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "fallback-meta", state.RunCompleted, 5*time.Second)
	assert.Equal(t, state.RunCompleted, run.Status)

	// tokens_used maps to output tokens (the resolveStepTokenData fallback).
	// RFC 0023 PR 3 — that resolved usage reaches the CostReporter; the
	// budget TokenCounter is fed by the agent-side wallet, not the scheduler.
	summary := cr.WorkflowSummary("test-wf")
	require.Len(t, summary.Steps, 1)
	assert.Equal(t, int64(500), summary.Steps[0].OutputTokens)
}

// TestTokenRecording_StepFailed_WithPartialMetadata verifies that when a step
// fails and the executor returns a non-nil result containing response metadata
// (e.g. token counts from an LLM call that completed before the step error),
// recordStepUsage is called and those tokens are recorded in the cost summary.
// This is the MT-COST-002 fix: previously tokens from failed steps were silently
// dropped because recordStepUsage was only called on the success path.
func TestTokenRecording_StepFailed_WithPartialMetadata(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := testCostConfig()
	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())
	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	// Executor returns partial metadata alongside the error, simulating an agent
	// that completed its LLM call but failed before returning output (e.g. truncation).
	exec := &mockExecutor{handler: func(_ context.Context, _ executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			Metadata: map[string]string{
				"input_tokens":  "200",
				"output_tokens": "80",
				"model":         "claude-sonnet",
			},
		}, fmt.Errorf("LLM response truncated: max_tokens limit reached")
	}}

	sched := newTestScheduler(t, store, exec, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "partial-meta-run", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "partial-meta-run", state.RunFailed, 5*time.Second)
	assert.Equal(t, state.RunFailed, run.Status)

	// recordStepUsage still runs on the failure path (the MT-COST-002 fix).
	// RFC 0023 PR 3 — the resolved usage now lands on the CostReporter; the
	// budget TokenCounter is fed by the agent-side wallet.
	summary := cr.WorkflowSummary("test-wf")
	require.Len(t, summary.Steps, 1)
	assert.Equal(t, int64(200), summary.Steps[0].InputTokens,
		"input tokens from failed step should be recorded")
	assert.Equal(t, int64(80), summary.Steps[0].OutputTokens,
		"output tokens from failed step should be recorded")
}

// TestTokenRecording_StepFailed_NilResult verifies that when a step fails and
// the executor returns nil alongside the error (e.g. transport-level gRPC
// failure before the agent is reached), no recording attempt is made and no
// panic occurs. This is the pre-existing behaviour preserved by the nil guard.
func TestTokenRecording_StepFailed_NilResult(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := testCostConfig()
	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())
	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, _ executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return nil, fmt.Errorf("agent not found in registry")
	}}

	sched := newTestScheduler(t, store, exec, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "nil-result-run", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "nil-result-run", state.RunFailed, 5*time.Second)
	assert.Equal(t, state.RunFailed, run.Status)

	// No tokens should be recorded when result is nil.
	input, output, _ := tc.GlobalUsage()
	assert.Equal(t, int64(0), input, "no input tokens should be recorded for nil result")
	assert.Equal(t, int64(0), output, "no output tokens should be recorded for nil result")
}

func TestCostReporter_StepCostRecorded(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := testCostConfig()
	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())
	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			TaskID: "t1",
			Output: "ok",
			Metadata: map[string]string{
				"input_tokens":  "1000",
				"output_tokens": "500",
				"model":         "claude-sonnet",
			},
		}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "reporter-run", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	waitForRunStatus(t, store, "reporter-run", state.RunCompleted, 5*time.Second)

	// CostReporter should have step-level data.
	summary := cr.WorkflowSummary("test-wf")
	require.Len(t, summary.Steps, 1)
	assert.Equal(t, "step1", summary.Steps[0].StepID)
	assert.Equal(t, int64(1000), summary.Steps[0].InputTokens)
	assert.Equal(t, int64(500), summary.Steps[0].OutputTokens)
	assert.Equal(t, "claude-sonnet", summary.Steps[0].Model)
	assert.Greater(t, summary.Steps[0].EstimatedUSD, 0.0)
}

func TestNoCostComponents_NoPanic(t *testing.T) {
	// When cost components are nil (not injected), scheduler should work normally.
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{TaskID: "t1", Output: "ok"}, nil
	}}

	// No WithCostComponents — all cost fields remain nil.
	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "no-cost", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "no-cost", state.RunCompleted, 5*time.Second)
	assert.Equal(t, state.RunCompleted, run.Status)
}

func TestParseMetadataInt64_MalformedValue(t *testing.T) {
	core, logs := observer.New(zap.WarnLevel)
	logger := zap.New(core)

	metadata := map[string]string{
		"input_tokens": "not-a-number",
	}

	result := parseMetadataInt64(metadata, "input_tokens", logger, "step-x")
	assert.Equal(t, int64(0), result)

	// Verify warning log was emitted.
	require.Equal(t, 1, logs.Len())
	entry := logs.All()[0]
	assert.Equal(t, zap.WarnLevel, entry.Level)
	assert.Contains(t, entry.Message, "failed to parse metadata value")
	assert.Equal(t, "step-x", entry.ContextMap()["step_id"])
	assert.Equal(t, "input_tokens", entry.ContextMap()["key"])
	assert.Equal(t, "not-a-number", entry.ContextMap()["value"])
}
