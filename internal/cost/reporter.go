package cost

import (
	"sort"
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
//
// NOTE: The returned data may be slightly inconsistent under concurrent writes.
// WorkflowUsage (TokenCounter lock) and per-step data (CostReporter lock) are
// acquired sequentially — a concurrent RecordStepCost + RecordUsage between the
// two acquisitions can produce a TotalEstimated that does not match the sum of
// Steps[*].EstimatedUSD. Acceptable for informational display.
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
//
// NOTE: The returned data may be slightly inconsistent under concurrent writes.
// GlobalUsage() and AgentUsages() acquire the TokenCounter lock separately — a
// concurrent RecordUsage between the two calls can produce a DailyEstimatedUSD
// that does not match the sum of TopAgents[*].EstimatedUSD. Acceptable for
// informational display (same TOCTOU pattern as BudgetEnforcer.CheckBudget).
func (r *CostReporter) GlobalSummary() GlobalCostSummary {
	input, output, usd := r.counter.GlobalUsage()

	// Build per-agent entries via the public AgentUsages() snapshot method.
	// This avoids direct access to TokenCounter's unexported fields (mu, perAgent).
	agents := r.counter.AgentUsages()

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

// ResetDaily clears per-workflow step cost data. Should be called alongside
// TokenCounter.ResetDaily() to prevent unbounded memory growth in long-running
// orchestrators (review finding: perWorkflowSteps grows without cleanup).
func (r *CostReporter) ResetDaily() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.perWorkflowSteps = make(map[string][]StepCostEntry)
}

// sortAgentsBySpend sorts agent entries by EstimatedUSD descending.
// Uses stable sort with AgentID as a secondary key for deterministic ordering
// when agents have equal spend (aids debugging and snapshot testing).
func sortAgentsBySpend(agents []AgentCostEntry) {
	sort.SliceStable(agents, func(i, j int) bool {
		if agents[i].EstimatedUSD != agents[j].EstimatedUSD {
			return agents[i].EstimatedUSD > agents[j].EstimatedUSD
		}
		return agents[i].AgentID < agents[j].AgentID
	})
}
