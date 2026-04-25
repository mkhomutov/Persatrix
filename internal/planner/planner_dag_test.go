package planner

import (
	"context"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// --- ValidateDAG tests ---

func TestValidateDAG_NoCycle(t *testing.T) {
	p := newTestPlanner()
	wf, err := p.Parse(context.Background(), filepath.Join("testdata", "valid_workflow.yaml"))
	require.NoError(t, err)

	err = p.ValidateDAG(context.Background(), wf)
	assert.NoError(t, err)
}

func TestValidateDAG_SimpleCycle(t *testing.T) {
	wf := &Workflow{
		Steps: []Step{
			{ID: "aa", DependsOn: []string{"bb"}},
			{ID: "bb", DependsOn: []string{"aa"}},
		},
	}
	p := newTestPlanner()
	err := p.ValidateDAG(context.Background(), wf)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "cycle detected")
}

func TestValidateDAG_ComplexCycle(t *testing.T) {
	wf := &Workflow{
		Steps: []Step{
			{ID: "aa", DependsOn: []string{}},
			{ID: "bb", DependsOn: []string{"aa"}},
			{ID: "cc", DependsOn: []string{"bb"}},
			{ID: "dd", DependsOn: []string{"cc", "bb"}},
		},
	}
	p := newTestPlanner()
	err := p.ValidateDAG(context.Background(), wf)
	assert.NoError(t, err) // No cycle — dd depends on cc and bb, both reachable from aa.
}

func TestValidateDAG_ActualCycle3Nodes(t *testing.T) {
	wf := &Workflow{
		Steps: []Step{
			{ID: "aa", DependsOn: []string{"cc"}},
			{ID: "bb", DependsOn: []string{"aa"}},
			{ID: "cc", DependsOn: []string{"bb"}},
		},
	}
	p := newTestPlanner()
	err := p.ValidateDAG(context.Background(), wf)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "cycle detected")
}

func TestValidateDAG_SelfReference(t *testing.T) {
	wf := &Workflow{
		Steps: []Step{
			{ID: "loop", DependsOn: []string{"loop"}},
		},
	}
	p := newTestPlanner()
	err := p.ValidateDAG(context.Background(), wf)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "cycle detected")
}

func TestValidateDAG_NoDeps(t *testing.T) {
	wf := &Workflow{
		Steps: []Step{
			{ID: "aa"},
			{ID: "bb"},
			{ID: "cc"},
		},
	}
	p := newTestPlanner()
	err := p.ValidateDAG(context.Background(), wf)
	assert.NoError(t, err)
}

// --- Fixture-based ValidateDAG tests (PR 3a F-02) ---

func TestValidateDAG_FixtureCycleSimple(t *testing.T) {
	p := newTestPlanner()
	wf, err := p.Parse(context.Background(), filepath.Join("testdata", "cycle_simple.yaml"))
	require.NoError(t, err)

	err = p.ValidateDAG(context.Background(), wf)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "cycle detected")
}

func TestValidateDAG_FixtureCycleComplex(t *testing.T) {
	p := newTestPlanner()
	wf, err := p.Parse(context.Background(), filepath.Join("testdata", "cycle_complex.yaml"))
	require.NoError(t, err)

	err = p.ValidateDAG(context.Background(), wf)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "cycle detected")
}

func TestValidateDAG_FixtureSelfReference(t *testing.T) {
	p := newTestPlanner()
	// self_reference.yaml has depends_on: ["loop"] on step "loop",
	// which is now caught by validate() as self-dependency.
	_, err := p.Parse(context.Background(), filepath.Join("testdata", "self_reference.yaml"))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "depends_on references itself")
}
