package executor

import (
	"context"
	"errors"
	"fmt"
	"math"
	"math/rand/v2"
	"time"

	"github.com/google/uuid"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	otelcodes "go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
	"go.uber.org/zap"
	"google.golang.org/grpc"
	grpcodes "google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"

	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/defaults"
	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
	"github.com/mkhomutov/persatrix/internal/observability/grpcmeta"
	"github.com/mkhomutov/persatrix/internal/observability/zapenc"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/security"
)

var executorTracer = otel.Tracer("persatrix/executor")

// ExecuteTask dispatches a task to the specified agent via gRPC with retry logic.
// It looks up the agent in the registry, verifies health status, establishes a
// per-task gRPC connection, and sends an ExecuteTask RPC.
//
// In derived deadline mode, the RPC timeout is computed from the step's
// TimeoutSeconds + DefaultTransportMargin. Retries share the step deadline —
// each attempt gets the remaining time rather than a fresh window. If less than
// MinRetryBudgetFraction of the original time or token budget remains, the retry
// is skipped. In static mode, each attempt gets the per-executor timeout.
func (e *GRPCExecutor) ExecuteTask(ctx context.Context, req ExecuteRequest) (*ExecuteResult, error) {
	ctx, span := executorTracer.Start(ctx, "agent.dispatch",
		trace.WithAttributes(
			attribute.String("persatrix.workflow_id", req.WorkflowID),
			attribute.String("persatrix.agent_id", req.AgentID),
		),
	)
	defer span.End()

	// RFC 0018 Phase 3 — inject the four correlation IDs onto the outgoing
	// gRPC metadata once for the whole retry loop.  AppendToOutgoingContext
	// returns a new ctx; every retry's client.ExecuteTask call inherits the
	// same metadata so log records on the agent side correlate correctly
	// even on the second/third attempt.
	ctx = grpcmeta.InjectIDs(ctx, grpcmeta.IDs{
		ExecutionID: req.ExecutionID,
		StepID:      req.StepID,
		AgentID:     req.AgentID,
		WorkflowID:  req.WorkflowID,
	})

	// Bind trace_id / span_id onto every log line emitted from this dispatch
	// — the schema's Optional contract (RFC 0018 § B) covers absence when no
	// span is active (the helper returns the original logger unchanged in
	// that case).  Re-shadowing e.logger in this scope keeps the existing
	// retry-loop call sites untouched.
	logger := zapenc.LoggerWithContext(ctx, e.logger)

	// Check cache for cacheable steps before any network I/O.
	if req.Cacheable && e.cache != nil {
		cacheKey := cost.CacheKey(req.AgentID, req.Payload, req.Context)
		if cached, ok := e.cache.Get(cacheKey); ok {
			logger.Info("cache hit, skipping gRPC dispatch",
				zap.String("agent_id", req.AgentID),
			)
			return &ExecuteResult{
				TaskID:   req.TaskID,
				Output:   cached.Output,
				Metadata: cached.Metadata,
				CacheHit: true,
			}, nil
		}
	}

	// Look up agent in registry.
	agent, err := e.registry.Get(ctx, req.AgentID)
	if err != nil {
		if errors.Is(err, registry.ErrAgentNotFound) {
			span.RecordError(err)
			span.SetStatus(otelcodes.Error, err.Error())
			return nil, fmt.Errorf("%w: %s", ErrAgentNotFound, req.AgentID)
		}
		span.RecordError(err)
		span.SetStatus(otelcodes.Error, err.Error())
		return nil, fmt.Errorf("registry lookup: %w", err)
	}

	// Check agent health before dialing.
	if agent.Status != registry.StatusHealthy {
		err := fmt.Errorf("%w: agent %s status is %s", ErrAgentNotReady, req.AgentID, agent.Status)
		span.RecordError(err)
		span.SetStatus(otelcodes.Error, err.Error())
		return nil, err
	}

	// Generate task ID if not provided.
	taskID := req.TaskID
	if taskID == "" {
		taskID = uuid.New().String()
	}
	span.SetAttributes(attribute.String("persatrix.task_id", taskID))

	// Build gRPC request.
	// F-02: Clamp int → int32 casts to prevent silent wraparound to negative
	// values in protobuf. Without this, values > math.MaxInt32 (e.g., from
	// programmatic misuse) would wrap negative and be interpreted by the agent
	// as extremely restrictive limits.
	maxLLMCalls := req.Limits.MaxLLMCalls
	maxTokens := req.Limits.MaxTokens
	timeoutSeconds := req.Limits.TimeoutSeconds
	if maxLLMCalls > math.MaxInt32 {
		logger.Warn("MaxLLMCalls exceeds int32 range, clamping to max",
			zap.Int("original", maxLLMCalls),
			zap.Int("clamped", math.MaxInt32),
		)
		maxLLMCalls = math.MaxInt32
	}
	if maxTokens > math.MaxInt32 {
		logger.Warn("MaxTokens exceeds int32 range, clamping to max",
			zap.Int("original", maxTokens),
			zap.Int("clamped", math.MaxInt32),
		)
		maxTokens = math.MaxInt32
	}
	if timeoutSeconds > math.MaxInt32 {
		logger.Warn("TimeoutSeconds exceeds int32 range, clamping to max",
			zap.Int("original", timeoutSeconds),
			zap.Int("clamped", math.MaxInt32),
		)
		timeoutSeconds = math.MaxInt32
	}
	grpcReq := &taskpb.TaskRequest{
		TaskId:     taskID,
		WorkflowId: req.WorkflowID,
		AgentId:    req.AgentID,
		Payload:    req.Payload,
		Context:    req.Context,
		Config: &taskpb.TaskConfig{
			MaxLlmCalls:    int32(maxLLMCalls),
			MaxTokens:      int32(maxTokens),
			TimeoutSeconds: int32(timeoutSeconds),
		},
	}

	// Compute step deadline and per-dispatch timeout based on deadline mode.
	stepDeadline, dispatchTimeout := e.resolveDeadline(req.Limits)

	// Precompute minimum budget thresholds for derived-mode retry decisions.
	// These are constant across retry iterations — they depend on the step's
	// configured limits, not the attempt number.
	minBudget := time.Duration(float64(stepDeadline) * defaults.MinRetryBudgetFraction)
	minTokens := int64(float64(req.Limits.MaxTokens) * defaults.MinRetryBudgetFraction)

	// Retry loop with exponential backoff + jitter.
	// In derived mode, retries share the step deadline — elapsed time is tracked
	// and each retry gets the remaining budget.
	var lastErr error
	var lastResult *ExecuteResult // preserves metadata from the last dispatch attempt for cost recording
	start := time.Now()
	var cumulativeTokens int64

	for attempt := range e.maxRetries + 1 {
		// In derived mode, check whether enough time budget remains for a retry.
		// Guarded on TimeoutSeconds > 0 so that zero-timeout steps — which fall
		// back to static dispatch in resolveDeadline — also get fully static retry
		// behavior without budget accounting. This mirrors the MaxTokens > 0 guard
		// on the token budget check below.
		if e.deadlineMode == DeadlineModeDerived && attempt > 0 && req.Limits.TimeoutSeconds > 0 {
			elapsed := time.Since(start)
			remaining := stepDeadline - elapsed
			if remaining < minBudget {
				logger.Warn("retry skipped: insufficient time budget",
					zap.String("agent_id", req.AgentID),
					zap.String("taskID", taskID),
					zap.Int("attempt", attempt),
					zap.Duration("remaining", remaining),
					zap.Duration("minBudget", minBudget),
				)
				break
			}
			// Update dispatch timeout: remaining step budget + transport margin.
			// `remaining` is the time left until the step deadline (stepDeadline - elapsed),
			// so this gives the RPC the full remaining budget plus overhead.
			// S6 invariant: dispatchTimeout > remaining always holds because
			// DefaultTransportMargin > 0. This ensures the gRPC context deadline
			// outlives the logical step deadline, so the agent sees context
			// cancellation from the step deadline, not from the transport layer.
			// If transport margin were 0, the gRPC deadline and step deadline
			// would race, producing non-deterministic timeout vs cancellation errors.
			dispatchTimeout = remaining + time.Duration(defaults.DefaultTransportMargin)*time.Second
		}

		// In derived mode, check token budget before retry.
		// Note: token budget cutoff is currently infrastructure-only — parseTokensUsed
		// returns 0 until gRPC trailer metadata parsing is implemented in PR 3a.
		// The logic is exercised via WithTokenParser injection in tests.
		if e.deadlineMode == DeadlineModeDerived && attempt > 0 && req.Limits.MaxTokens > 0 {
			remaining := int64(req.Limits.MaxTokens) - cumulativeTokens
			if remaining < minTokens {
				logger.Warn("retry skipped: insufficient token budget",
					zap.String("agent_id", req.AgentID),
					zap.String("taskID", taskID),
					zap.Int("attempt", attempt),
					zap.Int64("tokensUsed", cumulativeTokens),
					zap.Int("maxTokens", req.Limits.MaxTokens),
				)
				break
			}
		}

		result, err := e.dispatch(ctx, agent.Address, grpcReq, dispatchTimeout)
		if err == nil {
			wallTimeMs := time.Since(start).Milliseconds()
			result.RetryCount = attempt
			result.WallTimeMs = wallTimeMs
			logger.Info("step dispatched",
				zap.String("agent_id", req.AgentID),
				zap.String("taskID", taskID),
				zap.Int("retryCount", attempt),
				zap.Int64("wallTimeMs", wallTimeMs),
			)

			// RFC 0009 PR 1b — emit tool.invoked (telemetry-class, batched).
			// Action carries the agent capability invoked; Resource is
			// agent_id (not the agent address — addresses rotate, agent IDs
			// are the stable forensic anchor). Detail records workflow/step
			// IDs so the audit chain links to the workflow run audit trail.
			// Emit is best-effort: a stalled audit sink must not block the
			// dispatch hot path.
			//
			// PR #234 review M-2: detach the parent ctx via
			// [context.WithoutCancel]. The dispatch already succeeded;
			// tying the audit emit to ctx means a cancellation racing the
			// successful return (e.g. caller deadline expiring just after
			// dispatch unblocks, or a derived-deadline budget that consumed
			// nearly the whole window) silently drops the only forensic
			// record of the completed side effect via Emit's `ctx.Err()`
			// short-circuit. Values still propagate so trace/correlation
			// IDs are preserved on the emitted event.
			//
			// PR #234 review L-5: prior code discarded the Emit error with
			// `_ = ...` while the inline comment claimed it landed in debug
			// logs — it didn't. Honour the documented contract.
			if e.auditor != nil {
				emitCtx := context.WithoutCancel(ctx)
				if emitErr := e.auditor.Emit(emitCtx, security.AuditEvent{
					EventType: security.AuditToolInvoked,
					AgentID:   req.AgentID,
					Action:    "execute_task",
					Resource:  req.AgentID,
					Detail: map[string]any{
						"workflow_id":  req.WorkflowID,
						"step_id":      req.StepID,
						"task_id":      req.TaskID,
						"retry_count":  attempt,
						"wall_time_ms": wallTimeMs,
						"cache_hit":    result.CacheHit,
					},
				}); emitErr != nil {
					logger.Debug("audit emit failed",
						zap.String("event_type", string(security.AuditToolInvoked)),
						zap.String("agent_id", req.AgentID),
						zap.Error(emitErr),
					)
				}
			}

			// Store result in cache for cacheable steps.
			if req.Cacheable && e.cache != nil {
				cacheKey := cost.CacheKey(req.AgentID, req.Payload, req.Context)
				e.cache.Put(cacheKey, cost.CachedResponse{
					Output:   result.Output,
					Metadata: result.Metadata,
				})
			}

			span.SetAttributes(
				attribute.Int("persatrix.retry_count", attempt),
				attribute.Int64("persatrix.wall_time_ms", wallTimeMs),
				attribute.Bool("persatrix.cache_hit", result.CacheHit),
			)
			span.SetStatus(otelcodes.Ok, "dispatched")

			return result, nil
		}

		// Track token usage from failed attempts for budget accounting.
		if e.deadlineMode == DeadlineModeDerived {
			cumulativeTokens += e.tokenParser(err)
		}

		lastErr = err
		lastResult = result // may carry response metadata even on agent-side failure

		// Don't retry on non-transient errors or if retries exhausted.
		if !isTransient(err) {
			span.RecordError(err)
			span.SetStatus(otelcodes.Error, err.Error())
			// Propagate partial result so callers can record token usage from metadata.
			return result, fmt.Errorf("permanent failure dispatching to agent %s: %w", req.AgentID, err)
		}

		if attempt == e.maxRetries {
			break
		}

		// Exponential backoff: base = 100ms * 2^attempt, jitter ∈ [0.75, 1.25).
		base := 100 * time.Millisecond * (1 << uint(attempt))
		jitter := 0.75 + rand.Float64()*0.5 // [0.75, 1.25)
		delay := time.Duration(float64(base) * jitter)

		logger.Warn("transient failure, retrying",
			zap.String("agent_id", req.AgentID),
			zap.String("taskID", taskID),
			zap.Int("attempt", attempt+1),
			zap.Int("maxRetries", e.maxRetries),
			zap.Duration("delay", delay),
			zap.Error(err),
		)

		// TODO(deferred-S9): Consider checking remaining - delay >= minBudget
		// before sleeping, to avoid wasting the backoff window when the time
		// budget is nearly exhausted. Deferred: marginal benefit (~500ms
		// window vs 60s+ deadlines). See PR 5a review.
		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			timer.Stop()
			span.RecordError(ctx.Err())
			span.SetStatus(otelcodes.Error, ctx.Err().Error())
			// Propagate partial result so callers can record token usage from
			// the last dispatch attempt's metadata. Symmetric with the
			// permanent-failure and retry-exhaustion paths above — the
			// scheduler's stage_runner already treats any non-nil result on
			// error as cost-recordable data. (PR #101 review: nice-to-have #9)
			return lastResult, ctx.Err()
		case <-timer.C:
		}
	}

	if lastErr != nil {
		span.RecordError(lastErr)
		span.SetStatus(otelcodes.Error, lastErr.Error())
	}
	// Propagate partial result so callers can record token usage from metadata.
	return lastResult, fmt.Errorf("retries exhausted (%d attempts) for agent %s: %w", e.maxRetries+1, req.AgentID, lastErr)
}

