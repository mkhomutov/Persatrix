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
// Uses non-capturing (?:...) to avoid creating an extra capture group in templateRegex.
const stepIDPattern = `[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?`

// maxYAMLSize is the maximum allowed YAML file size (1 MB).
const maxYAMLSize = 1 << 20

// ResourceIDRegex validates external-facing resource identifiers (workflow IDs, agent IDs).
// Requires minimum 2 characters, lowercase alphanumeric with hyphens, no leading/trailing hyphens.`n// Pattern: ^[a-z0-9][a-z0-9-]*[a-z0-9]$ -- matches the spec in copilot-instructions.md.
// Exported for reuse by the server package (review finding F-04: eliminates regex
// duplication across security-relevant validation boundaries).
// Renamed from WorkflowIDRegex (PR #16 F-04) since it validates both workflow and agent IDs.
//
// agentIDRegex is kept as a separate compiled pattern for clearer error messages in
// planner-internal validation, even though the pattern is identical.
//
// stepIDRegex intentionally allows underscores and single-character IDs (e.g. "a",
// "step_1") because step IDs are workflow-internal identifiers, not externally
// visible names.
var (
	ResourceIDRegex = regexp.MustCompile(`^[a-z0-9][a-z0-9-]*[a-z0-9]$`)
	// PR #18 F-02: share the compiled regex instance since the pattern is
	// identical. Separate variable names are retained for clearer error
	// messages in validation call sites; separate compilation is unnecessary
	// and risks future divergence if one pattern is updated but not the other.
	agentIDRegex   = ResourceIDRegex
	stepIDRegex    = regexp.MustCompile(`^` + stepIDPattern + `$`)
	outputKeyRegex = regexp.MustCompile(`^` + stepIDPattern + `$`)
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

	// Execution limit fields (RFC 0006). Zero means "inherit from agent
	// config or system defaults". Negative values are rejected during Parse.
	TimeoutSeconds int `yaml:"timeout_seconds"`
	MaxLLMCalls    int `yaml:"max_llm_calls"`
	MaxTokens      int `yaml:"max_tokens"`

	// ContextBudget is a step-level context window budget in tokens (RFC 0008).
	// Added here alongside RFC 0006 fields to avoid a separate schema migration.
	// No enforcement logic in this PR — that is RFC 0008's scope.
	ContextBudget int `yaml:"context_budget"`

	// Cacheable marks this step as eligible for response caching (RFC 0006 PR 4b).
	// When true, the executor checks the response cache before dispatch and stores
	// results on cache miss. Persona and autonomous tasks should not be cached.
	Cacheable bool `yaml:"cacheable"`
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
	if !ResourceIDRegex.MatchString(w.ID) {
		return fmt.Errorf("invalid workflow id %q: must match %s", w.ID, ResourceIDRegex.String())
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

		// Reject negative execution limits (RFC 0006). Zero means "inherit".
		if step.TimeoutSeconds < 0 {
			return fmt.Errorf("step %q: timeout_seconds must not be negative", step.ID)
		}
		if step.MaxLLMCalls < 0 {
			return fmt.Errorf("step %q: max_llm_calls must not be negative", step.ID)
		}
		if step.MaxTokens < 0 {
			return fmt.Errorf("step %q: max_tokens must not be negative", step.ID)
		}
		if step.ContextBudget < 0 {
			return fmt.Errorf("step %q: context_budget must not be negative", step.ID)
		}
	}

	// Validate depends_on references point to existing step IDs and are not self-referential.
	for _, step := range w.Steps {
		for _, dep := range step.DependsOn {
			if dep == step.ID {
				return fmt.Errorf("step %q: depends_on references itself", step.ID)
			}
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

// templateRegex matches {{ steps.<id>.output }} and {{ variable }} patterns.
// Group 2 captures the step ID for output references; group 3 captures plain variable names.
// The step ID sub-pattern reuses stepIDPattern (with non-capturing group) to stay
// consistent with Parse validation without introducing extra capture groups.
var templateRegex = regexp.MustCompile(
	`\{\{\s*(steps\.(` + stepIDPattern + `)\.output|([a-z_][a-z0-9_]*))\s*\}\}`,
)

// suspiciousRegex matches anything that looks like {{ ... }} but was not matched
// by templateRegex. Used to emit warnings for potential typos in workflow YAML.
var suspiciousRegex = regexp.MustCompile(`\{\{.*?\}\}`)

// ResolveInputs substitutes template variables in step.Input with actual values.
// It resolves {{ steps.<id>.output }} from the outputs map and {{ variable }}
// from the vars map. Returns the resolved input string or an error if any
// referenced step ID or variable is missing. Resolution is single-pass —
// substituted values are not re-scanned for template patterns.
func ResolveInputs(step Step, outputs map[string]string, vars map[string]string, logger *zap.Logger) (string, error) {
	if logger == nil {
		logger = zap.NewNop()
	}

	input := step.Input

	// Find all template matches and their positions for single-pass replacement.
	matches := templateRegex.FindAllStringSubmatchIndex(input, -1)
	if len(matches) == 0 {
		// No templates found — check for suspicious patterns before returning.
		warnSuspicious(input, logger)
		return input, nil
	}

	var b strings.Builder
	b.Grow(len(input))
	lastEnd := 0

	for _, loc := range matches {
		// loc indices: [0]=full match start, [1]=full match end,
		// [2]=group1 start, [3]=group1 end (full inner match),
		// [4]=group2 start, [5]=group2 end (step ID — may be -1),
		// [6]=group3 start, [7]=group3 end (variable name — may be -1).

		// Write literal text before this match.
		b.WriteString(input[lastEnd:loc[0]])

		if loc[4] >= 0 {
			// steps.<id>.output — group 2 is the step ID.
			stepID := input[loc[4]:loc[5]]
			val, ok := outputs[stepID]
			if !ok {
				return "", fmt.Errorf("unresolved step output reference: steps.%s.output (step %q not in outputs map)", stepID, stepID)
			}
			b.WriteString(val)
		} else if loc[6] >= 0 {
			// {{ variable }} — group 3 is the variable name.
			varName := input[loc[6]:loc[7]]
			val, ok := vars[varName]
			if !ok {
				return "", fmt.Errorf("unresolved variable reference: %s in step %q (not in vars map)", varName, step.ID)
			}
			b.WriteString(val)
		}

		lastEnd = loc[1]
	}

	// Write trailing literal text.
	b.WriteString(input[lastEnd:])

	result := b.String()

	// Warn about suspicious patterns that were not matched.
	warnSuspicious(result, logger)

	return result, nil
}

// warnSuspicious emits logger.Warn for {{ ... }} patterns in text that were not
// matched by templateRegex. It scans the final result string for any remaining
// {{ ... }} occurrences since substituted values are not re-scanned (single-pass
// guarantee means any {{ }} in the result is either from unmatched original
// patterns or from substituted output values).
func warnSuspicious(text string, logger *zap.Logger) {
	for _, loc := range suspiciousRegex.FindAllStringIndex(text, -1) {
		logger.Warn("suspicious unresolved template pattern in step input",
			zap.String("pattern", text[loc[0]:loc[1]]),
		)
	}
}

// TODO: Implement condition evaluation
