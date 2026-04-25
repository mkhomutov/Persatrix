package scheduler

import (
	"context"
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

// budgetDepletionYAML defines a two-stage sequential workflow where step 2
// depends on step 1. Used to test that budget exhaustion in stage 1 causes
// stage 2 to be rejected by the budget enforcer.
const budgetDepletionYAML = `schema_version: "0.1"
workflow:
  id: "budget-depletion"
  name: "Budget Depletion"
  trigger: "manual"
  steps:
    - id: "step1"
      agent: "test-agent"
      input: "first task"
      output_key: "out1"
    - id: "step2"
      agent: "test-agent"
      input: "{{ steps.out1.output }}"
      output_key: "out2"
      depends_on: ["step1"]
`

// TestResolveAgentModel_RegistryError verifies graceful degradation when the
// registry is non-nil but Get() returns an error (e.g., agent deregistered
// between scheduling and dispatch). resolveAgentModel should return "" and
// the budget check should proceed with a zero-cost estimate (no panic).
// (PR #86 review S-01)
func TestResolveAgentModel_RegistryError(t *testing.T) {
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

	// Register agent WITHOUT the "test-agent" ID used in singleStepYAML.
	// The registry is non-nil, but Get("test-agent") will return ErrAgentNotFound.
	reg := newTestRegistry(t)
	require.NoError(t, reg.Register(context.Background(), registry.AgentInfo{
		ID: "other-agent", Name: "other-agent", Address: "passthrough:///test",
		Status: registry.StatusHealthy, Model: "claude-sonnet",
	}))

	sched := newTestSchedulerWithRegistry(t, store, exec, reg, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "reg-error", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	// Should complete — budget check passes with zero-cost estimate for unknown agent.
	run := waitForRunStatus(t, store, "reg-error", state.RunCompleted, 5*time.Second)
	assert.Equal(t, state.RunCompleted, run.Status)

	// Tokens should still be recorded (model comes from response metadata, not registry).
	input, output, _ := tc.GlobalUsage()
	assert.Equal(t, int64(100), input)
	assert.Equal(t, int64(50), output)
}

// TestMultiStepBudgetDepletion validates the full budget lifecycle:
// step 1 dispatches and records enough token usage to exhaust the per-workflow
// budget, then step 2 (in a later stage) is rejected by the budget enforcer.
// (PR #86 review S-02)
func TestMultiStepBudgetDepletion(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "budget-depletion", budgetDepletionYAML)

	cfg := testCostConfig()
	// Set a per-workflow budget that step 1's token usage will exhaust.
	// Step 1 reports 100k output tokens on claude-sonnet ($15/1M output):
	// cost = 100_000 / 1_000_000 * 15.00 = $1.50
	// Per-workflow budget: $1.00 — so step 2 should be rejected.
	cfg.Budgets.PerWorkflow.DefaultMaxUSD = 1.00

	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())
	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	var stepCalls atomic.Int64
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		call := stepCalls.Add(1)
		if call == 1 {
			// Step 1: return a large token count that exhausts the budget.
			return &executor.ExecuteResult{
				TaskID: "t1",
				Output: "step1-done",
				Metadata: map[string]string{
					"input_tokens":  "10000",
					"output_tokens": "100000",
					"model":         "claude-sonnet",
				},
			}, nil
		}
		// Step 2 should never reach the executor.
		return &executor.ExecuteResult{TaskID: "t2", Output: "step2-done"}, nil
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
	createPendingRun(t, store, "depletion-run", "budget-depletion", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "depletion-run", state.RunFailed, 5*time.Second)
	assert.Equal(t, state.RunFailed, run.Status)
	assert.Contains(t, run.Error, "budget exceeded")

	// Step 1 should have completed, step 2 should have been rejected.
	assert.Equal(t, int64(1), stepCalls.Load(), "only step 1 should reach executor")

	step1, ok := run.Steps["step1"]
	require.True(t, ok)
	assert.Equal(t, state.RunCompleted, step1.Status)

	step2, ok := run.Steps["step2"]
	require.True(t, ok)
	assert.Equal(t, state.RunFailed, step2.Status)
	assert.Contains(t, step2.Error, "budget exceeded")

	// CostReporter should have step-level data only for step 1.
	summary := cr.WorkflowSummary("budget-depletion")
	require.Len(t, summary.Steps, 1)
	assert.Equal(t, "step1", summary.Steps[0].StepID)
}

// TestModelResolutionFallback_MetadataEmpty_RegistrySucceeds validates the
// model resolution fallback path in recordStepUsage: when executor response
// metadata has no "model" key but the registry has a model configured for
// the agent, the registry model is used for cost estimation.
// (PR #86 review S-03)
func TestModelResolutionFallback_MetadataEmpty_RegistrySucceeds(t *testing.T) {
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
				// No "model" key — forces fallback to registry.
				"input_tokens":  "1000",
				"output_tokens": "500",
			},
		}, nil
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
	createPendingRun(t, store, "fallback-model", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	waitForRunStatus(t, store, "fallback-model", state.RunCompleted, 5*time.Second)

	// CostReporter step entry should use the registry model ("claude-sonnet").
	summary := cr.WorkflowSummary("test-wf")
	require.Len(t, summary.Steps, 1)
	assert.Equal(t, "claude-sonnet", summary.Steps[0].Model, "model should come from registry fallback")
	assert.Equal(t, int64(1000), summary.Steps[0].InputTokens)
	assert.Equal(t, int64(500), summary.Steps[0].OutputTokens)
	assert.Greater(t, summary.Steps[0].EstimatedUSD, 0.0, "cost should be non-zero with priced model")
}

