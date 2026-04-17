// Package scheduler executes workflow plans.
package scheduler

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"strconv"
	"sync"
	"time"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/defaults"
	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// ErrBudgetExceeded is returned when a step dispatch is rejected by the budget enforcer.
// Callers can use errors.Is(err, ErrBudgetExceeded) to programmatically distinguish
// budget failures from agent execution failures without string matching.
var ErrBudgetExceeded = errors.New("budget exceeded")

// Scheduler drives workflow execution by polling for pending runs and
// dispatching tasks to agents via the Executor.
type Scheduler interface {
	Run(ctx context.Context) error
}

// WorkflowScheduler polls for pending workflow runs and drives their execution
// through stages — parsing the workflow YAML, planning stages, fanning out
// parallel steps, and collecting results.
type WorkflowScheduler struct {
	store state.Store
	// NOTE: registry is accepted for forward compatibility — the scheduler does not
	// currently resolve agents directly (the executor owns that), but v0.2 scheduler
	// health-checks and cost-aware routing will need direct registry access.
	registry      registry.Registry
	planner       planner.Planner
	executor      executor.Executor
	logger        *zap.Logger
	workflowsDir  string
	pollInterval  time.Duration
	maxConcurrent int
	inFlight      sync.Map // runID → struct{} — prevents duplicate execution

	// Cost components (optional — nil-safe). Wired in PR 3b.
	tokenCounter   *cost.TokenCounter
	budgetEnforcer *cost.BudgetEnforcer
	costReporter   *cost.CostReporter
}

// Option configures a WorkflowScheduler.
type Option func(*WorkflowScheduler)

// WithPollInterval sets the polling interval for pending runs.
func WithPollInterval(d time.Duration) Option {
	return func(s *WorkflowScheduler) {
		if d > 0 {
			s.pollInterval = d
		}
	}
}

// WithMaxConcurrent sets the maximum number of concurrent workflow runs.
func WithMaxConcurrent(n int) Option {
	return func(s *WorkflowScheduler) {
		if n > 0 {
			s.maxConcurrent = n
		}
	}
}

// WithCostComponents injects cost tracking and budget enforcement into the scheduler.
// When provided, the scheduler performs pre-dispatch budget checks and post-dispatch
// token recording for each step.
func WithCostComponents(counter *cost.TokenCounter, enforcer *cost.BudgetEnforcer, reporter *cost.CostReporter) Option {
	return func(s *WorkflowScheduler) {
		s.tokenCounter = counter
		s.budgetEnforcer = enforcer
		s.costReporter = reporter
	}
}

// NewWorkflowScheduler creates a new scheduler with the given dependencies.
func NewWorkflowScheduler(
	store state.Store,
	reg registry.Registry,
	plan planner.Planner,
	exec executor.Executor,
	logger *zap.Logger,
	workflowsDir string,
	opts ...Option,
) *WorkflowScheduler {
	if logger == nil {
		logger = zap.NewNop()
	}
	s := &WorkflowScheduler{
		store:         store,
		registry:      reg,
		planner:       plan,
		executor:      exec,
		logger:        logger,
		workflowsDir:  workflowsDir,
		pollInterval:  1 * time.Second,
		maxConcurrent: 10,
	}
	for _, opt := range opts {
		opt(s)
	}
	return s
}

// Run starts the scheduler polling loop. It blocks until ctx is cancelled.
func (s *WorkflowScheduler) Run(ctx context.Context) error {
	s.logger.Info("scheduler started",
		zap.Duration("pollInterval", s.pollInterval),
		zap.Int("maxConcurrent", s.maxConcurrent),
	)

	ticker := time.NewTicker(s.pollInterval)
	defer ticker.Stop()

	sem := make(chan struct{}, s.maxConcurrent)

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
			s.pollAndExecute(ctx, sem)
		}
	}
}

// pollAndExecute lists pending runs and spawns goroutines to execute them.
func (s *WorkflowScheduler) pollAndExecute(ctx context.Context, sem chan struct{}) {
	runs, err := s.store.ListRuns(ctx)
	if err != nil {
		s.logger.Error("failed to list runs", zap.Error(err))
		return
	}

	for _, run := range runs {
		if run.Status != state.RunPending {
			continue
		}

		// Prevent duplicate execution across poll cycles.
		if _, loaded := s.inFlight.LoadOrStore(run.ID, struct{}{}); loaded {
			continue
		}

		runID := run.ID
		go func() {
			// Acquire semaphore with cancellation to prevent goroutine leaks on shutdown.
			select {
			case <-ctx.Done():
				s.inFlight.Delete(runID)
				return
			case sem <- struct{}{}:
			}
			defer func() {
				<-sem
				s.inFlight.Delete(runID)
			}()

			s.executeRun(ctx, runID)
		}()
	}
}

