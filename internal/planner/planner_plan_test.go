package planner

import (
	"context"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// --- Plan tests ---

func TestPlan_LinearChain(t *testing.T) {
	wf := &Workflow{
		ID: "linear",
		Steps: []Step{
			{ID: "s1"},
			{ID: "s2", DependsOn: []string{"s1"}},
			{ID: "s3", DependsOn: []string{"s2"}},
		},
	}
	p := newTestPlanner()
	plan, err := p.Plan(context.Background(), wf)
	require.NoError(t, err)

	assert.Equal(t, "linear", plan.WorkflowID)
	require.Len(t, plan.Stages, 3)
	assert.Len(t, plan.Stages[0], 1)
	assert.Equal(t, "s1", plan.Stages[0][0].ID)
	assert.Equal(t, "s2", plan.Stages[1][0].ID)
	assert.Equal(t, "s3", plan.Stages[2][0].ID)
}

func TestPlan_DiamondDependency(t *testing.T) {
	p := newTestPlanner()
	wf, err := p.Parse(context.Background(), filepath.Join("testdata", "diamond_deps.yaml"))
	require.NoError(t, err)

	plan, err := p.Plan(context.Background(), wf)
	require.NoError(t, err)

	require.Len(t, plan.Stages, 3)
	assert.Len(t, plan.Stages[0], 1) // start
	assert.Len(t, plan.Stages[1], 2) // left, right (parallel)
	assert.Len(t, plan.Stages[2], 1) // merge
}

func TestPlan_FullyParallel(t *testing.T) {
	p := newTestPlanner()
	wf, err := p.Parse(context.Background(), filepath.Join("testdata", "parallel_steps.yaml"))
	require.NoError(t, err)

	plan, err := p.Plan(context.Background(), wf)
	require.NoError(t, err)

	require.Len(t, plan.Stages, 1)
	assert.Len(t, plan.Stages[0], 3) // all parallel
}

func TestPlan_SingleStep(t *testing.T) {
	wf := &Workflow{
		ID:    "single",
		Steps: []Step{{ID: "only"}},
	}
	p := newTestPlanner()
	plan, err := p.Plan(context.Background(), wf)
	require.NoError(t, err)

	require.Len(t, plan.Stages, 1)
	assert.Len(t, plan.Stages[0], 1)
	assert.Equal(t, "only", plan.Stages[0][0].ID)
}

func TestPlan_DefensiveNodeCount(t *testing.T) {
	// Give Plan a cyclic workflow without calling ValidateDAG first.
	wf := &Workflow{
		ID: "cyclic",
		Steps: []Step{
			{ID: "aa", DependsOn: []string{"bb"}},
			{ID: "bb", DependsOn: []string{"aa"}},
		},
	}
	p := newTestPlanner()
	_, err := p.Plan(context.Background(), wf)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "possible cycle")
}

// --- Pipeline integration test ---

func TestPipeline_FeatureBuilder(t *testing.T) {
	p := newTestPlanner()
	ctx := context.Background()

	wf, err := p.Parse(ctx, filepath.Join("testdata", "valid_workflow.yaml"))
	require.NoError(t, err)

	err = p.ValidateDAG(ctx, wf)
	require.NoError(t, err)

	plan, err := p.Plan(ctx, wf)
	require.NoError(t, err)

	require.Len(t, plan.Stages, 4)
	assert.Equal(t, "plan", plan.Stages[0][0].ID)
	assert.Equal(t, "implement", plan.Stages[1][0].ID)
	assert.Equal(t, "review", plan.Stages[2][0].ID)
	assert.Equal(t, "revise", plan.Stages[3][0].ID)
}