// resolveDeadline computes the step deadline duration and the initial per-dispatch
// timeout based on the executor's deadline mode and the step's configured limits.
func (e *GRPCExecutor) resolveDeadline(limits StepLimits) (stepDeadline, dispatchTimeout time.Duration) {
	if e.deadlineMode == DeadlineModeDerived && limits.TimeoutSeconds > 0 {
		stepDeadline = time.Duration(limits.TimeoutSeconds) * time.Second
		dispatchTimeout = stepDeadline + time.Duration(defaults.DefaultTransportMargin)*time.Second
		e.logger.Debug("derived deadline computed",
			zap.Duration("stepDeadline", stepDeadline),
			zap.Duration("dispatchTimeout", dispatchTimeout),
			zap.Int("timeoutSeconds", limits.TimeoutSeconds),
		)
		return stepDeadline, dispatchTimeout
	}
	// Static mode or zero timeout: use per-executor timeout for each dispatch.
	return e.timeout, e.timeout
}

// dispatch performs a single gRPC ExecuteTask call to the agent at the given address.
func (e *GRPCExecutor) dispatch(ctx context.Context, address string, req *taskpb.TaskRequest, timeout time.Duration) (*ExecuteResult, error) {
	// Build dial options. Transport credentials are always included as the base
	// so callers using WithDialOptions (e.g., custom interceptors) don't need to
	// independently remember to supply credentials. (N-06)
	// TODO(security): enable mTLS — replace insecure credentials here.
	opts := make([]grpc.DialOption, 0, len(e.dialOpts)+1)
	opts = append(opts, grpc.WithTransportCredentials(insecure.NewCredentials()))
	opts = append(opts, e.dialOpts...)

	// TODO(v0.2): connection pooling — reuse connections across tasks.
	conn, err := grpc.NewClient(address, opts...)
	if err != nil {
		return nil, fmt.Errorf("dial agent at %s: %w", address, err)
	}
	defer conn.Close()

	client := taskpb.NewAgentServiceClient(conn)

	// Apply per-dispatch timeout.
	callCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	resp, err := client.ExecuteTask(callCtx, req)
	if err != nil {
		return nil, err
	}

	// Check response status — only COMPLETED is a successful outcome.
	// PENDING (proto default = 0), RUNNING, CANCELLED, and RETRYING are rejected
	// as unexpected because agents must return a terminal status.
	if resp.Status == taskpb.TaskStatus_FAILED {
		errMsg := resp.ErrorMessage
		if errMsg == "" {
			errMsg = "unknown error"
		}
		// Return metadata alongside the error so callers can record token usage
		// from LLM calls the agent completed before failing.
		return &ExecuteResult{
			TaskID:   resp.TaskId,
			Metadata: resp.Metadata,
		}, fmt.Errorf("%w: %s", ErrTaskFailed, errMsg)
	}
	if resp.Status != taskpb.TaskStatus_COMPLETED {
		return nil, fmt.Errorf("%w: %s", ErrUnexpectedStatus, resp.Status)
	}

	return &ExecuteResult{
		TaskID:   resp.TaskId,
		Output:   resp.Result,
		Metadata: resp.Metadata,
	}, nil
}

