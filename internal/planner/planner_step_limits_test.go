package planner

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// --- Step execution limit tests (RFC 0006 PR 1a) ---

func TestParse_StepLimits_Present(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Limits test"
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
      timeout_seconds: 120
      max_llm_calls: 10
      max_tokens: 4096
      context_budget: 16000
`
	p := newTestPlanner()
	wf, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.NoError(t, err)
	require.Len(t, wf.Steps, 1)

	assert.Equal(t, 120, wf.Steps[0].TimeoutSeconds)
	assert.Equal(t, 10, wf.Steps[0].MaxLLMCalls)
	assert.Equal(t, 4096, wf.Steps[0].MaxTokens)
	assert.Equal(t, 16000, wf.Steps[0].ContextBudget)
}

func TestParse_StepLimits_Absent(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "No limits"
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
`
	p := newTestPlanner()
	wf, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.NoError(t, err)
	require.Len(t, wf.Steps, 1)

	// Zero means "inherit from agent config or system defaults".
	assert.Equal(t, 0, wf.Steps[0].TimeoutSeconds)
	assert.Equal(t, 0, wf.Steps[0].MaxLLMCalls)
	assert.Equal(t, 0, wf.Steps[0].MaxTokens)
	assert.Equal(t, 0, wf.Steps[0].ContextBudget)
}

func TestParse_StepLimits_NegativeTimeoutSeconds(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Neg timeout"
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
      timeout_seconds: -1
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "timeout_seconds must not be negative")
}

func TestParse_StepLimits_NegativeMaxLLMCalls(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Neg llm calls"
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
      max_llm_calls: -5
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "max_llm_calls must not be negative")
}

func TestParse_StepLimits_NegativeMaxTokens(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Neg tokens"
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
      max_tokens: -100
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "max_tokens must not be negative")
}

func TestParse_StepLimits_NegativeContextBudget(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Neg context"
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
      context_budget: -1000
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "context_budget must not be negative")
}

func TestParse_StepLimits_PartialLimits(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Partial limits"
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
      max_llm_calls: 3
`
	p := newTestPlanner()
	wf, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.NoError(t, err)
	require.Len(t, wf.Steps, 1)

	assert.Equal(t, 3, wf.Steps[0].MaxLLMCalls)
	assert.Equal(t, 0, wf.Steps[0].TimeoutSeconds, "absent limit should be zero")
	assert.Equal(t, 0, wf.Steps[0].MaxTokens, "absent limit should be zero")
	assert.Equal(t, 0, wf.Steps[0].ContextBudget, "absent limit should be zero")
}

func TestParse_StepLimits_MultiStepInheritance(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Multi-step inheritance"
  steps:
    - id: "with-limits"
      agent: "coder"
      input: "implement feature"
      max_llm_calls: 10
      timeout_seconds: 120
    - id: "no-limits"
      agent: "reviewer"
      input: "review code"
      depends_on: ["with-limits"]
`
	p := newTestPlanner()
	wf, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.NoError(t, err)
	require.Len(t, wf.Steps, 2)

	// Step A: explicit limits preserved.
	assert.Equal(t, 10, wf.Steps[0].MaxLLMCalls)
	assert.Equal(t, 120, wf.Steps[0].TimeoutSeconds)
	assert.Equal(t, 0, wf.Steps[0].MaxTokens, "absent limit should be zero")
	assert.Equal(t, 0, wf.Steps[0].ContextBudget, "absent limit should be zero")

	// Step B: all limits zero (inherit from agent config or system defaults).
	assert.Equal(t, 0, wf.Steps[1].MaxLLMCalls)
	assert.Equal(t, 0, wf.Steps[1].TimeoutSeconds)
	assert.Equal(t, 0, wf.Steps[1].MaxTokens)
	assert.Equal(t, 0, wf.Steps[1].ContextBudget)
}

func TestParse_StepLimits_MinimumValidValues(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Minimum boundary"
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
      timeout_seconds: 1
      max_llm_calls: 1
      max_tokens: 1
      context_budget: 1
`
	p := newTestPlanner()
	wf, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.NoError(t, err)
	require.Len(t, wf.Steps, 1)

	assert.Equal(t, 1, wf.Steps[0].TimeoutSeconds)
	assert.Equal(t, 1, wf.Steps[0].MaxLLMCalls)
	assert.Equal(t, 1, wf.Steps[0].MaxTokens)
	assert.Equal(t, 1, wf.Steps[0].ContextBudget)
}
