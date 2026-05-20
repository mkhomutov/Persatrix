// stage_runner.go contains per-stage fan-out and per-step dispatch logic.
package scheduler

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/trace"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/executor/packaging"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/state"
)

// executeStage fans out parallel steps within a stage and waits for all to complete.
func (s *WorkflowScheduler) executeStage(
	ctx context.Context,
	runID string,
	workflowID string,
	steps []planner.Step,
	outputs map[string]string,
	vars map[string]string,
	mu *sync.Mutex,
	contextBudgets map[string]int,
) error {
	var wg sync.WaitGroup
	errCh := make(chan error, len(steps))

	for _, step := range steps {
		wg.Add(1)
		go func(step planner.Step) {
			defer wg.Done()

			output, err := s.executeStep(ctx, runID, workflowID, step, outputs, vars, mu, contextBudgets)
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
	contextBudgets map[string]int,
) (string, error) {
	ctx, span := schedulerTracer.Start(ctx, "workflow.step",
		trace.WithAttributes(
			attribute.String("persatrix.run_id", runID),
			attribute.String("persatrix.workflow_id", workflowID),
			attribute.String("persatrix.step_id", step.ID),
			attribute.String("persatrix.agent_id", step.AgentID),
		),
	)
	defer span.End()

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
			zap.String("execution_id", runID),
			zap.String("step_id", step.ID),
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
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
		s.markStepFailed(ctx, runID, step.ID, startedAt, err.Error())
		return "", err
	}

	// Dispatch to executor.
	// TODO(v0.2): evaluate step conditions
	limits := s.resolveStepLimits(ctx, step)

	// Resolve agent model once for both budget check and post-dispatch cost recording.
	// The model from the registry won't change between pre-dispatch and post-dispatch
	// within the same step execution. Post-dispatch recording may override this with
	// the model from response metadata if present. (PR #86 review: eliminate redundant
	// resolveAgentModel call per step)
	registryModel := s.resolveAgentModel(ctx, step.AgentID)

	// Pre-dispatch budget check (RFC 0006 PR 3b).
	//
	// RFC 0023 § G — as of v0.3.2 this check is an *early-fail optimisation*,
	// not the enforcement point. Every LLM call inside the dispatched task
	// acquires a per-call wallet lease from the orchestrator-side
	// WalletService before issuing (RFC 0023 PR 3 wires the workflow-task
	// origin); that agent-side lease is the enforcement point. This
	// pre-dispatch check is kept only because it fails a clearly over-budget
	// workflow before paying the executor-dispatch + agent-startup cost — it
	// is no longer load-bearing for cost correctness.
	//
	// NOTE: Budget check is optimistic — parallel steps within a stage may all
	// pass budget checks simultaneously and collectively exceed the budget.
	// Total potential overspend is bounded by (parallel_steps × max_token_cost).
	// TODO(v0.3): Consider pessimistic budget reservation for high-value workflows.
	if s.budgetEnforcer != nil {
		if registryModel == "" {
			// NOTE: Empty model means EstimateCost returns 0, making the budget check
			// a no-op for this step. This happens when the registry is nil or the agent
			// is not registered. Log a warning so operators can detect misconfiguration.
			s.logger.Warn("could not resolve model for budget check, cost estimate will be zero",
				zap.String("agent_id", step.AgentID),
				zap.String("step_id", step.ID),
			)
		}
		budgetResult := s.budgetEnforcer.CheckBudget(workflowID, step.AgentID, registryModel, int64(limits.MaxTokens))
		if budgetResult.Decision == cost.BudgetReject {
			err := fmt.Errorf("%w: %s", ErrBudgetExceeded, budgetResult.Error)
			span.RecordError(err)
			span.SetStatus(codes.Error, err.Error())
			s.markStepFailed(ctx, runID, step.ID, startedAt, err.Error())
			return "", err
		}
	}

	// RFC 0008 Phase 1: when the workflow opts in (context_budget_total > 0),
	// build a context package and serialize it under TaskRequest.context's
	// reserved `_context_package` key (Open Question 2 — additive; no proto
	// changes). Agents that don't recognise the key continue to use the raw
	// outputs map verbatim.
	//
	// Review (M5): packaging failure is treated as a hard step failure rather
	// than silently degrading to legacy passthrough. The contract for an
	// opted-in workflow is that every step receives a `_context_package` (the
	// integration test asserts both s1 and s2 do); silent degradation would
	// break that invariant invisibly. attachContextPackage's only failure mode
	// today is json.Marshal of a pure-Go struct — effectively a programming
	// bug, so failing loudly is appropriate and surfaces it in CI/staging.
	// PR 1b: capture the built package so its Metrics can flow to the cost
	// record (StepCostEntry.ContextPackage) and so RemainingContextBudget can
	// be persisted on the post-dispatch StepState update. Retries (when
	// scheduler-level retry lands post-v0.3) read RemainingContextBudget from
	// the prior step state via remainingContextBudgetForStep so the second
	// attempt consumes from the persisted remainder rather than the original
	// per-step allocation.
	var pkg *packaging.Package
	var effectiveBudget int
	if budget, ok := contextBudgets[step.ID]; ok && budget > 0 {
		effectiveBudget = s.remainingContextBudgetForStep(ctx, runID, step.ID, budget)
		built, err := s.attachContextPackage(outputsCopy, runID, step, resolved, effectiveBudget)
		if err != nil {
			wrapped := fmt.Errorf("context packaging failed: %w", err)
			s.logger.Error("context packaging failed; failing step",
				zap.String("execution_id", runID),
				zap.String("step_id", step.ID),
				zap.Error(err),
			)
			span.RecordError(wrapped)
			span.SetStatus(codes.Error, wrapped.Error())
			s.markStepFailed(ctx, runID, step.ID, startedAt, wrapped.Error())
			return "", wrapped
		}
		pkg = built
	}

	result, err := s.executor.ExecuteTask(ctx, executor.ExecuteRequest{
		ExecutionID: runID,
		StepID:      step.ID,
		WorkflowID:  workflowID,
		AgentID:     step.AgentID,
		Payload:     resolved,
		Context:     outputsCopy,
		Limits:      limits,
		Cacheable:   step.Cacheable,
	})
	// RFC 0019 PR 3: step-level metrics.  Dispatch count is recorded
	// unconditionally (success + failure both represent a dispatched step);
	// duration is recorded in milliseconds to keep the histogram bucket
	// distribution in the same unit family as the workflow-duration instrument.
	//
	// PR-170 S2: metric attribute keys are unprefixed dotted form per
	// RFC 0019 § F.  See the matching comment in scheduler.go::executeRun
	// for the full rationale.
	stepAttrs := metric.WithAttributes(
		attribute.String("workflow.id", workflowID),
		attribute.String("agent.id", step.AgentID),
		attribute.Bool("step.success", err == nil),
	)
	if s.metrics != nil {
		s.metrics.StepDispatched.Add(ctx, 1, stepAttrs)
		s.metrics.StepDuration.Record(
			ctx,
			float64(time.Since(startedAt).Milliseconds()),
			stepAttrs,
		)
	}
	if err != nil {
		// Record token usage from any metadata the agent returned before failing.
		// This preserves cost tracking for runs aborted by LLM truncation or other
		// agent-side errors where the LLM call completed but the step did not.
		if result != nil {
			s.recordStepUsage(workflowID, step, result, registryModel, pkg)
		}
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
		s.markStepFailed(ctx, runID, step.ID, startedAt, err.Error())
		return "", err
	}

	// Post-dispatch: record token usage and step cost (RFC 0006 PR 3b;
	// RFC 0008 PR 1b plumbs pkg.Metrics into the cost record).
	s.recordStepUsage(workflowID, step, result, registryModel, pkg)

	// Build execution metadata for observability (RFC 0006 PR 4a).
	metadata := s.buildStepMetadata(result, registryModel, step.ID)

	s.logger.Info("step completed",
		zap.String("execution_id", runID),
		zap.String("step_id", step.ID),
		zap.Int("tokensUsed", metadata.TokensUsed),
		zap.Int("retryCount", metadata.RetryCount),
		zap.Int64("wallTimeMs", metadata.WallTimeMs),
		zap.Float64("estimatedCostUSD", metadata.EstimatedCostUSD),
	)
	span.SetAttributes(
		attribute.String("persatrix.status", state.RunCompleted.String()),
		attribute.Int("persatrix.tokens_used", metadata.TokensUsed),
		attribute.Int("persatrix.retry_count", metadata.RetryCount),
		attribute.Int64("persatrix.wall_time_ms", metadata.WallTimeMs),
	)
	span.SetStatus(codes.Ok, "completed")

	// Mark step as completed.
	//
	// RFC 0008 PR 1b: persist RemainingContextBudget so a future scheduler-level
	// retry can resume from the leftover budget rather than re-allocating the
	// full per-step amount. Computed from pkg.Metrics.TokensAfter (== bytes the
	// packager admitted) against the effective budget the packager was given.
	// Clamped to zero so a packager that admits more than the budget (the
	// pinned-overflow path is the only known site today) cannot persist a
	// negative value.
	remainingBudget := remainingFromPackage(effectiveBudget, pkg)
	if err := s.store.UpdateStepState(ctx, runID, state.StepState{
		StepID:                 step.ID,
		Status:                 state.RunCompleted,
		Output:                 result.Output,
		StartedAt:              startedAt,
		FinishedAt:             time.Now(),
		Metadata:               metadata,
		RemainingContextBudget: remainingBudget,
	}); err != nil {
		s.logger.Error("failed to update step state to completed",
			zap.String("execution_id", runID),
			zap.String("step_id", step.ID),
			zap.Error(err),
		)
	}

	return result.Output, nil
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
			zap.String("execution_id", runID),
			zap.String("step_id", stepID),
			zap.Error(err),
		)
	}
}
