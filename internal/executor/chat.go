package executor

import (
	"context"
	"fmt"
	"time"

	"go.opentelemetry.io/otel/attribute"
	otelcodes "go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
	"go.uber.org/zap"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
	"github.com/mkhomutov/persatrix/internal/registry"
)

// ChatExecutor sends chat messages to agents via gRPC.
type ChatExecutor interface {
	SendChatMessage(ctx context.Context, agentID string, req *taskpb.ChatRequest) (*taskpb.ChatResponse, error)
}

// GRPCChatExecutor implements ChatExecutor using gRPC calls to agents.
type GRPCChatExecutor struct {
	registry registry.Registry
	logger   *zap.Logger
	timeout  time.Duration
	dialOpts []grpc.DialOption
}

// ChatOption configures a GRPCChatExecutor.
type ChatOption func(*GRPCChatExecutor)

// WithChatTimeout sets the default timeout for chat gRPC calls.
func WithChatTimeout(d time.Duration) ChatOption {
	return func(e *GRPCChatExecutor) {
		if d > 0 {
			e.timeout = d
		}
	}
}

// WithChatDialOptions sets additional gRPC dial options (for testing with bufconn).
func WithChatDialOptions(opts ...grpc.DialOption) ChatOption {
	return func(e *GRPCChatExecutor) {
		e.dialOpts = opts
	}
}

// NewGRPCChatExecutor creates a new GRPCChatExecutor.
func NewGRPCChatExecutor(reg registry.Registry, logger *zap.Logger, opts ...ChatOption) *GRPCChatExecutor {
	if logger == nil {
		logger = zap.NewNop()
	}
	e := &GRPCChatExecutor{
		registry: reg,
		logger:   logger,
		timeout:  30 * time.Second,
	}
	for _, opt := range opts {
		opt(e)
	}
	return e
}

// SendChatMessage looks up the agent in the registry and dispatches a gRPC
// SendChatMessage call. The caller is responsible for mapping returned gRPC
// errors to HTTP status codes.
func (e *GRPCChatExecutor) SendChatMessage(ctx context.Context, agentID string, req *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
	ctx, span := executorTracer.Start(ctx, "chat.send",
		trace.WithAttributes(
			attribute.String("persatrix.agent_id", agentID),
			attribute.String("persatrix.user_id", req.GetUserId()),
		),
	)
	defer span.End()

	agent, err := e.registry.Get(ctx, agentID)
	if err != nil {
		span.RecordError(err)
		span.SetStatus(otelcodes.Error, err.Error())
		return nil, fmt.Errorf("registry lookup: %w", err)
	}

	if agent.Status != registry.StatusHealthy {
		err := fmt.Errorf("%w: agent %s status is %s", ErrAgentNotReady, agentID, agent.Status)
		span.RecordError(err)
		span.SetStatus(otelcodes.Error, err.Error())
		return nil, err
	}

	// Build dial options: base transport credentials + caller-provided options.
	opts := make([]grpc.DialOption, 0, len(e.dialOpts)+1)
	opts = append(opts, grpc.WithTransportCredentials(insecure.NewCredentials()))
	opts = append(opts, e.dialOpts...)

	// TODO(v0.2): connection pooling — reuse connections across calls
	conn, err := grpc.NewClient(agent.Address, opts...)
	if err != nil {
		span.RecordError(err)
		span.SetStatus(otelcodes.Error, err.Error())
		return nil, fmt.Errorf("dial agent at %s: %w", agent.Address, err)
	}
	defer conn.Close()

	client := taskpb.NewAgentServiceClient(conn)

	// Use request timeout_seconds if provided, otherwise use executor default.
	timeout := e.timeout
	if req.GetTimeoutSeconds() > 0 {
		timeout = time.Duration(req.GetTimeoutSeconds()) * time.Second
	}

	callCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	resp, err := client.SendChatMessage(callCtx, req)
	if err != nil {
		e.logger.Error("SendChatMessage gRPC call failed",
			zap.String("agent_id", agentID),
			zap.String("address", agent.Address),
			zap.Error(err),
		)
		span.RecordError(err)
		span.SetStatus(otelcodes.Error, err.Error())
		return nil, err
	}

	span.SetStatus(otelcodes.Ok, "chat message sent")
	return resp, nil
}
