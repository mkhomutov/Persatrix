package cost

import (
	"sort"
	"sync"
	"time"

	"go.uber.org/zap"
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
	// TODO(v0.2): Add per-key eviction or TTL to prevent unbounded growth.
	// Currently only cleared by ResetDaily(). For long-running orchestrators
	// handling many workflows, completed entries accumulate until the next reset.
	perWorkflowSteps map[string][]StepCostEntry

	// workflowCountWarned tracks whether the high-workflow-count warning has
	// been emitted this reset cycle, to avoid log spam.
	// (PR #86 review: make perWorkflowSteps growth concern more visible to operators)
	workflowCountWarned bool

	counter *TokenCounter
	config  *CostConfig
	logger  *zap.Logger
}

// NewCostReporter creates a new CostReporter backed by the given TokenCounter.
// Accepts a logger for consistency with TokenCounter and BudgetEnforcer.
// (PR #86 review S-02: adding logger now avoids a breaking constructor change later.)
//
// Panics if counter is nil — a nil counter is a programming error (would cause
// nil-pointer panics in WorkflowSummary, GlobalSummary, and ResetDaily).
// The primary caller is WithCostComponents which always provides a valid counter,
// but the constructor is exported and must be safe to call directly.
// (PR #86 review: must-fix nil-guard)
//
// config may be nil — the reporter does not currently reference config, but the
// parameter is retained for future use (e.g., per-workflow budget display in
// summaries). Nil config is safe and does not affect any current behavior.
// (PR 3a finding S-06: nil-config guard)
func NewCostReporter(counter *TokenCounter, config *CostConfig, logger *zap.Logger) *CostReporter {
	if counter == nil {
		panic("cost: NewCostReporter requires a non-nil TokenCounter")
	}
	if logger == nil {
		logger = zap.NewNop()
	}
	return &CostReporter{
		perWorkflowSteps: make(map[string][]StepCostEntry),
		counter:          counter,
		config:           config,
		logger:           logger,
	}
}

// RecordStepCost records cost data for a single workflow step.
// This is called by the scheduler after each step dispatch completes.
func (r *CostReporter) RecordStepCost(workflowID string, entry StepCostEntry) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.perWorkflowSteps[workflowID] = append(r.perWorkflowSteps[workflowID], entry)

	// Warn once per reset cycle when the number of tracked workflows exceeds
	// a threshold. This alerts operators to potential memory growth in long-running
	// deployments before the v0.2 TTL/eviction feature is implemented.
	// (PR #86 review: perWorkflowSteps unbounded growth concern)
	const workflowCountThreshold = 10000
	if !r.workflowCountWarned && len(r.perWorkflowSteps) > workflowCountThreshold {
		r.workflowCountWarned = true
		r.logger.Warn("high workflow count in cost reporter, consider increasing ResetDaily frequency",
			zap.Int("workflowCount", len(r.perWorkflowSteps)),
			zap.Int("threshold", workflowCountThreshold),
		)
	}
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

// ResetDaily clears per-workflow step cost data and resets the underlying
// TokenCounter. Calling this single method ensures both components reset in
// a single call from the caller's perspective, preventing state divergence
// from independent resets. Note: the two resets are not atomically serialized
// — a concurrent reader may briefly observe a partially-reset state.
//
// Order rationale (PR #86 review): reporter map is cleared before the counter
// so that a concurrent reader sees either (old counter + old steps) or
// (old counter + empty steps), but never (empty counter + old steps). The
// latter case would produce zero totals with non-zero steps, which is more
// confusing than the reverse (totals briefly exceeding step sums).
//
// For long-running orchestrators, this prevents unbounded memory growth in
// the perWorkflowSteps map (review finding: grows without per-key eviction).
func (r *CostReporter) ResetDaily() {
	r.mu.Lock()
	r.perWorkflowSteps = make(map[string][]StepCostEntry)
	r.workflowCountWarned = false
	r.mu.Unlock()
	r.counter.ResetDaily()
	r.logger.Info("daily cost reporter data reset")
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