// parseTokensUsed extracts token usage from a gRPC error's trailing metadata.
// Returns 0 if the error doesn't carry token information — this is the common case
// for transport-level errors. Token usage from successful responses is tracked
// separately via ExecuteResult.Metadata.
func parseTokensUsed(err error) int64 {
	// In v0.2, agent errors may carry token metadata via gRPC trailers.
	// For now, return 0 — the primary budget enforcement is time-based.
	_ = err
	return 0
}

// isTransient classifies whether an error is transient and eligible for retry.
// gRPC Unavailable, ResourceExhausted, and Aborted are transient.
// DeadlineExceeded is permanent (retrying with the same timeout won't help).
// Canceled is permanent (retrying with a cancelled context always fails — see N-12 test).
// Non-gRPC errors (DNS, connection refused, etc.) are treated as transient.
// Application-level permanent errors (ErrTaskFailed, ErrUnexpectedStatus) from
// dispatch are explicitly excluded — these indicate the agent processed the
// request and returned a terminal failure, so retrying is wasteful.
func isTransient(err error) bool {
	// Agent returned an explicit failure or non-terminal status — permanent.
	if errors.Is(err, ErrTaskFailed) || errors.Is(err, ErrUnexpectedStatus) {
		return false
	}

	st, ok := status.FromError(err)
	if !ok {
		// Non-gRPC error (DNS failure, connection refused, etc.) — transient.
		return true
	}

	switch st.Code() {
	case grpcodes.Unavailable, grpcodes.ResourceExhausted, grpcodes.Aborted:
		return true
	default:
		return false
	}
}

// Close releases any resources held by the executor.
// Returns nil in v0.1 (no persistent connections).
func (e *GRPCExecutor) Close() error {
	return nil
}
