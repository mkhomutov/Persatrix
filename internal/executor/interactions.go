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
	"github.com/mkhomutov/persatrix/internal/observability/grpcmeta"
	"github.com/mkhomutov/persatrix/internal/registry"
)

// InteractionReader reads an agent's closed-interaction summaries over gRPC
// (v0.3.8 interaction-summary surface). Read-only: the RFC 0020 summary is
// generated agent-side at interaction close; this just surfaces it to the
// REST layer (web console + CLI).
type InteractionReader interface {
	GetClosedInteractions(ctx context.Context, agentID string, req *taskpb.ClosedInteractionsRequest) (*taskpb.ClosedInteractionsResponse, error)
}

// GRPCInteractionReader implements InteractionReader via gRPC calls to agents.
// One connection per call (no pooling), mirroring GRPCChatExecutor.
type GRPCInteractionReader struct {
	registry registry.Registry
	logger   *zap.Logger
	timeout  time.Duration
	dialOpts []grpc.DialOption
}

// InteractionReaderOption configures a GRPCInteractionReader.
type InteractionReaderOption func(*GRPCInteractionReader)

// WithInteractionDialOptions sets additional gRPC dial options (bufconn tests).
func WithInteractionDialOptions(opts ...grpc.DialOption) InteractionReaderOption {
	return func(r *GRPCInteractionReader) {
		r.dialOpts = opts
	}
}

// NewGRPCInteractionReader creates a new GRPCInteractionReader.
func NewGRPCInteractionReader(reg registry.Registry, logger *zap.Logger, opts ...InteractionReaderOption) *GRPCInteractionReader {
	if logger == nil {
		logger = zap.NewNop()
	}
	r := &GRPCInteractionReader{
		registry: reg,
		logger:   logger,
		timeout:  15 * time.Second,
	}
	for _, opt := range opts {
		opt(r)
	}
	return r
}

// GetClosedInteractions looks up the agent in the registry and dispatches a
// gRPC GetClosedInteractions call. The caller maps returned gRPC errors to
// HTTP status codes.
func (r *GRPCInteractionReader) GetClosedInteractions(ctx context.Context, agentID string, req *taskpb.ClosedInteractionsRequest) (*taskpb.ClosedInteractionsResponse, error) {
	ctx, span := executorTracer.Start(ctx, "interactions.closed.read",
		trace.WithAttributes(attribute.String("persatrix.agent_id", agentID)),
	)
	defer span.End()

	agent, err := r.registry.Get(ctx, agentID)
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

	ctx = grpcmeta.InjectIDs(ctx, grpcmeta.IDs{AgentID: agentID})

	opts := make([]grpc.DialOption, 0, len(r.dialOpts)+1)
	opts = append(opts, grpc.WithTransportCredentials(insecure.NewCredentials()))
	opts = append(opts, r.dialOpts...)
	conn, err := grpc.NewClient(agent.Address, opts...)
	if err != nil {
		span.RecordError(err)
		span.SetStatus(otelcodes.Error, err.Error())
		return nil, fmt.Errorf("dial agent at %s: %w", agent.Address, err)
	}
	defer func() {
		if cerr := conn.Close(); cerr != nil {
			r.logger.Debug("interactions: gRPC connection close returned error",
				zap.String("agent_id", agentID), zap.Error(cerr))
		}
	}()

	callCtx, cancel := context.WithTimeout(ctx, r.timeout)
	defer cancel()

	client := taskpb.NewAgentServiceClient(conn)
	resp, err := client.GetClosedInteractions(callCtx, req)
	if err != nil {
		span.RecordError(err)
		span.SetStatus(otelcodes.Error, err.Error())
		return nil, err
	}
	span.SetStatus(otelcodes.Ok, "closed interactions read")
	return resp, nil
}