// executeRun drives a single workflow run through all stages.
func (s *WorkflowScheduler) executeRun(ctx context.Context, runID string) {
	run, err := s.store.GetRun(ctx, runID)
	if err != nil {
		s.logger.Error("failed to get run", zap.String("runID", runID), zap.Error(err))
		return
	}

	// Guard against TOCTOU: the run may have been cancelled via REST API between
	// the time pollAndExecute filtered for RunPending and now.
	if run.Status != state.RunPending {
		s.logger.Info("run no longer pending, skipping",
			zap.String("runID", runID),
			zap.String("status", run.Status.String()),
		)
		return
	}

	s.logger.Info("executing run",
		zap.String("runID", runID),
		zap.String("workflowID", run.WorkflowID),
	)

	// Resolve workflow file path.
	yamlPath, err := resolveWorkflowPath(s.workflowsDir, run.WorkflowID)
	if err != nil {
		s.failRun(ctx, runID, fmt.Sprintf("resolve workflow path: %v", err))
		return
	}

	// Parse workflow YAML.
	wf, err := s.planner.Parse(ctx, yamlPath)
	if err != nil {
		s.failRun(ctx, runID, fmt.Sprintf("parse workflow: %v", err))
		return
	}

	// Validate DAG.
	if err := s.planner.ValidateDAG(ctx, wf); err != nil {
		s.failRun(ctx, runID, fmt.Sprintf("validate DAG: %v", err))
		return
	}

	// Create execution plan.
	plan, err := s.planner.Plan(ctx, wf)
	if err != nil {
		s.failRun(ctx, runID, fmt.Sprintf("plan workflow: %v", err))
		return
	}

	// Transition to Running and set StartedAt.
	// NOTE: if UpdateRunStatus fails (e.g., run deleted between poll and execution),
	// the run remains in-flight until the goroutine exits and inFlight.Delete() fires
	// in pollAndExecute's deferred cleanup. The run stays Pending in the store but
	// won't be re-polled while in-flight. Acceptable for v0.1 — a persistent store
	// backend should surface this via monitoring.
	if err := s.store.UpdateRunStatus(ctx, runID, state.RunRunning); err != nil {
		s.logger.Error("failed to set run status to running", zap.String("runID", runID), zap.Error(err))
		return
	}
	now := time.Now()
	if err := s.store.SetRunTimestamps(ctx, runID, &now, nil); err != nil {
		s.logger.Error("failed to set run start time", zap.String("runID", runID), zap.Error(err))
	}

	// outputs accumulates step output_key → output value across stages.
	var mu sync.Mutex
	outputs := make(map[string]string)

	// Copy run inputs for template variable resolution.
	// NOTE: vars is read-only during stage execution — populated once here before
	// the stages loop. No lock is needed for concurrent reads in executeStep, but
	// DO NOT write to vars from step goroutines without adding synchronization.
	vars := make(map[string]string, len(run.Inputs))
	for k, v := range run.Inputs {
		vars[k] = v
	}

	// Execute stages sequentially; steps within a stage run in parallel.
	for stageIdx, stage := range plan.Stages {
		// Check context between stages.
		select {
		case <-ctx.Done():
			s.failRun(ctx, runID, "context cancelled")
			return
		default:
		}

		s.logger.Debug("executing stage",
			zap.String("runID", runID),
			zap.Int("stage", stageIdx),
			zap.Int("steps", len(stage)),
		)

		if err := s.executeStage(ctx, runID, run.WorkflowID, stage, outputs, vars, &mu); err != nil {
			s.failRun(ctx, runID, fmt.Sprintf("stage %d: %v", stageIdx, err))
			return
		}
	}

	// All stages completed — mark run as completed.
	// Use context.WithoutCancel so state persistence succeeds even if the parent
	// context is cancelled between the last stage completing and these calls
	// (mirrors failRun's cleanup pattern). Without this, a well-timed shutdown
	// leaves the run stuck in Running with all steps Completed.
	cleanupCtx := context.WithoutCancel(ctx)
	if err := s.store.UpdateRunStatus(cleanupCtx, runID, state.RunCompleted); err != nil {
		s.logger.Error("failed to set run status to completed", zap.String("runID", runID), zap.Error(err))
	}
	finished := time.Now()
	if err := s.store.SetRunTimestamps(cleanupCtx, runID, nil, &finished); err != nil {
		s.logger.Error("failed to set run finish time", zap.String("runID", runID), zap.Error(err))
	}

	s.logger.Info("run completed", zap.String("runID", runID))
}

