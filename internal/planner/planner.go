// Package planner handles workflow YAML parsing, DAG construction,
// and topological sort for execution ordering.
package planner

import "context"

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

// TODO: Implement YAMLPlanner
// TODO: Implement template variable resolution ({{ steps.X.output }})
// TODO: Implement condition evaluation
