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

	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
	"github.com/mkhomutov/persatrix/internal/registry"
)

// Sentinel errors for executor operations.
var (
	ErrAgentNotFound    = errors.New("agent not found in registry")
	ErrAgentNotReady    = errors.New("agent is not healthy")
	ErrTaskFailed       = errors.New("task execution failed")
	ErrUnexpectedStatus = errors.New("unexpected task status from agent")
)

// ExecuteRequest contains the parameters for a task dispatch.
type ExecuteRequest struct {
	TaskID     string
	WorkflowID string
	AgentID    string
	Payload    string
	Context    map[string]string
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

// GRPCExecutor dispatches tasks to agents via gRPC.
type GRPCExecutor struct {
	registry   registry.Registry
	logger     *zap.Logger
	timeout    time.Duration
	maxRetries int
	dialOpts   []grpc.DialOption
}

// NewGRPCExecutor creates a new GRPCExecutor with the given registry and options.
func NewGRPCExecutor(reg registry.Registry, logger *zap.Logger, opts ...Option) *GRPCExecutor {
	if logger == nil {
		logger = zap.NewNop()
	}
	e := &GRPCExecutor{
		registry:   reg,
		logger:     logger,
		timeout:    30 * time.Second,
		maxRetries: 3,
	}
	for _, opt := range opts {
		opt(e)
	}
	return e
}

// ExecuteTask dispatches a task to the specified agent via gRPC with retry logic.
// It looks up the agent in the registry, verifies health status, establishes a
// per-task gRPC connection, and sends an ExecuteTask RPC.
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
			TimeoutSeconds: int32(e.timeout.Seconds()),
		},
	}

	// Retry loop with exponential backoff + jitter.
	var lastErr error
	for attempt := range e.maxRetries + 1 {
		result, err := e.dispatch(ctx, agent.Address, grpcReq)
		if err == nil {
			return result, nil
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

// dispatch performs a single gRPC ExecuteTask call to the agent at the given address.
func (e *GRPCExecutor) dispatch(ctx context.Context, address string, req *taskpb.TaskRequest) (*ExecuteResult, error) {
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

	// Apply per-task timeout. Intentionally scoped per-dispatch (not wrapping
	// the entire retry loop) so each attempt gets a fresh timeout window.
	callCtx, cancel := context.WithTimeout(ctx, e.timeout)
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
