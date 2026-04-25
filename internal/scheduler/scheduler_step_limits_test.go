package scheduler

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// --- Test helpers for resolveStepLimits ---

func newTestRegistry(t *testing.T) *registry.InMemoryRegistry {
	t.Helper()
	return registry.NewInMemoryRegistry(zap.NewNop())
}

func registerAgentWithLimits(t *testing.T, reg *registry.InMemoryRegistry, agentID string, maxLLMCalls, maxTokens, timeoutSeconds int) {
	t.Helper()
	err := reg.Register(context.Background(), registry.AgentInfo{
		ID:             agentID,
		Name:           agentID,
		Address:        "passthrough:///test",
		Status:         registry.StatusHealthy,
		MaxLLMCalls:    maxLLMCalls,
		MaxTokens:      maxTokens,
		TimeoutSeconds: timeoutSeconds,
	})
	require.NoError(t, err)
}

func newTestSchedulerWithRegistry(t *testing.T, store state.Store, exec executor.Executor, reg registry.Registry, workflowsDir string, opts ...Option) *WorkflowScheduler {
	t.Helper()
	logger := zap.NewNop()
	plan := planner.NewYAMLPlanner(logger)
	return NewWorkflowScheduler(store, reg, plan, exec, logger, workflowsDir, opts...)
}

func TestResolveStepLimits_SystemDefaults(t *testing.T) {
	// Both step and agent have zero limits → system defaults used.
	store := state.NewInMemoryStore(zap.NewNop())
	reg := newTestRegistry(t)
	registerAgentWithLimits(t, reg, "test-agent", 0, 0, 0)

	sched := newTestSchedulerWithRegistry(t, store, &mockExecutor{}, reg, t.TempDir())
	step := planner.Step{AgentID: "test-agent"}

	limits := sched.resolveStepLimits(context.Background(), step)

	assert.Equal(t, 5, limits.MaxLLMCalls, "should use DefaultMaxLLMCalls")
	assert.Equal(t, 8192, limits.MaxTokens, "should use DefaultMaxTokens")
	assert.Equal(t, 60, limits.TimeoutSeconds, "should use DefaultTimeoutSeconds")
}

func TestResolveStepLimits_AgentOverridesDefaults(t *testing.T) {
	store := state.NewInMemoryStore(zap.NewNop())
	reg := newTestRegistry(t)
	registerAgentWithLimits(t, reg, "custom-agent", 8, 16384, 120)

	sched := newTestSchedulerWithRegistry(t, store, &mockExecutor{}, reg, t.TempDir())
	step := planner.Step{AgentID: "custom-agent"}

	limits := sched.resolveStepLimits(context.Background(), step)

	assert.Equal(t, 8, limits.MaxLLMCalls)
	assert.Equal(t, 16384, limits.MaxTokens)
	assert.Equal(t, 120, limits.TimeoutSeconds)
}

func TestResolveStepLimits_StepOverridesAgent(t *testing.T) {
	store := state.NewInMemoryStore(zap.NewNop())
	reg := newTestRegistry(t)
	registerAgentWithLimits(t, reg, "custom-agent", 8, 16384, 120)

	sched := newTestSchedulerWithRegistry(t, store, &mockExecutor{}, reg, t.TempDir())
	step := planner.Step{
		AgentID:        "custom-agent",
		MaxLLMCalls:    3,
		MaxTokens:      2048,
		TimeoutSeconds: 30,
	}

	limits := sched.resolveStepLimits(context.Background(), step)

	assert.Equal(t, 3, limits.MaxLLMCalls)
	assert.Equal(t, 2048, limits.MaxTokens)
	assert.Equal(t, 30, limits.TimeoutSeconds)
}