// executeStage fans out parallel steps within a stage and waits for all to complete.
func (s *WorkflowScheduler) executeStage(
	ctx context.Context,
	runID string,
	workflowID string,
	steps []planner.Step,
	outputs map[string]string,
	vars map[string]string,
	mu *sync.Mutex,
) error {
	var wg sync.WaitGroup
	errCh := make(chan error, len(steps))

	for _, step := range steps {
		wg.Add(1)
		go func(step planner.Step) {
			defer wg.Done()

			output, err := s.executeStep(ctx, runID, workflowID, step, outputs, vars, mu)
			if err != nil {
				errCh <- fmt.Errorf("step %q: %w", step.ID, err)
				return
			}

			// Store output if the step has an output_key.
			if step.OutputKey != "" {
				mu.Lock()
				outputs[step.OutputKey] = output
				mu.Unlock()
			}
		}(step)
	}

	wg.Wait()
	close(errCh)

	// Collect all errors from parallel steps so the run-level error message
	// describes every failure, not just the first one (RFC 0003 §executeStage).
	// Individual step failures are also recorded via markStepFailed.
	var errs []error
	for err := range errCh {
		errs = append(errs, err)
	}
	if len(errs) > 0 {
		return errors.Join(errs...)
	}
	return nil
}

// executeStep resolves inputs, dispatches to the executor, and updates step state.
func (s *WorkflowScheduler) executeStep(
	ctx context.Context,
	runID string,
	workflowID string,
	step planner.Step,
	outputs map[string]string,
	vars map[string]string,
	mu *sync.Mutex,
) (string, error) {
	// Track start time locally so subsequent UpdateStepState calls (which do
	// full replacement) preserve the value. See state.InMemoryStore.UpdateStepState.
	startedAt := time.Now()

	// Mark step as running.
	if err := s.store.UpdateStepState(ctx, runID, state.StepState{
		StepID:    step.ID,
		Status:    state.RunRunning,
		StartedAt: startedAt,
	}); err != nil {
		s.logger.Error("failed to update step state to running",
			zap.String("runID", runID),
			zap.String("stepID", step.ID),
			zap.Error(err),
		)
	}

	// Resolve template variables in step input.
	// Lock outputs for reading during template resolution.
	mu.Lock()
	outputsCopy := make(map[string]string, len(outputs))
	for k, v := range outputs {
		outputsCopy[k] = v
	}
	mu.Unlock()

	resolved, err := planner.ResolveInputs(step, outputsCopy, vars, s.logger)
	if err != nil {
		s.markStepFailed(ctx, runID, step.ID, startedAt, err.Error())
		return "", err
	}

	// Dispatch to executor.
	// TODO(v0.2): evaluate step conditions
	limits := s.resolveStepLimits(ctx, step)

	// Pre-dispatch budget check (RFC 0006 PR 3b).
	if s.budgetEnforcer != nil {
		model := s.resolveAgentModel(ctx, step.AgentID)
		if model == "" {
			// NOTE: Empty model means EstimateCost returns 0, making the budget check
			// a no-op for this step. This happens when the registry is nil or the agent
			// is not registered. Log a warning so operators can detect misconfiguration.
			s.logger.Warn("could not resolve model for budget check, cost estimate will be zero",
				zap.String("agentID", step.AgentID),
				zap.String("stepID", step.ID),
			)
		}
		budgetResult := s.budgetEnforcer.CheckBudget(workflowID, step.AgentID, model, int64(limits.MaxTokens))
		if budgetResult.Decision == cost.BudgetReject {
			err := fmt.Errorf("%w: %s", ErrBudgetExceeded, budgetResult.Reason)
			s.markStepFailed(ctx, runID, step.ID, startedAt, err.Error())
			return "", err
		}
	}

	result, err := s.executor.ExecuteTask(ctx, executor.ExecuteRequest{
		WorkflowID: workflowID,
		AgentID:    step.AgentID,
		Payload:    resolved,
		Context:    outputsCopy,
		Limits:     limits,
	})
	if err != nil {
		s.markStepFailed(ctx, runID, step.ID, startedAt, err.Error())
		return "", err
	}

	// Post-dispatch: record token usage and step cost (RFC 0006 PR 3b).
	s.recordStepUsage(ctx, workflowID, step, result)

	// Mark step as completed.
	if err := s.store.UpdateStepState(ctx, runID, state.StepState{
		StepID:     step.ID,
		Status:     state.RunCompleted,
		Output:     result.Output,
		StartedAt:  startedAt,
		FinishedAt: time.Now(),
	}); err != nil {
		s.logger.Error("failed to update step state to completed",
			zap.String("runID", runID),
			zap.String("stepID", step.ID),
			zap.Error(err),
		)
	}

	return result.Output, nil
}

