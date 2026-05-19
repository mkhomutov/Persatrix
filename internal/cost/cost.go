// Package cost implements token counting, price calculation, and budget enforcement.
package cost

import (
	"fmt"
	"sync"
	"time"

	"go.uber.org/zap"
)

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

	// provisional holds worst-case charges recorded at lease-acquire time
	// (RFC 0023 — LLM Call Leasing), keyed by lease ID. An entry lives only
	// between RecordProvisional and the matching Reconcile; Reconcile
	// removes it. The scope totals above already include each provisional
	// charge — this map exists so Reconcile can compute the estimate→actual
	// delta and so a lease can be reconciled exactly once.
	provisional map[string]*provisionalCharge

	config *CostConfig
	logger *zap.Logger
}

// usageTotals holds cumulative token counts and estimated cost for a scope.
type usageTotals struct {
	InputTokens  int64
	OutputTokens int64
	EstimatedUSD float64
}

// provisionalCharge records the worst-case charge held against an
// in-flight lease. It captures the scope keys and the amounts added so
// Reconcile can apply the estimate→actual delta to the same three scopes.
type provisionalCharge struct {
	workflowID, agentID, model string
	inputTokens, outputTokens  int64
	estimatedUSD               float64
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
		provisional: make(map[string]*provisionalCharge),
		config:      config,
		logger:      logger,
	}
}

// addToScopesLocked applies a token/cost delta to all three scopes. The
// delta may be negative (Reconcile of an under-estimate, or a Release).
// Callers must hold tc.mu. Map entries are created on first touch so a
// provisional charge for a not-yet-seen workflow/agent still lands.
func (tc *TokenCounter) addToScopesLocked(workflowID, agentID string, dInput, dOutput int64, dUSD float64) {
	wf, ok := tc.perWorkflow[workflowID]
	if !ok {
		wf = &usageTotals{}
		tc.perWorkflow[workflowID] = wf
	}
	wf.InputTokens += dInput
	wf.OutputTokens += dOutput
	wf.EstimatedUSD += dUSD

	ag, ok := tc.perAgent[agentID]
	if !ok {
		ag = &usageTotals{}
		tc.perAgent[agentID] = ag
	}
	ag.InputTokens += dInput
	ag.OutputTokens += dOutput
	ag.EstimatedUSD += dUSD

	tc.global.InputTokens += dInput
	tc.global.OutputTokens += dOutput
	tc.global.EstimatedUSD += dUSD
}

// RecordUsage adds a token usage event to all three scopes and computes estimated cost.
func (tc *TokenCounter) RecordUsage(record UsageRecord) {
	cost := tc.config.EstimateCost(record.Model, record.InputTokens, record.OutputTokens)

	tc.mu.Lock()
	defer tc.mu.Unlock()

	tc.addToScopesLocked(record.WorkflowID, record.AgentID,
		record.InputTokens, record.OutputTokens, cost)

	tc.logger.Debug("token usage recorded",
		zap.String("workflow_id", record.WorkflowID),
		zap.String("agent_id", record.AgentID),
		zap.String("model", record.Model),
		zap.Int64("inputTokens", record.InputTokens),
		zap.Int64("outputTokens", record.OutputTokens),
		zap.Float64("estimatedUSD", cost),
	)
}

// RecordProvisional adds a worst-case provisional charge to all three
// scopes and records it under leaseID so a later Reconcile can replace it
// with the actual usage (RFC 0023 — LLM Call Leasing). The token counts in
// record are the agent's pre-call estimates; the charge is computed with
// the same EstimateCost formula RecordUsage uses.
//
// The WalletService guards against a colliding leaseID before calling this
// (it rejects a generated lease ID already present in its in-flight map),
// so an existing entry is overwritten defensively rather than treated as a
// hard error here.
func (tc *TokenCounter) RecordProvisional(leaseID string, record UsageRecord) {
	cost := tc.config.EstimateCost(record.Model, record.InputTokens, record.OutputTokens)

	tc.mu.Lock()
	defer tc.mu.Unlock()

	tc.addToScopesLocked(record.WorkflowID, record.AgentID,
		record.InputTokens, record.OutputTokens, cost)
	tc.provisional[leaseID] = &provisionalCharge{
		workflowID:   record.WorkflowID,
		agentID:      record.AgentID,
		model:        record.Model,
		inputTokens:  record.InputTokens,
		outputTokens: record.OutputTokens,
		estimatedUSD: cost,
	}

	tc.logger.Debug("provisional charge recorded",
		zap.String("lease_id", leaseID),
		zap.String("workflow_id", record.WorkflowID),
		zap.String("agent_id", record.AgentID),
		zap.String("model", record.Model),
		zap.Int64("estimatedInputTokens", record.InputTokens),
		zap.Int64("estimatedOutputTokens", record.OutputTokens),
		zap.Float64("estimatedUSD", cost),
	)
}

