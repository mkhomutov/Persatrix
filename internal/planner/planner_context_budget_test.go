package planner

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// --- Workflow-level context_budget_total tests (RFC 0008 PR 1) ---

func TestParse_WorkflowContextBudgetTotal_Present(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Budget test"
  context_budget_total: 6000
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
`
	p := newTestPlanner()
	wf, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.NoError(t, err)
	assert.Equal(t, 6000, wf.ContextBudgetTotal)
}

func TestParse_WorkflowContextBudgetTotal_Absent(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "No budget"
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
`
	p := newTestPlanner()
	wf, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.NoError(t, err)
	// Absence yields zero (packaging disabled — legacy passthrough).
	assert.Equal(t, 0, wf.ContextBudgetTotal)
}

func TestParse_WorkflowContextBudgetTotal_Negative(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Neg budget"
  context_budget_total: -1
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "context_budget_total must not be negative")
}

func TestParse_WorkflowContextBudgetTotal_OverrideSumExceeds(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Override too big"
  context_budget_total: 1000
  steps:
    - id: "s1"
      agent: "planner"
      input: "a"
      context_budget: 800
    - id: "s2"
      agent: "planner"
      input: "b"
      context_budget: 800
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "sum of per-step context_budget overrides")
}

func TestParse_WorkflowContextBudgetTotal_OverrideSumEqualsTotal(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Equal sum"
  context_budget_total: 1000
  steps:
    - id: "s1"
      agent: "planner"
      input: "a"
      context_budget: 600
    - id: "s2"
      agent: "planner"
      input: "b"
      context_budget: 400
`
	p := newTestPlanner()
	wf, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.NoError(t, err, "override sum equal to total is valid (zero remainder)")
	assert.Equal(t, 1000, wf.ContextBudgetTotal)
}
