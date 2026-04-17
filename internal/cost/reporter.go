package cost

import (
	"sync"
	"time"
)

// StepCostEntry captures cost data for a single workflow step.
type StepCostEntry struct {
	StepID       string  `json:"step_id"`
	AgentID      string  `json:"agent_id"`
	Model        string  `json:"model"`
	InputTokens  int64   `json:"input_tokens"`
	OutputTokens int64   `json:"output_tokens"`
	EstimatedUSD float64 `json:"estimated_usd"`
}

// WorkflowCostSummary aggregates cost data for a single workflow run.
type WorkflowCostSummary struct {
	WorkflowID     string          `json:"workflow_id"`
	TotalInput     int64           `json:"total_input_tokens"`
	TotalOutput    int64           `json:"total_output_tokens"`
	TotalEstimated float64         `json:"total_estimated_usd"`
	Steps          []StepCostEntry `json:"steps"`
}

// AgentCostEntry captures cost data for a single agent in the global summary.
type AgentCostEntry struct {
	AgentID      string  `json:"agent_id"`
	InputTokens  int64   `json:"input_tokens"`
	OutputTokens int64   `json:"output_tokens"`
	EstimatedUSD float64 `json:"estimated_usd"`
}

// GlobalCostSummary aggregates cost data across all workflows and agents.
type GlobalCostSummary struct {
	DailyInputTokens  int64            `json:"daily_input_tokens"`
	DailyOutputTokens int64            `json:"daily_output_tokens"`
	DailyEstimatedUSD float64          `json:"daily_estimated_usd"`
	TopAgents         []AgentCostEntry `json:"top_agents"`
	ReportedAt        time.Time        `json:"reported_at"`
}

// CostReporter aggregates cost metadata from the TokenCounter and provides
// structured summaries for API responses. It also maintains per-workflow
// step-level cost breakdowns that the TokenCounter does not track.
type CostReporter struct {
	mu sync.Mutex

	// perWorkflowSteps tracks step-level cost entries per workflow ID.
	perWorkflowSteps map[string][]StepCostEntry

	counter *TokenCounter
	config  *CostConfig
}

// NewCostReporter creates a new CostReporter backed by the given TokenCounter.
func NewCostReporter(counter *TokenCounter, config *CostConfig) *CostReporter {
	return &CostReporter{
		perWorkflowSteps: make(map[string][]StepCostEntry),
		counter:          counter,
		config:           config,
	}
}

// RecordStepCost records cost data for a single workflow step.
// This is called by the scheduler after each step dispatch completes.
func (r *CostReporter) RecordStepCost(workflowID string, entry StepCostEntry) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.perWorkflowSteps[workflowID] = append(r.perWorkflowSteps[workflowID], entry)
}

// WorkflowSummary returns a cost summary for the given workflow, including
// per-step breakdown and totals from the TokenCounter.
func (r *CostReporter) WorkflowSummary(workflowID string) WorkflowCostSummary {
	input, output, usd := r.counter.WorkflowUsage(workflowID)

	r.mu.Lock()
	steps := make([]StepCostEntry, len(r.perWorkflowSteps[workflowID]))
	copy(steps, r.perWorkflowSteps[workflowID])
	r.mu.Unlock()

	return WorkflowCostSummary{
		WorkflowID:     workflowID,
		TotalInput:     input,
		TotalOutput:    output,
		TotalEstimated: usd,
		Steps:          steps,
	}
}

// GlobalSummary returns a cost summary across all workflows and agents,
// including the top agents by estimated spend.
func (r *CostReporter) GlobalSummary() GlobalCostSummary {
	input, output, usd := r.counter.GlobalUsage()

	// Build per-agent entries from the counter's per-agent data.
	r.counter.mu.Lock()
	agents := make([]AgentCostEntry, 0, len(r.counter.perAgent))
	for agentID, totals := range r.counter.perAgent {
		agents = append(agents, AgentCostEntry{
			AgentID:      agentID,
			InputTokens:  totals.InputTokens,
			OutputTokens: totals.OutputTokens,
			EstimatedUSD: totals.EstimatedUSD,
		})
	}
	r.counter.mu.Unlock()

	// Sort by estimated USD descending (top spenders first).
	sortAgentsBySpend(agents)

	return GlobalCostSummary{
		DailyInputTokens:  input,
		DailyOutputTokens: output,
		DailyEstimatedUSD: usd,
		TopAgents:         agents,
		ReportedAt:        time.Now(),
	}
}

// sortAgentsBySpend sorts agent entries by EstimatedUSD descending.
func sortAgentsBySpend(agents []AgentCostEntry) {
	// Simple insertion sort — expected to have < 100 agents.
	for i := 1; i < len(agents); i++ {
		for j := i; j > 0 && agents[j].EstimatedUSD > agents[j-1].EstimatedUSD; j-- {
			agents[j], agents[j-1] = agents[j-1], agents[j]
		}
	}
}
