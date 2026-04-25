package planner

import (
	"context"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// --- Parse tests ---

func TestParse_ValidWorkflow(t *testing.T) {
	p := newTestPlanner()
	wf, err := p.Parse(context.Background(), filepath.Join("testdata", "valid_workflow.yaml"))
	require.NoError(t, err)

	assert.Equal(t, "feature-builder", wf.ID)
	assert.Equal(t, "Build a Feature End-to-End", wf.Name)
	assert.Equal(t, "manual", wf.Trigger)
	assert.Len(t, wf.Steps, 4)

	assert.Equal(t, "plan", wf.Steps[0].ID)
	assert.Equal(t, "planner", wf.Steps[0].AgentID)
	assert.Equal(t, "{{ user_request }}", wf.Steps[0].Input)
	assert.Equal(t, "plan", wf.Steps[0].OutputKey)

	assert.Equal(t, "implement", wf.Steps[1].ID)
	assert.Equal(t, []string{"plan"}, wf.Steps[1].DependsOn)

	assert.Equal(t, "revise", wf.Steps[3].ID)
	assert.Equal(t, []string{"review"}, wf.Steps[3].DependsOn)
	assert.Equal(t, "{{ steps.review.output.approved == false }}", wf.Steps[3].Condition)
}

func TestParse_SingleStep(t *testing.T) {
	p := newTestPlanner()
	wf, err := p.Parse(context.Background(), filepath.Join("testdata", "single_step.yaml"))
	require.NoError(t, err)

	assert.Equal(t, "single-step", wf.ID)
	assert.Len(t, wf.Steps, 1)
	assert.Equal(t, "do", wf.Steps[0].ID)
}

func TestParse_MissingWorkflowID(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  name: "No ID"
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "workflow id is required")
}

func TestParse_MissingWorkflowName(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "workflow name is required")
}

func TestParse_EmptySteps(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Empty steps"
  steps: []
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "at least one step")
}

func TestParse_MissingStepID(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Missing step ID"
  steps:
    - agent: "planner"
      input: "hello"
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "id is required")
}

func TestParse_MissingStepAgent(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Missing agent"
  steps:
    - id: "s1"
      input: "hello"
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "agent is required")
}

func TestParse_MissingStepInput(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Missing input"
  steps:
    - id: "s1"
      agent: "planner"
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "input is required")
}

func TestParse_InvalidWorkflowID(t *testing.T) {
	tests := []struct {
		name string
		id   string
	}{
		{"uppercase", "Test-WF"},
		{"starts with hyphen", "-test"},
		{"ends with hyphen", "test-"},
		{"spaces", "test wf"},
		{"single char", "a"},
		{"dots", "test.wf"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			yaml := `
schema_version: "0.1"
workflow:
  id: "` + tc.id + `"
  name: "Bad ID"
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
`
			p := newTestPlanner()
			_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
			require.Error(t, err)
			assert.Contains(t, err.Error(), "invalid workflow id")
		})
	}
}

func TestParse_InvalidAgentID(t *testing.T) {
	tests := []struct {
		name    string
		agentID string
	}{
		{"uppercase", "Planner"},
		{"starts with hyphen", "-planner"},
		{"single char", "p"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Bad agent"
  steps:
    - id: "s1"
      agent: "` + tc.agentID + `"
      input: "hello"
`
			p := newTestPlanner()
			_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
			require.Error(t, err)
			assert.Contains(t, err.Error(), "invalid agent id")
		})
	}
}

func TestParse_InvalidStepID(t *testing.T) {
	tests := []struct {
		name   string
		stepID string
	}{
		{"uppercase", "Step1"},
		{"dots", "step.1"},
		{"braces", "step{1}"},
		{"slash", "step/1"},
		{"starts with hyphen", "-step"},
		{"ends with hyphen", "step-"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Bad step"
  steps:
    - id: "` + tc.stepID + `"
      agent: "planner"
      input: "hello"
`
			p := newTestPlanner()
			_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
			require.Error(t, err)
			assert.Contains(t, err.Error(), "invalid step id")
		})
	}
}

func TestParse_DuplicateStepID(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Dup steps"
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
    - id: "s1"
      agent: "planner"
      input: "world"
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "duplicate step id")
}

func TestParse_UnknownSchemaVersion(t *testing.T) {
	tests := []struct {
		name    string
		version string
	}{
		{"0.2", "0.2"},
		{"empty", ""},
		{"1.0", "1.0"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			yaml := `
schema_version: "` + tc.version + `"
workflow:
  id: "test-wf"
  name: "Bad version"
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
`
			p := newTestPlanner()
			_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
			require.Error(t, err)
			assert.Contains(t, err.Error(), "unsupported schema version")
		})
	}
}

func TestParse_InvalidOutputKey(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Bad output key"
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
      output_key: "BAD KEY!"
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "invalid output_key")
}

func TestParse_DuplicateOutputKey(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Dup output key"
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
      output_key: "result"
    - id: "s2"
      agent: "planner"
      input: "world"
      output_key: "result"
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "duplicate output_key")
}

func TestParse_InvalidDependsOnRef(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Bad dep"
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
      depends_on: ["nonexistent"]
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "depends_on references nonexistent step")
}

func TestParse_FileNotFound(t *testing.T) {
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), "testdata/nonexistent.yaml")
	require.Error(t, err)
	assert.Contains(t, err.Error(), "open workflow file")
}

func TestParse_FileTooLarge(t *testing.T) {
	// Create a file larger than 1 MB.
	content := strings.Repeat("x", maxYAMLSize+100)
	path := writeTempYAML(t, content)

	p := newTestPlanner()
	_, err := p.Parse(context.Background(), path)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "exceeds maximum size")
}

func TestParse_AnchorAliasRejected(t *testing.T) {
	yaml := `
schema_version: "0.1"
defaults: &defaults
  agent: "planner"
  input: "hello"
workflow:
  id: "test-wf"
  name: "Alias test"
  steps:
    - id: "s1"
      <<: *defaults
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "anchors/aliases are not allowed")
}

func TestParse_PathCleaning(t *testing.T) {
	// filepath.Clean normalizes the path — Parse should handle it.
	p := newTestPlanner()
	wf, err := p.Parse(context.Background(), filepath.Join("testdata", "..", "testdata", "single_step.yaml"))
	require.NoError(t, err)
	assert.Equal(t, "single-step", wf.ID)
}

func TestParse_NilLogger(t *testing.T) {
	// NewYAMLPlanner with nil logger should not panic.
	p := NewYAMLPlanner(nil)
	wf, err := p.Parse(context.Background(), filepath.Join("testdata", "single_step.yaml"))
	require.NoError(t, err)
	assert.Equal(t, "single-step", wf.ID)
}

// --- Parse: self-dependency check (PR 3a F-05) ---

func TestParse_SelfDependency(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Self dep"
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
      depends_on: ["s1"]
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "depends_on references itself")
}

// --- Parse: malformed YAML syntax (PR 3a F-08) ---

func TestParse_MalformedYAML(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Bad YAML
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "unmarshal YAML")
}