// TestParseMetadataInt64_NegativeValue verifies that negative token values are
// clamped to zero with a warning log. This prevents adversarial agents from
// reporting negative tokens to decrease the running budget total.
// (PR #86 review F-01: security fix)
func TestParseMetadataInt64_NegativeValue(t *testing.T) {
	core, logs := observer.New(zap.WarnLevel)
	logger := zap.New(core)

	metadata := map[string]string{
		"input_tokens": "-500",
	}

	result := parseMetadataInt64(metadata, "input_tokens", logger, "step-neg")
	assert.Equal(t, int64(0), result)

	// Verify warning log was emitted for the clamped value.
	require.Equal(t, 1, logs.Len())
	entry := logs.All()[0]
	assert.Equal(t, zap.WarnLevel, entry.Level)
	assert.Contains(t, entry.Message, "negative token value clamped to zero")
	assert.Equal(t, "step-neg", entry.ContextMap()["step_id"])
	assert.Equal(t, "input_tokens", entry.ContextMap()["key"])
	assert.Equal(t, int64(-500), entry.ContextMap()["value"])
}

// TestUnpricedModel_DebugLog validates the S-04 diagnostic log: when a step
// response contains a model not in the pricing table with non-zero tokens,
// a Debug-level log is emitted with "model not in pricing table". This prevents
// accidental removal of the observability signal that helps operators detect
// unpriced models. (PR #86 review S-04)
func TestUnpricedModel_DebugLog(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := testCostConfig()
	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())

	// Use DebugLevel observer on the scheduler logger to capture Debug-level logs.
	core, logs := observer.New(zap.DebugLevel)
	schedLogger := zap.New(core)

	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			TaskID: "t1",
			Output: "ok",
			Metadata: map[string]string{
				"input_tokens":  "1000",
				"output_tokens": "500",
				"model":         "unknown-model-xyz",
			},
		}, nil
	}}

	plan := planner.NewYAMLPlanner(zap.NewNop())
	sched := NewWorkflowScheduler(store, nil, plan, exec, schedLogger, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "unpriced-run", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	waitForRunStatus(t, store, "unpriced-run", state.RunCompleted, 5*time.Second)

	// Find the Debug-level log about unpriced model.
	var found bool
	for _, entry := range logs.All() {
		if entry.Message == "model not in pricing table, step cost recorded as $0" {
			found = true
			assert.Equal(t, zap.DebugLevel, entry.Level)
			assert.Equal(t, "unknown-model-xyz", entry.ContextMap()["model"])
			assert.Equal(t, "step1", entry.ContextMap()["step_id"])
			break
		}
	}
	assert.True(t, found, "expected Debug log for unpriced model")

	// Step cost should be recorded with $0 for unknown model.
	summary := cr.WorkflowSummary("test-wf")
	require.Len(t, summary.Steps, 1)
	assert.Equal(t, 0.0, summary.Steps[0].EstimatedUSD)
	assert.Equal(t, "unknown-model-xyz", summary.Steps[0].Model)
}

// TestTokensUsedFallback_LogMessage validates that when the legacy "tokens_used"
// fallback path is taken, an Info-level log is emitted so operators can identify
// agents that need to provide granular input_tokens/output_tokens data.
// (PR #86 review: observability regression protection)
func TestTokensUsedFallback_LogMessage(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := testCostConfig()
	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())

	// Use InfoLevel observer to capture the fallback log.
	core, logs := observer.New(zap.InfoLevel)
	schedLogger := zap.New(core)

	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			TaskID: "t1",
			Output: "ok",
			Metadata: map[string]string{
				"tokens_used": "750",
				"model":       "claude-sonnet",
			},
		}, nil
	}}

	plan := planner.NewYAMLPlanner(zap.NewNop())
	sched := NewWorkflowScheduler(store, nil, plan, exec, schedLogger, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "fallback-log", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	waitForRunStatus(t, store, "fallback-log", state.RunCompleted, 5*time.Second)

	// Find the Info-level log about tokens_used fallback.
	var found bool
	for _, entry := range logs.All() {
		if entry.Message == "using tokens_used fallback (all tokens mapped to output, cost may be overestimated)" {
			found = true
			assert.Equal(t, zap.InfoLevel, entry.Level)
			assert.Equal(t, "step1", entry.ContextMap()["step_id"])
			assert.Equal(t, "test-agent", entry.ContextMap()["agent_id"])
			assert.Equal(t, int64(750), entry.ContextMap()["tokensUsed"])
			break
		}
	}
	assert.True(t, found, "expected Info log for tokens_used fallback")
}
