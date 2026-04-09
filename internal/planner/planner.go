// Package planner handles workflow YAML parsing, DAG construction,
// and topological sort for execution ordering.
package planner

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"go.uber.org/zap"
	"gopkg.in/yaml.v3"
)

// stepIDPattern is the canonical step ID format pattern, shared between
// Parse validation and ResolveInputs template capture group.
const stepIDPattern = `[a-z0-9]([a-z0-9_-]*[a-z0-9])?`

// maxYAMLSize is the maximum allowed YAML file size (1 MB).
const maxYAMLSize = 1 << 20

var (
	workflowIDRegex = regexp.MustCompile(`^[a-z0-9][a-z0-9-]*[a-z0-9]$`)
	agentIDRegex    = regexp.MustCompile(`^[a-z0-9][a-z0-9-]*[a-z0-9]$`)
	stepIDRegex     = regexp.MustCompile(`^` + stepIDPattern + `$`)
	outputKeyRegex  = regexp.MustCompile(`^` + stepIDPattern + `$`)
)

// Workflow represents a parsed workflow definition.
type Workflow struct {
	ID      string `yaml:"id"`
	Name    string `yaml:"name"`
	Trigger string `yaml:"trigger"`
	Steps   []Step `yaml:"steps"`
}

// Step represents a single step in a workflow DAG.
type Step struct {
	ID        string   `yaml:"id"`
	AgentID   string   `yaml:"agent"`
	Input     string   `yaml:"input"`
	OutputKey string   `yaml:"output_key"`
	DependsOn []string `yaml:"depends_on"`
	Condition string   `yaml:"condition"`
}

// ExecutionPlan is a topologically sorted list of execution stages.
// Steps within the same stage can run in parallel.
type ExecutionPlan struct {
	WorkflowID string
	Stages     [][]Step // each inner slice is a parallel group
}

// Planner parses workflows and produces execution plans.
type Planner interface {
	Parse(ctx context.Context, yamlPath string) (*Workflow, error)
	Plan(ctx context.Context, workflow *Workflow) (*ExecutionPlan, error)
	ValidateDAG(ctx context.Context, workflow *Workflow) error
}

// WorkflowFile is the top-level YAML structure wrapping schema_version and workflow.
type WorkflowFile struct {
	SchemaVersion string   `yaml:"schema_version"`
	Workflow      Workflow `yaml:"workflow"`
}

// YAMLPlanner implements the Planner interface by parsing YAML workflow files,
// validating the DAG, and producing topologically sorted execution plans.
type YAMLPlanner struct {
	logger *zap.Logger
}

// NewYAMLPlanner creates a new YAMLPlanner with the given logger.
func NewYAMLPlanner(logger *zap.Logger) *YAMLPlanner {
	if logger == nil {
		logger = zap.NewNop()
	}
	return &YAMLPlanner{logger: logger}
}

// Parse reads a workflow YAML file, validates its structure, and returns the Workflow.
func (p *YAMLPlanner) Parse(_ context.Context, yamlPath string) (*Workflow, error) {
	cleanPath := filepath.Clean(yamlPath)

	f, err := os.Open(cleanPath)
	if err != nil {
		return nil, fmt.Errorf("open workflow file: %w", err)
	}
	defer f.Close()

	// Read up to maxYAMLSize+1 bytes to detect overflow.
	limited := io.LimitReader(f, maxYAMLSize+1)
	data, err := io.ReadAll(limited)
	if err != nil {
		return nil, fmt.Errorf("read workflow file: %w", err)
	}
	if len(data) > maxYAMLSize {
		return nil, fmt.Errorf("workflow file exceeds maximum size of %d bytes", maxYAMLSize)
	}

	// 2-pass YAML decode: first decode to yaml.Node tree to reject anchors/aliases,
	// then decode the validated node to the target struct.
	var node yaml.Node
	if err := yaml.Unmarshal(data, &node); err != nil {
		return nil, fmt.Errorf("unmarshal YAML: %w", err)
	}

	if err := rejectAliases(&node); err != nil {
		return nil, err
	}

	var wf WorkflowFile
	if err := node.Decode(&wf); err != nil {
		return nil, fmt.Errorf("decode workflow: %w", err)
	}

	if err := p.validate(&wf); err != nil {
		return nil, err
	}

	p.logger.Debug("workflow parsed",
		zap.String("id", wf.Workflow.ID),
		zap.String("name", wf.Workflow.Name),
		zap.Int("steps", len(wf.Workflow.Steps)),
	)

	return &wf.Workflow, nil
}