// resolveStepLimits implements the three-level cascade for execution limits:
// workflow step config → agent config → system defaults (RFC 0006 Section A).
// Zero values at any level mean "not configured" and fall through to the next level.
func (s *WorkflowScheduler) resolveStepLimits(ctx context.Context, step planner.Step) executor.StepLimits {
	limits := executor.StepLimits{
		MaxLLMCalls:    defaults.DefaultMaxLLMCalls,
		MaxTokens:      defaults.DefaultMaxTokens,
		TimeoutSeconds: defaults.DefaultTimeoutSeconds,
	}

	// Middle tier: agent config overrides system defaults.
	if s.registry != nil {
		agent, err := s.registry.Get(ctx, step.AgentID)
		if err == nil {
			if agent.MaxLLMCalls > 0 {
				limits.MaxLLMCalls = agent.MaxLLMCalls
			}
			if agent.MaxTokens > 0 {
				limits.MaxTokens = agent.MaxTokens
			}
			if agent.TimeoutSeconds > 0 {
				limits.TimeoutSeconds = agent.TimeoutSeconds
			}
		} else {
			s.logger.Warn("failed to look up agent config for limit resolution, using defaults",
				zap.String("agentID", step.AgentID),
				zap.Error(err),
			)
		}
	}

	// Highest tier: step config overrides agent config.
	if step.MaxLLMCalls > 0 {
		limits.MaxLLMCalls = step.MaxLLMCalls
	}
	if step.MaxTokens > 0 {
		limits.MaxTokens = step.MaxTokens
	}
	if step.TimeoutSeconds > 0 {
		limits.TimeoutSeconds = step.TimeoutSeconds
	}

	return limits
}

// markStepFailed updates the step state to failed with the given error message.
// startedAt is passed explicitly because UpdateStepState does full replacement
// and would otherwise zero out the start time recorded when the step began.
func (s *WorkflowScheduler) markStepFailed(ctx context.Context, runID, stepID string, startedAt time.Time, errMsg string) {
	if err := s.store.UpdateStepState(ctx, runID, state.StepState{
		StepID:     stepID,
		Status:     state.RunFailed,
		Error:      errMsg,
		StartedAt:  startedAt,
		FinishedAt: time.Now(),
	}); err != nil {
		s.logger.Error("failed to update step state to failed",
			zap.String("runID", runID),
			zap.String("stepID", stepID),
			zap.Error(err),
		)
	}
}

// failRun marks a workflow run as failed with the given error message and sets FinishedAt.
// It uses context.WithoutCancel so that cleanup store operations succeed even when
// the parent context is already cancelled (e.g., during graceful shutdown).
func (s *WorkflowScheduler) failRun(ctx context.Context, runID string, errMsg string) {
	s.logger.Error("run failed", zap.String("runID", runID), zap.String("error", errMsg))

	// Detach from parent cancellation — we must persist failure state regardless.
	cleanupCtx := context.WithoutCancel(ctx)

	if err := s.store.UpdateRunStatus(cleanupCtx, runID, state.RunFailed); err != nil {
		s.logger.Error("failed to set run status to failed", zap.String("runID", runID), zap.Error(err))
	}
	if err := s.store.SetRunError(cleanupCtx, runID, errMsg); err != nil {
		s.logger.Error("failed to set run error", zap.String("runID", runID), zap.Error(err))
	}
	finished := time.Now()
	if err := s.store.SetRunTimestamps(cleanupCtx, runID, nil, &finished); err != nil {
		s.logger.Error("failed to set run finish time", zap.String("runID", runID), zap.Error(err))
	}
}

// resolveWorkflowPath constructs the filesystem path for a workflow YAML file.
// Defense-in-depth: validates workflowID format even though the REST API layer
// also validates it. This prevents path traversal if the scheduler is ever
// invoked from a code path that bypasses REST validation.
func resolveWorkflowPath(workflowsDir, workflowID string) (string, error) {
	if !planner.ResourceIDRegex.MatchString(workflowID) {
		return "", fmt.Errorf("invalid workflow ID format: %q", workflowID)
	}
	return filepath.Join(workflowsDir, workflowID+".yaml"), nil
}

