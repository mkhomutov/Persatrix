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
	"go.opentelemetry.io/otel/trace"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/executor"
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
				zap.String("agentID", step.AgentID),
				zap.String("stepID", step.ID),
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

	result, err := s.executor.ExecuteTask(ctx, executor.ExecuteRequest{
		WorkflowID: workflowID,
		AgentID:    step.AgentID,
		Payload:    resolved,
		Context:    outputsCopy,
		Limits:     limits,
		Cacheable:  step.Cacheable,
	})
	if err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
		s.markStepFailed(ctx, runID, step.ID, startedAt, err.Error())
		return "", err
	}

	// Post-dispatch: record token usage and step cost (RFC 0006 PR 3b).
	s.recordStepUsage(workflowID, step, result, registryModel)

	// Build execution metadata for observability (RFC 0006 PR 4a).
	metadata := s.buildStepMetadata(result, registryModel, step.ID)

	s.logger.Info("step completed",
		zap.String("runID", runID),
		zap.String("stepID", step.ID),
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
	if err := s.store.UpdateStepState(ctx, runID, state.StepState{
		StepID:     step.ID,
		Status:     state.RunCompleted,
		Output:     result.Output,
		StartedAt:  startedAt,
		FinishedAt: time.Now(),
		Metadata:   metadata,
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
