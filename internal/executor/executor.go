// Package executor handles gRPC communication with agent processes.
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
	"github.com/mkhomutov/persatrix/internal/registry"
)

var executorTracer = otel.Tracer("persatrix/executor")

// DeadlineMode controls how per-dispatch RPC timeouts are computed.
type DeadlineMode string

const (
	// DeadlineModeDerived computes the RPC timeout from the step's
	// TimeoutSeconds + DefaultTransportMargin. Retries share the step deadline.
	DeadlineModeDerived DeadlineMode = "derived"
	// DeadlineModeStatic uses the per-executor timeout for every dispatch,
	// preserving pre-PR 2 behavior.
	DeadlineModeStatic DeadlineMode = "static"
)

// Sentinel errors for executor operations.
var (
	ErrAgentNotFound    = errors.New("agent not found in registry")
	ErrAgentNotReady    = errors.New("agent is not healthy")
	ErrTaskFailed       = errors.New("task execution failed")
	ErrUnexpectedStatus = errors.New("unexpected task status from agent")
)

// StepLimits holds resolved execution limits for a single step dispatch.
// These are populated by the scheduler's three-level cascade (step config →
// agent config → system defaults) before being passed to the executor.
type StepLimits struct {
	MaxLLMCalls    int
	MaxTokens      int
	TimeoutSeconds int
}

// ExecuteRequest contains the parameters for a task dispatch.
type ExecuteRequest struct {
	TaskID     string
	WorkflowID string
	AgentID    string
	Payload    string
	Context    map[string]string
	Limits     StepLimits
	Cacheable  bool
}

// ExecuteResult contains the outcome of a task dispatch.
type ExecuteResult struct {
	TaskID     string
	Output     string
	Metadata   map[string]string
	RetryCount int
	WallTimeMs int64
	CacheHit   bool
}

// Executor defines the interface for dispatching tasks to agents.
type Executor interface {
	ExecuteTask(ctx context.Context, req ExecuteRequest) (*ExecuteResult, error)
	Close() error
}

// Option configures a GRPCExecutor.
type Option func(*GRPCExecutor)

// WithTimeout sets the per-task gRPC call timeout.
// A zero or negative duration is clamped to 1 second to prevent
// immediately-expired contexts on every dispatch.
func WithTimeout(d time.Duration) Option {
	return func(e *GRPCExecutor) {
		if d <= 0 {
			d = time.Second
		}
		e.timeout = d
	}
}

// WithMaxRetries sets the maximum number of retries for transient failures.
// A negative value is clamped to 0 (no retries) to prevent silent no-op
// from `range 0` producing zero loop iterations and skipping dispatch entirely.
func WithMaxRetries(n int) Option {
	return func(e *GRPCExecutor) {
		if n < 0 {
			n = 0
		}
		e.maxRetries = n
	}
}

// WithDialOptions sets additional gRPC dial options (primarily for testing with bufconn).
func WithDialOptions(opts ...grpc.DialOption) Option {
	return func(e *GRPCExecutor) {
		e.dialOpts = opts
	}
}

// WithDeadlineMode sets how per-dispatch RPC timeouts are computed.
// In "derived" mode, the timeout is computed from the step's TimeoutSeconds
// + DefaultTransportMargin, and retries share the step deadline.
// In "static" mode, the per-executor timeout is used (pre-PR 2 behavior).
// Unrecognized values are treated as "static" with a warning log at
// construction time (validated in NewGRPCExecutor).
func WithDeadlineMode(mode DeadlineMode) Option {
	return func(e *GRPCExecutor) {
		e.deadlineMode = mode
	}
}

// WithTokenParser sets the function used to extract token usage from gRPC
// errors during retry budget accounting. Primarily useful for testing —
// the default parseTokensUsed returns 0 until gRPC trailer metadata
// parsing is implemented (see PR 3a).
func WithTokenParser(parser func(error) int64) Option {
	return func(e *GRPCExecutor) {
		if parser != nil {
			e.tokenParser = parser
		}
	}
}

// WithResponseCache sets the response cache for cacheable step dispatch.
// When set, cacheable steps check the cache before gRPC dispatch and store
// results on cache miss.
func WithResponseCache(cache *cost.ResponseCache) Option {
	return func(e *GRPCExecutor) {
		e.cache = cache
	}
}

// GRPCExecutor dispatches tasks to agents via gRPC.
type GRPCExecutor struct {
	registry     registry.Registry
	logger       *zap.Logger
	timeout      time.Duration
	maxRetries   int
	dialOpts     []grpc.DialOption
	deadlineMode DeadlineMode
	tokenParser  func(error) int64
	cache        *cost.ResponseCache
}

// NewGRPCExecutor creates a new GRPCExecutor with the given registry and options.
func NewGRPCExecutor(reg registry.Registry, logger *zap.Logger, opts ...Option) *GRPCExecutor {
	if logger == nil {
		logger = zap.NewNop()
	}
	e := &GRPCExecutor{
		registry:     reg,
		logger:       logger,
		timeout:      30 * time.Second,
		maxRetries:   3,
		deadlineMode: DeadlineModeStatic,
		tokenParser:  parseTokensUsed,
	}
	for _, opt := range opts {
		opt(e)
	}

	// Validate deadline mode after all options are applied.
	// Unrecognized values fall back to static mode with a warning so that
	// typos in config/CLI flags don't silently change retry semantics.
	if e.deadlineMode != DeadlineModeDerived && e.deadlineMode != DeadlineModeStatic {
		e.logger.Warn("unrecognized deadline mode, falling back to static",
			zap.String("mode", string(e.deadlineMode)),
		)
		e.deadlineMode = DeadlineModeStatic
	}

	return e
}

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

	// Check cache for cacheable steps before any network I/O.
	if req.Cacheable && e.cache != nil {
		cacheKey := cost.CacheKey(req.AgentID, req.Payload, req.Context)
		if cached, ok := e.cache.Get(cacheKey); ok {
			e.logger.Info("cache hit, skipping gRPC dispatch",
				zap.String("agentID", req.AgentID),
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
		e.logger.Warn("MaxLLMCalls exceeds int32 range, clamping to max",
			zap.Int("original", maxLLMCalls),
			zap.Int("clamped", math.MaxInt32),
		)
		maxLLMCalls = math.MaxInt32
	}
	if maxTokens > math.MaxInt32 {
		e.logger.Warn("MaxTokens exceeds int32 range, clamping to max",
			zap.Int("original", maxTokens),
			zap.Int("clamped", math.MaxInt32),
		)
		maxTokens = math.MaxInt32
	}
	if timeoutSeconds > math.MaxInt32 {
		e.logger.Warn("TimeoutSeconds exceeds int32 range, clamping to max",
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
				e.logger.Warn("retry skipped: insufficient time budget",
					zap.String("agentID", req.AgentID),
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
				e.logger.Warn("retry skipped: insufficient token budget",
					zap.String("agentID", req.AgentID),
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
			e.logger.Info("step dispatched",
				zap.String("agentID", req.AgentID),
				zap.String("taskID", taskID),
				zap.Int("retryCount", attempt),
				zap.Int64("wallTimeMs", wallTimeMs),
			)

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

		e.logger.Warn("transient failure, retrying",
			zap.String("agentID", req.AgentID),
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
