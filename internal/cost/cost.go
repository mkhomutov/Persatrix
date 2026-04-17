// Package cost implements token counting, price calculation, and budget enforcement.
package cost

import (
	"sync"
	"time"

	"go.uber.org/zap"
)

// TODO: Implement CostReporter (attribution by agent/workflow/model)
// TODO: Implement AlertManager (threshold-based cost alerts)

// UsageRecord captures a single token usage event for recording.
type UsageRecord struct {
	WorkflowID   string
	AgentID      string
	Model        string
	InputTokens  int64
	OutputTokens int64
}

// TokenCounter tracks running token/cost totals at three scopes:
// per-workflow, per-agent, and global daily. All methods are thread-safe.
type TokenCounter struct {
	mu sync.Mutex

	// perWorkflow tracks cumulative token usage keyed by workflow ID.
	perWorkflow map[string]*usageTotals
	// perAgent tracks cumulative token usage keyed by agent ID.
	perAgent map[string]*usageTotals
	// global tracks the daily total across all workflows and agents.
	global *usageTotals

	config *CostConfig
	logger *zap.Logger
}

// usageTotals holds cumulative token counts and estimated cost for a scope.
type usageTotals struct {
	InputTokens  int64
	OutputTokens int64
	EstimatedUSD float64
}

// NewTokenCounter creates a new TokenCounter with the given configuration.
func NewTokenCounter(config *CostConfig, logger *zap.Logger) *TokenCounter {
	if logger == nil {
		logger = zap.NewNop()
	}
	return &TokenCounter{
		perWorkflow: make(map[string]*usageTotals),
		perAgent:    make(map[string]*usageTotals),
		global:      &usageTotals{},
		config:      config,
		logger:      logger,
	}
}

// RecordUsage adds a token usage event to all three scopes and computes estimated cost.
func (tc *TokenCounter) RecordUsage(record UsageRecord) {
	cost := tc.config.EstimateCost(record.Model, record.InputTokens, record.OutputTokens)

	tc.mu.Lock()
	defer tc.mu.Unlock()

	// Per-workflow.
	wf, ok := tc.perWorkflow[record.WorkflowID]
	if !ok {
		wf = &usageTotals{}
		tc.perWorkflow[record.WorkflowID] = wf
	}
	wf.InputTokens += record.InputTokens
	wf.OutputTokens += record.OutputTokens
	wf.EstimatedUSD += cost

	// Per-agent.
	ag, ok := tc.perAgent[record.AgentID]
	if !ok {
		ag = &usageTotals{}
		tc.perAgent[record.AgentID] = ag
	}
	ag.InputTokens += record.InputTokens
	ag.OutputTokens += record.OutputTokens
	ag.EstimatedUSD += cost

	// Global daily.
	tc.global.InputTokens += record.InputTokens
	tc.global.OutputTokens += record.OutputTokens
	tc.global.EstimatedUSD += cost

	tc.logger.Debug("token usage recorded",
		zap.String("workflowID", record.WorkflowID),
		zap.String("agentID", record.AgentID),
		zap.String("model", record.Model),
		zap.Int64("inputTokens", record.InputTokens),
		zap.Int64("outputTokens", record.OutputTokens),
		zap.Float64("estimatedUSD", cost),
	)
}

// WorkflowUsage returns the cumulative usage for a workflow.
// Returns zero values if the workflow has no recorded usage.
func (tc *TokenCounter) WorkflowUsage(workflowID string) (inputTokens, outputTokens int64, estimatedUSD float64) {
	tc.mu.Lock()
	defer tc.mu.Unlock()
	if wf, ok := tc.perWorkflow[workflowID]; ok {
		return wf.InputTokens, wf.OutputTokens, wf.EstimatedUSD
	}
	return 0, 0, 0
}

// AgentUsage returns the cumulative usage for an agent.
// Returns zero values if the agent has no recorded usage.
func (tc *TokenCounter) AgentUsage(agentID string) (inputTokens, outputTokens int64, estimatedUSD float64) {
	tc.mu.Lock()
	defer tc.mu.Unlock()
	if ag, ok := tc.perAgent[agentID]; ok {
		return ag.InputTokens, ag.OutputTokens, ag.EstimatedUSD
	}
	return 0, 0, 0
}

// GlobalUsage returns the cumulative daily usage across all scopes.
func (tc *TokenCounter) GlobalUsage() (inputTokens, outputTokens int64, estimatedUSD float64) {
	tc.mu.Lock()
	defer tc.mu.Unlock()
	return tc.global.InputTokens, tc.global.OutputTokens, tc.global.EstimatedUSD
}

// Config returns the cost configuration used by this counter.
func (tc *TokenCounter) Config() *CostConfig {
	return tc.config
}

// ResetDaily clears all counters. Intended to be called at midnight for daily budget resets.
func (tc *TokenCounter) ResetDaily() {
	tc.mu.Lock()
	defer tc.mu.Unlock()
	tc.perWorkflow = make(map[string]*usageTotals)
	tc.perAgent = make(map[string]*usageTotals)
	tc.global = &usageTotals{}
	tc.logger.Info("daily token counters reset", zap.Time("resetAt", time.Now()))
}

