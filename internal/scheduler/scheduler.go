// Package scheduler executes workflow plans.
package scheduler

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"sync"
	"time"

	"go.uber.org/zap"

	"github.com/orchestr8/orchestr8/internal/executor"
	"github.com/orchestr8/orchestr8/internal/planner"
	"github.com/orchestr8/orchestr8/internal/registry"
	"github.com/orchestr8/orchestr8/internal/state"
)

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
	if err := s.store.UpdateRunStatus(ctx, runID, state.RunCompleted); err != nil {
		s.logger.Error("failed to set run status to completed", zap.String("runID", runID), zap.Error(err))
	}
	finished := time.Now()
	if err := s.store.SetRunTimestamps(ctx, runID, nil, &finished); err != nil {
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
	// Mark step as running.
	if err := s.store.UpdateStepState(ctx, runID, state.StepState{
		StepID:    step.ID,
		Status:    state.RunRunning,
		StartedAt: time.Now(),
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
		s.markStepFailed(ctx, runID, step.ID, err.Error())
		return "", err
	}

	// Dispatch to executor.
	// TODO(v0.2): evaluate step conditions
	result, err := s.executor.ExecuteTask(ctx, executor.ExecuteRequest{
		WorkflowID: workflowID,
		AgentID:    step.AgentID,
		Payload:    resolved,
	})
	if err != nil {
		s.markStepFailed(ctx, runID, step.ID, err.Error())
		return "", err
	}

	// Mark step as completed.
	if err := s.store.UpdateStepState(ctx, runID, state.StepState{
		StepID:     step.ID,
		Status:     state.RunCompleted,
		Output:     result.Output,
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

// markStepFailed updates the step state to failed with the given error message.
func (s *WorkflowScheduler) markStepFailed(ctx context.Context, runID, stepID, errMsg string) {
	if err := s.store.UpdateStepState(ctx, runID, state.StepState{
		StepID:     stepID,
		Status:     state.RunFailed,
		Error:      errMsg,
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

// TODO: Implement retry logic with circuit breaker integration (post-v0.1)
// TODO: Implement dead letter queue for failed tasks (post-v0.1)
