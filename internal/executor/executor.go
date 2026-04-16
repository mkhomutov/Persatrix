// Package executor handles gRPC communication with agent processes.
package executor

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"time"

	"github.com/google/uuid"
	"go.uber.org/zap"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"

	"github.com/mkhomutov/persatrix/internal/defaults"
	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
	"github.com/mkhomutov/persatrix/internal/registry"
)

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
}

// ExecuteResult contains the outcome of a task dispatch.
type ExecuteResult struct {
	TaskID   string
	Output   string
	Metadata map[string]string
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
// Unrecognized values are treated as "static" with a warning log at construction.
func WithDeadlineMode(mode DeadlineMode) Option {
	return func(e *GRPCExecutor) {
		e.deadlineMode = mode
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
	}
	for _, opt := range opts {
		opt(e)
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
	// Look up agent in registry.
	agent, err := e.registry.Get(ctx, req.AgentID)
	if err != nil {
		if errors.Is(err, registry.ErrAgentNotFound) {
			return nil, fmt.Errorf("%w: %s", ErrAgentNotFound, req.AgentID)
		}
		return nil, fmt.Errorf("registry lookup: %w", err)
	}

	// Check agent health before dialing.
	if agent.Status != registry.StatusHealthy {
		return nil, fmt.Errorf("%w: agent %s status is %s", ErrAgentNotReady, req.AgentID, agent.Status)
	}

	// Generate task ID if not provided.
	taskID := req.TaskID
	if taskID == "" {
		taskID = uuid.New().String()
	}

	// Build gRPC request.
	grpcReq := &taskpb.TaskRequest{
		TaskId:     taskID,
		WorkflowId: req.WorkflowID,
		AgentId:    req.AgentID,
		Payload:    req.Payload,
		Context:    req.Context,
		Config: &taskpb.TaskConfig{
			MaxLlmCalls:    int32(req.Limits.MaxLLMCalls),
			MaxTokens:      int32(req.Limits.MaxTokens),
			TimeoutSeconds: int32(req.Limits.TimeoutSeconds),
		},
	}

	// Compute step deadline and per-dispatch timeout based on deadline mode.
	stepDeadline, dispatchTimeout := e.resolveDeadline(req.Limits)

	// Retry loop with exponential backoff + jitter.
	// In derived mode, retries share the step deadline — elapsed time is tracked
	// and each retry gets the remaining budget.
	var lastErr error
	start := time.Now()
	var cumulativeTokens int64

	for attempt := range e.maxRetries + 1 {
		// In derived mode, check whether enough time budget remains for a retry.
		if e.deadlineMode == DeadlineModeDerived && attempt > 0 {
			elapsed := time.Since(start)
			remaining := stepDeadline - elapsed
			minBudget := time.Duration(float64(stepDeadline) * defaults.MinRetryBudgetFraction)
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
			// Update dispatch timeout to remaining time + transport margin.
			dispatchTimeout = remaining + time.Duration(defaults.DefaultTransportMargin)*time.Second
		}

		// In derived mode, check token budget before retry.
		if e.deadlineMode == DeadlineModeDerived && attempt > 0 && req.Limits.MaxTokens > 0 {
			remaining := int64(req.Limits.MaxTokens) - cumulativeTokens
			minTokens := int64(float64(req.Limits.MaxTokens) * defaults.MinRetryBudgetFraction)
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
			return result, nil
		}

		// Track token usage from failed attempts for budget accounting.
		if e.deadlineMode == DeadlineModeDerived {
			cumulativeTokens += parseTokensUsed(err)
		}

		lastErr = err

		// Don't retry on non-transient errors or if retries exhausted.
		if !isTransient(err) {
			return nil, fmt.Errorf("permanent failure dispatching to agent %s: %w", req.AgentID, err)
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

		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil, ctx.Err()
		case <-timer.C:
		}
	}

	return nil, fmt.Errorf("retries exhausted (%d attempts) for agent %s: %w", e.maxRetries+1, req.AgentID, lastErr)
}

// resolveDeadline computes the step deadline duration and the initial per-dispatch
// timeout based on the executor's deadline mode and the step's configured limits.
func (e *GRPCExecutor) resolveDeadline(limits StepLimits) (stepDeadline, dispatchTimeout time.Duration) {
	if e.deadlineMode == DeadlineModeDerived && limits.TimeoutSeconds > 0 {
		stepDeadline = time.Duration(limits.TimeoutSeconds) * time.Second
		dispatchTimeout = stepDeadline + time.Duration(defaults.DefaultTransportMargin)*time.Second
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
		return nil, fmt.Errorf("%w: %s", ErrTaskFailed, errMsg)
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
	case codes.Unavailable, codes.ResourceExhausted, codes.Aborted:
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