// validate checks required fields, formats, and referential integrity.
func (p *YAMLPlanner) validate(wf *WorkflowFile) error {
	if wf.SchemaVersion != "0.1" {
		return fmt.Errorf("unsupported schema version: %q (expected \"0.1\")", wf.SchemaVersion)
	}

	w := &wf.Workflow

	if w.ID == "" {
		return errors.New("workflow id is required")
	}
	if !workflowIDRegex.MatchString(w.ID) {
		return fmt.Errorf("invalid workflow id %q: must match %s", w.ID, workflowIDRegex.String())
	}

	if w.Name == "" {
		return errors.New("workflow name is required")
	}

	if len(w.Steps) == 0 {
		return errors.New("workflow must have at least one step")
	}

	stepIDs := make(map[string]bool, len(w.Steps))
	outputKeys := make(map[string]string) // output_key -> step ID

	for i, step := range w.Steps {
		if step.ID == "" {
			return fmt.Errorf("step %d: id is required", i)
		}
		if !stepIDRegex.MatchString(step.ID) {
			return fmt.Errorf("step %d: invalid step id %q: must match %s", i, step.ID, stepIDRegex.String())
		}
		if stepIDs[step.ID] {
			return fmt.Errorf("step %d: duplicate step id %q", i, step.ID)
		}
		stepIDs[step.ID] = true

		if step.AgentID == "" {
			return fmt.Errorf("step %q: agent is required", step.ID)
		}
		if !agentIDRegex.MatchString(step.AgentID) {
			return fmt.Errorf("step %q: invalid agent id %q: must match %s", step.ID, step.AgentID, agentIDRegex.String())
		}

		if step.Input == "" {
			return fmt.Errorf("step %q: input is required", step.ID)
		}

		if step.OutputKey != "" {
			if !outputKeyRegex.MatchString(step.OutputKey) {
				return fmt.Errorf("step %q: invalid output_key %q: must match %s", step.ID, step.OutputKey, outputKeyRegex.String())
			}
			if existing, ok := outputKeys[step.OutputKey]; ok {
				return fmt.Errorf("step %q: duplicate output_key %q (already used by step %q)", step.ID, step.OutputKey, existing)
			}
			outputKeys[step.OutputKey] = step.ID
		}
	}

	// Validate depends_on references point to existing step IDs.
	for _, step := range w.Steps {
		for _, dep := range step.DependsOn {
			if !stepIDs[dep] {
				return fmt.Errorf("step %q: depends_on references nonexistent step %q", step.ID, dep)
			}
		}
	}

	return nil
}

// rejectAliases walks the yaml.Node tree and rejects any alias nodes.
func rejectAliases(node *yaml.Node) error {
	if node.Kind == yaml.AliasNode {
		return fmt.Errorf("YAML anchors/aliases are not allowed (found alias %q)", node.Value)
	}
	for _, child := range node.Content {
		if err := rejectAliases(child); err != nil {
			return err
		}
	}
	return nil
}

// ValidateDAG checks for cycles in the workflow's step dependency graph.
func (p *YAMLPlanner) ValidateDAG(_ context.Context, workflow *Workflow) error {
	// Build adjacency list: step ID -> list of step IDs it depends on.
	adj := make(map[string][]string, len(workflow.Steps))
	for _, step := range workflow.Steps {
		adj[step.ID] = step.DependsOn
	}

	const (
		white = 0 // unvisited
		gray  = 1 // in current DFS path
		black = 2 // fully explored
	)

	color := make(map[string]int, len(workflow.Steps))
	parent := make(map[string]string)

	var dfs func(id string) error
	dfs = func(id string) error {
		color[id] = gray
		for _, dep := range adj[id] {
			switch color[dep] {
			case gray:
				// Cycle found — reconstruct cycle path.
				cycle := []string{dep, id}
				cur := id
				for cur != dep {
					cur = parent[cur]
					cycle = append(cycle, cur)
				}
				// Reverse to get readable order.
				for i, j := 0, len(cycle)-1; i < j; i, j = i+1, j-1 {
					cycle[i], cycle[j] = cycle[j], cycle[i]
				}
				return fmt.Errorf("cycle detected: %s", strings.Join(cycle, " → "))
			case white:
				parent[dep] = id
				if err := dfs(dep); err != nil {
					return err
				}
			}
		}
		color[id] = black
		return nil
	}

	for _, step := range workflow.Steps {
		if color[step.ID] == white {
			if err := dfs(step.ID); err != nil {
				return err
			}
		}
	}

	return nil
}

// Plan produces a topologically sorted ExecutionPlan using Kahn's algorithm.
// Precondition: workflow must have passed ValidateDAG.
func (p *YAMLPlanner) Plan(_ context.Context, workflow *Workflow) (*ExecutionPlan, error) {
	stepMap := make(map[string]Step, len(workflow.Steps))
	inDegree := make(map[string]int, len(workflow.Steps))
	dependents := make(map[string][]string) // step ID -> IDs that depend on it

	for _, step := range workflow.Steps {
		stepMap[step.ID] = step
		inDegree[step.ID] = len(step.DependsOn)
		for _, dep := range step.DependsOn {
			dependents[dep] = append(dependents[dep], step.ID)
		}
	}

	// Seed with steps that have no dependencies.
	var queue []string
	for _, step := range workflow.Steps {
		if inDegree[step.ID] == 0 {
			queue = append(queue, step.ID)
		}
	}

	var stages [][]Step
	emitted := 0

	for len(queue) > 0 {
		stage := make([]Step, 0, len(queue))
		for _, id := range queue {
			stage = append(stage, stepMap[id])
		}
		stages = append(stages, stage)
		emitted += len(queue)

		var next []string
		for _, id := range queue {
			for _, dep := range dependents[id] {
				inDegree[dep]--
				if inDegree[dep] == 0 {
					next = append(next, dep)
				}
			}
		}
		queue = next
	}

	// Defensive node-count check: catches cycles that slipped past a missing ValidateDAG call.
	if emitted != len(workflow.Steps) {
		return nil, fmt.Errorf("plan produced %d steps but workflow has %d (possible cycle)", emitted, len(workflow.Steps))
	}

	p.logger.Debug("execution plan created",
		zap.String("workflowID", workflow.ID),
		zap.Int("stages", len(stages)),
		zap.Int("totalSteps", emitted),
	)

	return &ExecutionPlan{
		WorkflowID: workflow.ID,
		Stages:     stages,
	}, nil
}

// TODO: Implement template variable resolution ({{ steps.X.output }}) — PR 3b
// TODO: Implement condition evaluation