// Reconcile replaces the provisional charge held under leaseID with the
// actual usage the provider reported. The estimate→actual delta (positive
// or negative) is applied atomically to all three scopes, and the
// provisional entry is removed so the lease cannot be reconciled twice.
//
// A SettleLease reconciles with the provider-reported actuals; a
// ReleaseLease reconciles with (0, 0), fully reversing the provisional.
// Reconcile returns an error when leaseID has no outstanding provisional
// charge — an unknown lease, or one already settled / released / reaped.
func (tc *TokenCounter) Reconcile(leaseID string, actualInputTokens, actualOutputTokens int64) error {
	tc.mu.Lock()
	defer tc.mu.Unlock()

	pc, ok := tc.provisional[leaseID]
	if !ok {
		return fmt.Errorf("reconcile: no outstanding provisional charge for lease %q", leaseID)
	}

	actualUSD := tc.config.EstimateCost(pc.model, actualInputTokens, actualOutputTokens)
	tc.addToScopesLocked(pc.workflowID, pc.agentID,
		actualInputTokens-pc.inputTokens,
		actualOutputTokens-pc.outputTokens,
		actualUSD-pc.estimatedUSD,
	)
	delete(tc.provisional, leaseID)

	tc.logger.Debug("provisional charge reconciled",
		zap.String("lease_id", leaseID),
		zap.String("workflow_id", pc.workflowID),
		zap.String("agent_id", pc.agentID),
		zap.String("model", pc.model),
		zap.Int64("actualInputTokens", actualInputTokens),
		zap.Int64("actualOutputTokens", actualOutputTokens),
		zap.Float64("estimatedUSD", pc.estimatedUSD),
		zap.Float64("actualUSD", actualUSD),
	)
	return nil
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

// AgentUsages returns a snapshot of per-agent usage data.
// The returned slice is a copy — callers may read it without holding any lock.
// This method exists to decouple CostReporter from TokenCounter internals
// (review finding: avoid direct access to unexported fields across components).
func (tc *TokenCounter) AgentUsages() []AgentCostEntry {
	tc.mu.Lock()
	defer tc.mu.Unlock()
	entries := make([]AgentCostEntry, 0, len(tc.perAgent))
	for agentID, totals := range tc.perAgent {
		entries = append(entries, AgentCostEntry{
			AgentID:      agentID,
			InputTokens:  totals.InputTokens,
			OutputTokens: totals.OutputTokens,
			EstimatedUSD: totals.EstimatedUSD,
		})
	}
	return entries
}

// Config returns the cost configuration used by this counter.
func (tc *TokenCounter) Config() *CostConfig {
	return tc.config
}

// usageSnapshot reads global, per-workflow, and per-agent estimated USD totals
// under a single lock acquisition. This ensures the three scope reads are atomic
// with respect to concurrent RecordUsage calls, eliminating torn reads where
// (e.g.) the global total reflects a write that the per-workflow total does not.
func (tc *TokenCounter) usageSnapshot(workflowID, agentID string) (globalUSD, workflowUSD, agentUSD float64) {
	tc.mu.Lock()
	defer tc.mu.Unlock()
	globalUSD = tc.global.EstimatedUSD
	if wf, ok := tc.perWorkflow[workflowID]; ok {
		workflowUSD = wf.EstimatedUSD
	}
	if ag, ok := tc.perAgent[agentID]; ok {
		agentUSD = ag.EstimatedUSD
	}
	return
}

// ResetDaily clears all counters. Intended to be called at midnight for daily budget resets.
//
// Outstanding provisional charges are dropped along with the scope totals:
// a Reconcile for a lease that was in flight across the reset then surfaces
// as an unknown lease rather than applying an estimate→actual delta against
// a total that no longer holds the provisional. The WalletService treats
// that reconcile miss as a benign no-op settlement.
func (tc *TokenCounter) ResetDaily() {
	tc.mu.Lock()
	defer tc.mu.Unlock()
	tc.perWorkflow = make(map[string]*usageTotals)
	tc.perAgent = make(map[string]*usageTotals)
	tc.global = &usageTotals{}
	tc.provisional = make(map[string]*provisionalCharge)
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

// BudgetError is a structured error returned when a budget check rejects a dispatch.
// It carries the scope that triggered the rejection along with the numeric details,
// enabling structured 429 responses and programmatic budget-exceeded handling.
type BudgetError struct {
	Scope     string  // "global", "per_workflow", or "per_agent"
	Spent     float64 // amount already spent in this scope
	Limit     float64 // configured budget limit for this scope
	Estimated float64 // estimated cost of the rejected dispatch
}

func (e *BudgetError) Error() string {
	return fmt.Sprintf("%s budget exceeded: spent=%.6f, limit=%.6f, estimated=%.6f",
		e.Scope, e.Spent, e.Limit, e.Estimated)
}

// BudgetCheckResult contains the decision and a structured error when rejected.
type BudgetCheckResult struct {
	Decision BudgetDecision
	Error    *BudgetError
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
//
// All three scope totals (global, per-workflow, per-agent) are read in a single
// atomic snapshot to prevent torn reads from concurrent RecordUsage calls.
//
// RFC 0023 — LLM Call Leasing: CheckBudget is composed by the orchestrator's
// WalletService, which holds its own mutex across CheckBudget +
// RecordProvisional so the read-then-write is atomic. The scheduler's
// pre-dispatch CheckBudget call is retained only as an early-fail
// optimisation; the per-call lease is the enforcement point. CheckBudget
// itself is read-only and unchanged.
func (be *BudgetEnforcer) CheckBudget(workflowID, agentID, model string, estimatedMaxTokens int64) BudgetCheckResult {
	// Estimate the worst-case cost of this dispatch using output tokens as proxy.
	// Input tokens for the estimate are unknown pre-dispatch; use output only.
	estimatedCost := be.config.EstimateCost(model, 0, estimatedMaxTokens)

	// Atomic snapshot: read all three scope totals under one lock.
	globalSpent, wfSpent, agSpent := be.counter.usageSnapshot(workflowID, agentID)

	// Check global daily budget.
	globalLimit := be.config.Budgets.Global.MaxDailyUSD
	if globalLimit > 0 && globalSpent+estimatedCost > globalLimit {
		budgetErr := &BudgetError{
			Scope:     "global",
			Spent:     globalSpent,
			Limit:     globalLimit,
			Estimated: estimatedCost,
		}
		be.logger.Warn("budget check rejected",
			zap.String("scope", "global"),
			zap.String("workflow_id", workflowID),
			zap.String("agent_id", agentID),
			zap.Float64("spent", globalSpent),
			zap.Float64("estimatedCost", estimatedCost),
			zap.Float64("limit", globalLimit),
		)

		// Log warning for pause_and_alert degradation.
		if be.config.Budgets.Global.OnExceed == "pause_and_alert" {
			be.logger.Warn("pause_and_alert not implemented, rejecting dispatch",
				zap.String("workflow_id", workflowID),
				zap.String("agent_id", agentID),
			)
		}

		return BudgetCheckResult{Decision: BudgetReject, Error: budgetErr}
	}

	// Check per-workflow budget.
	wfLimit := be.config.Budgets.PerWorkflow.DefaultMaxUSD
	if wfLimit > 0 && wfSpent+estimatedCost > wfLimit {
		budgetErr := &BudgetError{
			Scope:     "per_workflow",
			Spent:     wfSpent,
			Limit:     wfLimit,
			Estimated: estimatedCost,
		}
		be.logger.Warn("budget check rejected",
			zap.String("scope", "per_workflow"),
			zap.String("workflow_id", workflowID),
			zap.Float64("spent", wfSpent),
			zap.Float64("estimatedCost", estimatedCost),
			zap.Float64("limit", wfLimit),
		)
		return BudgetCheckResult{Decision: BudgetReject, Error: budgetErr}
	}

	// Check per-agent budget.
	agLimit := be.config.Budgets.PerAgent.DefaultMaxUSD
	if agLimit > 0 && agSpent+estimatedCost > agLimit {
		budgetErr := &BudgetError{
			Scope:     "per_agent",
			Spent:     agSpent,
			Limit:     agLimit,
			Estimated: estimatedCost,
		}
		be.logger.Warn("budget check rejected",
			zap.String("scope", "per_agent"),
			zap.String("agent_id", agentID),
			zap.Float64("spent", agSpent),
			zap.Float64("estimatedCost", estimatedCost),
			zap.Float64("limit", agLimit),
		)
		return BudgetCheckResult{Decision: BudgetReject, Error: budgetErr}
	}

	return BudgetCheckResult{Decision: BudgetAllow}
}