// recordStepUsage records token usage from a completed step dispatch.
// Parses tokens_used from the response metadata and records it in the
// TokenCounter and CostReporter. Missing or unparseable metadata is
// handled gracefully with a warning log.
func (s *WorkflowScheduler) recordStepUsage(ctx context.Context, workflowID string, step planner.Step, result *executor.ExecuteResult) {
	if s.tokenCounter == nil || result == nil {
		return
	}

	var inputTokens, outputTokens int64
	if result.Metadata != nil {
		inputTokens = parseMetadataInt64(result.Metadata, "input_tokens", s.logger, step.ID)
		outputTokens = parseMetadataInt64(result.Metadata, "output_tokens", s.logger, step.ID)

		// Fall back to combined "tokens_used" if per-direction tokens are absent.
		// NOTE: tokens_used is mapped entirely to outputTokens — this intentionally
		// overestimates cost because output tokens are priced higher than input tokens.
		// The pessimistic estimate propagates into both TokenCounter running totals
		// and CostReporter step entries, which makes subsequent budget checks more
		// conservative. This is the safe default for budget enforcement.
		if inputTokens == 0 && outputTokens == 0 {
			outputTokens = parseMetadataInt64(result.Metadata, "tokens_used", s.logger, step.ID)
			// Log the fallback so operators can identify agents that should be updated
			// to provide granular input_tokens/output_tokens data. (PR #86 review S-03)
			if outputTokens > 0 {
				s.logger.Info("using tokens_used fallback (all tokens mapped to output, cost may be overestimated)",
					zap.String("stepID", step.ID),
					zap.String("agentID", step.AgentID),
					zap.Int64("tokensUsed", outputTokens),
				)
			}
		}
	}

	if inputTokens == 0 && outputTokens == 0 {
		s.logger.Warn("no token usage in step response metadata, recording zero",
			zap.String("stepID", step.ID),
			zap.String("agentID", step.AgentID),
		)
	}

	model := ""
	if result.Metadata != nil {
		model = result.Metadata["model"]
	}
	if model == "" {
		model = s.resolveAgentModel(ctx, step.AgentID)
	}

	s.tokenCounter.RecordUsage(cost.UsageRecord{
		WorkflowID:   workflowID,
		AgentID:      step.AgentID,
		Model:        model,
		InputTokens:  inputTokens,
		OutputTokens: outputTokens,
	})

	if s.costReporter != nil {
		estimatedCost := s.tokenCounter.Config().EstimateCost(model, inputTokens, outputTokens)
		s.costReporter.RecordStepCost(workflowID, cost.StepCostEntry{
			StepID:       step.ID,
			AgentID:      step.AgentID,
			Model:        model,
			InputTokens:  inputTokens,
			OutputTokens: outputTokens,
			EstimatedUSD: estimatedCost,
		})
	}
}

// resolveAgentModel looks up the model configured for an agent in the registry.
// Returns an empty string if the agent is not found (graceful degradation).
func (s *WorkflowScheduler) resolveAgentModel(ctx context.Context, agentID string) string {
	if s.registry == nil {
		return ""
	}
	agent, err := s.registry.Get(ctx, agentID)
	if err != nil {
		return ""
	}
	return agent.Model
}

// parseMetadataInt64 parses an int64 value from a metadata map.
// Returns 0 if the key is absent or the value is not a valid integer.
func parseMetadataInt64(metadata map[string]string, key string, logger *zap.Logger, stepID string) int64 {
	val, ok := metadata[key]
	if !ok || val == "" {
		return 0
	}
	n, err := strconv.ParseInt(val, 10, 64)
	if err != nil {
		logger.Warn("failed to parse metadata value as int64",
			zap.String("stepID", stepID),
			zap.String("key", key),
			zap.String("value", val),
			zap.Error(err),
		)
		return 0
	}
	// Security: clamp negative values to zero to prevent adversarial agents from
	// reporting negative token counts, which would decrease the running total in
	// TokenCounter and effectively bypass budget enforcement. (PR #86 review F-01)
	if n < 0 {
		logger.Warn("negative token value clamped to zero",
			zap.String("stepID", stepID),
			zap.String("key", key),
			zap.Int64("value", n),
		)
		return 0
	}
	return n
}

// TODO(post-v0.1): Implement retry logic with circuit breaker integration
// TODO(post-v0.1): Implement dead letter queue for failed tasks