// BudgetDecision represents the outcome of a budget check.
type BudgetDecision int

const (
	// BudgetAllow indicates the dispatch is within budget.
	BudgetAllow BudgetDecision = iota
	// BudgetReject indicates the dispatch would exceed budget.
	BudgetReject
)

func (d BudgetDecision) String() string {
	switch d {
	case BudgetAllow:
		return "allow"
	case BudgetReject:
		return "reject"
	default:
		return "unknown"
	}
}

// BudgetCheckResult contains the decision and a human-readable reason when rejected.
type BudgetCheckResult struct {
	Decision BudgetDecision
	Reason   string
}

// BudgetEnforcer performs pre-dispatch budget checks against configured thresholds.
// It uses the TokenCounter's running totals plus a heuristic estimate of the
// upcoming dispatch's maximum cost to decide whether to allow or reject.
type BudgetEnforcer struct {
	counter *TokenCounter
	config  *CostConfig
	logger  *zap.Logger
}

// NewBudgetEnforcer creates a new BudgetEnforcer with the given counter and config.
func NewBudgetEnforcer(counter *TokenCounter, config *CostConfig, logger *zap.Logger) *BudgetEnforcer {
	if logger == nil {
		logger = zap.NewNop()
	}

	// Warn if on_exceed is "pause_and_alert" — not implemented, treated as "fail".
	if config.Budgets.Global.OnExceed == "pause_and_alert" {
		logger.Warn("on_exceed: pause_and_alert is not implemented, treating as fail",
			zap.String("configuredOnExceed", config.Budgets.Global.OnExceed),
		)
	}

	return &BudgetEnforcer{
		counter: counter,
		config:  config,
		logger:  logger,
	}
}

// CheckBudget evaluates whether dispatching a task for the given workflow/agent
// with the estimated maximum token usage would exceed any budget threshold.
// The estimatedMaxTokens parameter is typically the step's MaxTokens limit.
// The model parameter is used to look up per-token pricing for cost estimation.
func (be *BudgetEnforcer) CheckBudget(workflowID, agentID, model string, estimatedMaxTokens int64) BudgetCheckResult {
	// Estimate the worst-case cost of this dispatch using output tokens as proxy.
	// Input tokens for the estimate are unknown pre-dispatch; use output only.
	estimatedCost := be.config.EstimateCost(model, 0, estimatedMaxTokens)

	// Check global daily budget.
	_, _, globalSpent := be.counter.GlobalUsage()
	globalLimit := be.config.Budgets.Global.MaxDailyUSD
	if globalLimit > 0 && globalSpent+estimatedCost > globalLimit {
		reason := "global daily budget exceeded"
		be.logger.Warn("budget check rejected",
			zap.String("scope", "global"),
			zap.String("workflowID", workflowID),
			zap.String("agentID", agentID),
			zap.Float64("spent", globalSpent),
			zap.Float64("estimatedCost", estimatedCost),
			zap.Float64("limit", globalLimit),
		)

		// Log warning for pause_and_alert degradation.
		if be.config.Budgets.Global.OnExceed == "pause_and_alert" {
			be.logger.Warn("pause_and_alert not implemented, rejecting dispatch",
				zap.String("workflowID", workflowID),
				zap.String("agentID", agentID),
			)
		}

		return BudgetCheckResult{Decision: BudgetReject, Reason: reason}
	}

	// Check per-workflow budget.
	_, _, wfSpent := be.counter.WorkflowUsage(workflowID)
	wfLimit := be.config.Budgets.PerWorkflow.DefaultMaxUSD
	if wfLimit > 0 && wfSpent+estimatedCost > wfLimit {
		reason := "per-workflow budget exceeded"
		be.logger.Warn("budget check rejected",
			zap.String("scope", "per_workflow"),
			zap.String("workflowID", workflowID),
			zap.Float64("spent", wfSpent),
			zap.Float64("estimatedCost", estimatedCost),
			zap.Float64("limit", wfLimit),
		)
		return BudgetCheckResult{Decision: BudgetReject, Reason: reason}
	}

	// Check per-agent budget.
	_, _, agSpent := be.counter.AgentUsage(agentID)
	agLimit := be.config.Budgets.PerAgent.DefaultMaxUSD
	if agLimit > 0 && agSpent+estimatedCost > agLimit {
		reason := "per-agent budget exceeded"
		be.logger.Warn("budget check rejected",
			zap.String("scope", "per_agent"),
			zap.String("agentID", agentID),
			zap.Float64("spent", agSpent),
			zap.Float64("estimatedCost", estimatedCost),
			zap.Float64("limit", agLimit),
		)
		return BudgetCheckResult{Decision: BudgetReject, Reason: reason}
	}

	return BudgetCheckResult{Decision: BudgetAllow}
}
