package planner

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// Compile-time check: YAMLPlanner implements Planner.
var _ Planner = (*YAMLPlanner)(nil)

func newTestPlanner() *YAMLPlanner {
	return NewYAMLPlanner(zap.NewNop())
}

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

// --- Regex and format tests ---

func TestStepIDRegex_ValidIDs(t *testing.T) {
	valid := []string{"a", "s1", "plan", "code-review", "step_1", "a1b2c3"}
	for _, id := range valid {
		assert.True(t, stepIDRegex.MatchString(id), "expected valid: %q", id)
	}
}

func TestStepIDRegex_InvalidIDs(t *testing.T) {
	invalid := []string{"", "-start", "end-", "Step1", "has space", "has.dot", "a{b}", "a/b"}
	for _, id := range invalid {
		assert.False(t, stepIDRegex.MatchString(id), "expected invalid: %q", id)
	}
}

func TestWorkflowIDRegex_ValidIDs(t *testing.T) {
	valid := []string{"ab", "feature-builder", "v01", "a1b2"}
	for _, id := range valid {
		assert.True(t, workflowIDRegex.MatchString(id), "expected valid: %q", id)
	}
}

func TestWorkflowIDRegex_InvalidIDs(t *testing.T) {
	invalid := []string{"", "a", "-start", "end-", "A-B", "has space"}
	for _, id := range invalid {
		assert.False(t, workflowIDRegex.MatchString(id), "expected invalid: %q", id)
	}
}

// --- Helper ---

func writeTempYAML(t *testing.T, content string) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "test.yaml")
	err := os.WriteFile(path, []byte(content), 0o644)
	require.NoError(t, err)
	return path
}