func TestResolveStepLimits_PartialStepOverride(t *testing.T) {
	// Step overrides only MaxLLMCalls; the rest come from agent config.
	store := state.NewInMemoryStore(zap.NewNop())
	reg := newTestRegistry(t)
	registerAgentWithLimits(t, reg, "custom-agent", 8, 16384, 120)

	sched := newTestSchedulerWithRegistry(t, store, &mockExecutor{}, reg, t.TempDir())
	step := planner.Step{
		AgentID:     "custom-agent",
		MaxLLMCalls: 2,
	}

	limits := sched.resolveStepLimits(context.Background(), step)

	assert.Equal(t, 2, limits.MaxLLMCalls, "step override")
	assert.Equal(t, 16384, limits.MaxTokens, "from agent config")
	assert.Equal(t, 120, limits.TimeoutSeconds, "from agent config")
}

func TestResolveStepLimits_PartialAgentOverride(t *testing.T) {
	// Agent config sets only TimeoutSeconds; rest fall to system defaults.
	store := state.NewInMemoryStore(zap.NewNop())
	reg := newTestRegistry(t)
	registerAgentWithLimits(t, reg, "partial-agent", 0, 0, 300)

	sched := newTestSchedulerWithRegistry(t, store, &mockExecutor{}, reg, t.TempDir())
	step := planner.Step{AgentID: "partial-agent"}

	limits := sched.resolveStepLimits(context.Background(), step)

	assert.Equal(t, 5, limits.MaxLLMCalls, "system default")
	assert.Equal(t, 8192, limits.MaxTokens, "system default")
	assert.Equal(t, 300, limits.TimeoutSeconds, "agent override")
}

func TestResolveStepLimits_AgentNotInRegistry(t *testing.T) {
	// Agent not found in registry → system defaults used (with warning log).
	store := state.NewInMemoryStore(zap.NewNop())
	reg := newTestRegistry(t) // empty registry

	sched := newTestSchedulerWithRegistry(t, store, &mockExecutor{}, reg, t.TempDir())
	step := planner.Step{AgentID: "unknown-agent"}

	limits := sched.resolveStepLimits(context.Background(), step)

	assert.Equal(t, 5, limits.MaxLLMCalls)
	assert.Equal(t, 8192, limits.MaxTokens)
	assert.Equal(t, 60, limits.TimeoutSeconds)
}

func TestResolveStepLimits_NilRegistry(t *testing.T) {
	// nil registry (e.g., in tests using newTestScheduler which passes nil)
	// → system defaults used, no panic.
	store := state.NewInMemoryStore(zap.NewNop())
	sched := newTestScheduler(t, store, &mockExecutor{}, t.TempDir())
	step := planner.Step{AgentID: "any-agent"}

	limits := sched.resolveStepLimits(context.Background(), step)

	assert.Equal(t, 5, limits.MaxLLMCalls)
	assert.Equal(t, 8192, limits.MaxTokens)
	assert.Equal(t, 60, limits.TimeoutSeconds)
}

func TestResolveStepLimits_EndToEnd(t *testing.T) {
	// Full end-to-end: workflow with step limits dispatches correct TaskConfig.
	const limitsYAML = `schema_version: "0.1"
workflow:
  id: "limits-wf"
  name: "Limits Workflow"
  trigger: "manual"
  steps:
    - id: "s1"
      agent: "test-agent"
      input: "do work"
      output_key: "result"
      max_llm_calls: 3
      max_tokens: 2048
      timeout_seconds: 45
`
	dir := t.TempDir()
	writeWorkflow(t, dir, "limits-wf", limitsYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	var receivedLimits executor.StepLimits
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		receivedLimits = req.Limits
		return &executor.ExecuteResult{TaskID: "t1", Output: "ok"}, nil
	}}

	reg := newTestRegistry(t)
	registerAgentWithLimits(t, reg, "test-agent", 10, 16384, 300)

	sched := newTestSchedulerWithRegistry(t, store, exec, reg, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "limits-run", "limits-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	waitForRunStatus(t, store, "limits-run", state.RunCompleted, 5*time.Second)

	assert.Equal(t, 3, receivedLimits.MaxLLMCalls, "step config overrides agent")
	assert.Equal(t, 2048, receivedLimits.MaxTokens, "step config overrides agent")
	assert.Equal(t, 45, receivedLimits.TimeoutSeconds, "step config overrides agent")
}
