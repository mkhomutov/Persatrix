// Package executor handles gRPC communication with agent processes.
package executor

import (
	"context"
	"errors"
	"time"

	"go.uber.org/zap"
	"google.golang.org/grpc"

	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/security"
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
//
// ExecutionID and StepID are the workflow run and per-step identifiers
// (RFC 0018 § D); they are propagated to the agent process as gRPC
// metadata so structured-log records on both sides of the boundary can
// be correlated.  Both default to empty when omitted (chat dispatch
// has no step concept; ad-hoc tests may not have an execution).
type ExecuteRequest struct {
	TaskID      string
	ExecutionID string
	StepID      string
	WorkflowID  string
	AgentID     string
	Payload     string
	Context     map[string]string
	Limits      StepLimits
	Cacheable   bool
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

// WithAuditLogger injects the security audit logger used to emit
// `tool.invoked` (telemetry-class, batched) for every successful dispatch.
// Nil-safe: when unset, the executor skips audit emit so callers that do
// not opt into RFC 0009 audit retain prior behaviour. (RFC 0009 PR 1b.)
func WithAuditLogger(a security.AuditLogger) Option {
	return func(e *GRPCExecutor) {
		e.auditor = a
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
	auditor      security.AuditLogger
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
