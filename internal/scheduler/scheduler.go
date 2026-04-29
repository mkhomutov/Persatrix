// Package scheduler executes workflow plans.
package scheduler

import (
	"context"
	"fmt"
	"path/filepath"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/trace"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/executor/packaging"
	obsmetrics "github.com/mkhomutov/persatrix/internal/observability/metrics"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

var schedulerTracer = otel.Tracer("persatrix/scheduler")

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

	// Metrics instruments (optional — nil-safe).  Wired in RFC 0019 PR 3.
	metrics *obsmetrics.Instruments

	// packager assembles per-step context packages when the workflow opts in
	// via context_budget_total > 0 (RFC 0008 Phase 1). Always non-nil — a
	// scheduler constructed without an explicit packager uses the default
	// HeuristicScorer-backed packager.
	packager *packaging.Packager

	// warningSampler bounds how many times each (execution_id, step_id,
	// warning) tuple emits a structured zap warning from the context-package
	// pipeline (RFC 0008 PR 1b — finding L11). The cost record on
	// StepCostEntry.ContextPackage carries the unsampled metric for every
	// step, so capping log noise here loses no telemetry.
	warningSampler warningSampler
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

// WithMetrics injects the orchestrator metric instruments so executeRun and
// executeStep can record workflow / step lifecycle counters and histograms.
// When nil, all metric recording is skipped (nil-safe per RFC 0019 § F).
func WithMetrics(inst *obsmetrics.Instruments) Option {
	return func(s *WorkflowScheduler) {
		s.metrics = inst
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
		packager:      packaging.NewPackager(nil),
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
	// M11 (RFC 0008 PR 6a): drop the per-run sampler bucket on terminal
	// status so the warningSampler.runs map does not accumulate state for
	// completed runs across a long-running orchestrator. Runs that never
	// emitted a sampled warning are no-ops here.
	defer s.warningSampler.pruneRun(runID)
	run, err := s.store.GetRun(ctx, runID)
	if err != nil {
		s.logger.Error("failed to get run", zap.String("execution_id", runID), zap.Error(err))
		return
	}

	ctx, span := schedulerTracer.Start(ctx, "workflow.run",
		trace.WithAttributes(
			attribute.String("persatrix.run_id", runID),
			attribute.String("persatrix.workflow_id", run.WorkflowID),
		),
	)
	defer span.End()

	// Guard against TOCTOU: the run may have been cancelled via REST API between
	// the time pollAndExecute filtered for RunPending and now.
	if run.Status != state.RunPending {
		span.SetAttributes(attribute.String("persatrix.status", run.Status.String()))
		s.logger.Info("run no longer pending, skipping",
			zap.String("execution_id", runID),
			zap.String("status", run.Status.String()),
		)
		return
	}

	s.logger.Info("executing run",
		zap.String("execution_id", runID),
		zap.String("workflow_id", run.WorkflowID),
	)

	// RFC 0019 PR 3: emit workflow-start metrics.  Submitted-counter emission
	// lives at the REST submit boundary (server.go); here we only bump the
	// ``workflow.active`` gauge and record a duration timer so completion /
	// failure paths can emit the histogram with stable attribute keys.
	//
	// PR-170 S2: metric attribute keys use the unprefixed dotted form
	// (``workflow.id``, ``agent.id``) per RFC 0019 § F — the freshly-
	// canonicalised orchestrator metric inventory.  ``persatrix.*`` is
	// reserved for vendor-specific dimensions (e.g. ``persatrix.llm.cache.hit``
	// on the agent side).  Span attributes intentionally retain the
	// ``persatrix.workflow_id`` form — that is the established repo-wide
	// span-attribute convention used by every tracer call site, and joining
	// metrics to spans by trace_id (via exemplars) makes attribute-name
	// alignment between the two signals unnecessary.
	runStarted := time.Now()
	wfAttrs := metric.WithAttributes(
		attribute.String("workflow.id", run.WorkflowID),
	)
	if s.metrics != nil {
		s.metrics.WorkflowActive.Add(ctx, 1, wfAttrs)
	}
	// Failure metric emission happens in a defer so every failRun() branch
	// below participates without each branch having to call the recorder.
	// ``runSucceeded`` is flipped to true at the tail of the happy path; the
	// defer runs on the cleanupCtx so emission survives parent cancellation.
	runSucceeded := false
	defer func() {
		if s.metrics == nil {
			return
		}
		if runSucceeded {
			return
		}
		cleanupCtx := context.WithoutCancel(ctx)
		s.metrics.WorkflowFailed.Add(cleanupCtx, 1, wfAttrs)
		s.metrics.WorkflowActive.Add(cleanupCtx, -1, wfAttrs)
		s.metrics.WorkflowDuration.Record(
			cleanupCtx,
			float64(time.Since(runStarted).Milliseconds()),
			wfAttrs,
		)
	}()

	// Resolve workflow file path.
	yamlPath, err := resolveWorkflowPath(s.workflowsDir, run.WorkflowID)
	if err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
		s.failRun(ctx, runID, fmt.Sprintf("resolve workflow path: %v", err))
		return
	}

	// Parse workflow YAML.
	wf, err := s.planner.Parse(ctx, yamlPath)
	if err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
		s.failRun(ctx, runID, fmt.Sprintf("parse workflow: %v", err))
		return
	}

	// Validate DAG.
	if err := s.planner.ValidateDAG(ctx, wf); err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
		s.failRun(ctx, runID, fmt.Sprintf("validate DAG: %v", err))
		return
	}

	// Create execution plan.
	plan, err := s.planner.Plan(ctx, wf)
	if err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
		s.failRun(ctx, runID, fmt.Sprintf("plan workflow: %v", err))
		return
	}

	// RFC 0008: compute per-step context budgets once after planning. Returns
	// nil when context_budget_total is 0 (packaging disabled — legacy passthrough).
	contextBudgets := allocateContextBudgets(wf.ContextBudgetTotal, wf.Steps)

	// Transition to Running and set StartedAt.
	// NOTE: if UpdateRunStatus fails (e.g., run deleted between poll and execution),
	// the run remains in-flight until the goroutine exits and inFlight.Delete() fires
	// in pollAndExecute's deferred cleanup. The run stays Pending in the store but
	// won't be re-polled while in-flight. Acceptable for v0.1 — a persistent store
	// backend should surface this via monitoring.
	if err := s.store.UpdateRunStatus(ctx, runID, state.RunRunning); err != nil {
		s.logger.Error("failed to set run status to running", zap.String("execution_id", runID), zap.Error(err))
		return
	}
	now := time.Now()
	if err := s.store.SetRunTimestamps(ctx, runID, &now, nil); err != nil {
		s.logger.Error("failed to set run start time", zap.String("execution_id", runID), zap.Error(err))
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
			span.RecordError(ctx.Err())
			span.SetStatus(codes.Error, ctx.Err().Error())
			s.failRun(ctx, runID, "context cancelled")
			return
		default:
		}

		s.logger.Debug("executing stage",
			zap.String("execution_id", runID),
			zap.Int("stage", stageIdx),
			zap.Int("steps", len(stage)),
		)

		if err := s.executeStage(ctx, runID, run.WorkflowID, stage, outputs, vars, &mu, contextBudgets); err != nil {
			span.RecordError(err)
			span.SetStatus(codes.Error, err.Error())
			s.failRun(ctx, runID, fmt.Sprintf("stage %d: %v", stageIdx, err))
			return
		}
	}

	// All stages completed — mark run as completed.
	// Use context.WithoutCancel so state persistence succeeds even if the parent
	// context is cancelled between the last stage completing and these calls
	// (mirrors failRun's cleanup pattern). Without this, a well-timed shutdown
	// leaves the run stuck in Running with all steps Completed.
	//
	// Set FinishedAt before transitioning to RunCompleted so that any observer
	// that sees RunCompleted is guaranteed to also see a non-zero FinishedAt.
	cleanupCtx := context.WithoutCancel(ctx)
	finished := time.Now()
	if err := s.store.SetRunTimestamps(cleanupCtx, runID, nil, &finished); err != nil {
		s.logger.Error("failed to set run finish time", zap.String("execution_id", runID), zap.Error(err))
	}
	if err := s.store.UpdateRunStatus(cleanupCtx, runID, state.RunCompleted); err != nil {
		s.logger.Error("failed to set run status to completed", zap.String("execution_id", runID), zap.Error(err))
	}

	s.logger.Info("run completed", zap.String("execution_id", runID))
	span.SetAttributes(attribute.String("persatrix.status", state.RunCompleted.String()))
	span.SetStatus(codes.Ok, "completed")

	// PR-170 N2: disarm the failure-emission defer *before* recording success
	// metrics so a hypothetical panic inside ``Add`` / ``Record`` (extremely
	// unlikely on these no-allocation hot paths, but technically possible)
	// does not cause the defer to also classify the run as failed and
	// double-count it on both the failed and completed counters.
	runSucceeded = true

	if s.metrics != nil {
		s.metrics.WorkflowCompleted.Add(ctx, 1, wfAttrs)
		s.metrics.WorkflowActive.Add(ctx, -1, wfAttrs)
		s.metrics.WorkflowDuration.Record(
			ctx,
			float64(time.Since(runStarted).Milliseconds()),
			wfAttrs,
		)
	}
}

