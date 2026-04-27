package scheduler

import (
	"context"
	"encoding/json"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/executor/packaging"
	"github.com/mkhomutov/persatrix/internal/state"
)

// --- RFC 0008 PR 1: end-to-end context-package attachment ---

// TestContextPackage_EndToEnd verifies that when a workflow declares a
// context_budget_total, the scheduler builds a packaging.Package and attaches
// it under the reserved key on every dispatch's context map. This is the
// integration-level contract that v0.3.0 RFC 0008 PR 2 (MemoryFacade) and
// downstream consumers will rely on.
func TestContextPackage_EndToEnd_BudgetedWorkflow(t *testing.T) {
	const yaml = `schema_version: "0.1"
workflow:
  id: "ctxpkg-wf"
  name: "Context package wiring"
  trigger: "manual"
  context_budget_total: 6000
  steps:
    - id: "s1"
      agent: "test-agent"
      input: "step one"
      output_key: "out1"
    - id: "s2"
      agent: "test-agent"
      input: "step two consuming {{ steps.out1.output }}"
      depends_on: ["s1"]
      output_key: "out2"
`
	dir := t.TempDir()
	writeWorkflow(t, dir, "ctxpkg-wf", yaml)

	store := state.NewInMemoryStore(zap.NewNop())

	var mu sync.Mutex
	type observed struct {
		stepHasPackage bool
		pkg            packaging.Package
	}
	seen := map[string]observed{}

	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		mu.Lock()
		defer mu.Unlock()
		o := observed{}
		if raw, ok := req.Context[ContextPackageKey]; ok {
			o.stepHasPackage = true
			require.NoError(t, json.Unmarshal([]byte(raw), &o.pkg))
		}
		seen[req.StepID] = o
		return &executor.ExecuteResult{TaskID: "t1", Output: "ok-" + req.StepID}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "ctxpkg-run", "ctxpkg-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	waitForRunStatus(t, store, "ctxpkg-run", state.RunCompleted, 5*time.Second)

	mu.Lock()
	defer mu.Unlock()
	require.Contains(t, seen, "s1")
	require.Contains(t, seen, "s2")
	// Both steps received a context package (even s1 with no upstream outputs —
	// the package is empty but present, signalling to the agent that the
	// orchestrator opted into packaging).
	assert.True(t, seen["s1"].stepHasPackage, "s1 must receive _context_package")
	assert.True(t, seen["s2"].stepHasPackage, "s2 must receive _context_package")

	// Wire shape contract.
	assert.Equal(t, packaging.PackageVersion, seen["s2"].pkg.Version)
	assert.Equal(t, 0, seen["s2"].pkg.BudgetMemoryTokens, "PR 1 emits 0; PR 2 wires non-zero")
}

// TestContextPackage_DisabledByDefault verifies that workflows without
// context_budget_total preserve the legacy passthrough behaviour — no
// _context_package key is injected, agents receive raw outputs verbatim.
func TestContextPackage_DisabledByDefault(t *testing.T) {
	const yaml = `schema_version: "0.1"
workflow:
  id: "legacy-wf"
  name: "Legacy passthrough"
  trigger: "manual"
  steps:
    - id: "s1"
      agent: "test-agent"
      input: "no packaging here"
`
	dir := t.TempDir()
	writeWorkflow(t, dir, "legacy-wf", yaml)

	store := state.NewInMemoryStore(zap.NewNop())

	var mu sync.Mutex
	var sawPackageKey bool
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		mu.Lock()
		defer mu.Unlock()
		_, ok := req.Context[ContextPackageKey]
		sawPackageKey = ok
		return &executor.ExecuteResult{TaskID: "t1", Output: "ok"}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "legacy-run", "legacy-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()
	// Diagnostic: surface the actual failure error if the run doesn't complete.
	// (Review M2: previously polled the wrong run ID — "ctxpkg-run" — which was
	// the prior test's run and never matched, so the diagnostic never fired.)
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		run, _ := store.GetRun(context.Background(), "legacy-run")
		if run != nil && (run.Status == state.RunFailed || run.Status == state.RunCompleted) {
			t.Logf("terminal status=%s error=%q", run.Status, run.Error)
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	waitForRunStatus(t, store, "legacy-run", state.RunCompleted, 5*time.Second)

	mu.Lock()
	defer mu.Unlock()
	assert.False(t, sawPackageKey, "legacy workflows must not receive _context_package")
}