// failRun marks a workflow run as failed with the given error message and sets FinishedAt.
// It uses context.WithoutCancel so that cleanup store operations succeed even when
// the parent context is already cancelled (e.g., during graceful shutdown).
func (s *WorkflowScheduler) failRun(ctx context.Context, runID string, errMsg string) {
	s.logger.Error("run failed", zap.String("execution_id", runID), zap.String("error", errMsg))

	// Detach from parent cancellation — we must persist failure state regardless.
	cleanupCtx := context.WithoutCancel(ctx)

	// Set FinishedAt before transitioning to RunFailed so that any observer
	// that sees RunFailed is guaranteed to also see a non-zero FinishedAt.
	finished := time.Now()
	if err := s.store.SetRunTimestamps(cleanupCtx, runID, nil, &finished); err != nil {
		s.logger.Error("failed to set run finish time", zap.String("execution_id", runID), zap.Error(err))
	}
	if err := s.store.SetRunError(cleanupCtx, runID, errMsg); err != nil {
		s.logger.Error("failed to set run error", zap.String("execution_id", runID), zap.Error(err))
	}
	if err := s.store.UpdateRunStatus(cleanupCtx, runID, state.RunFailed); err != nil {
		s.logger.Error("failed to set run status to failed", zap.String("execution_id", runID), zap.Error(err))
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

// TODO(post-v0.1): Implement retry logic with circuit breaker integration
// TODO(post-v0.1): Implement dead letter queue for failed tasks
