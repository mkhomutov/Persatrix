package executor

import (
	"context"
	"net"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
	"github.com/mkhomutov/persatrix/internal/registry"
)

// mockInteractionServer implements the GetClosedInteractions RPC for testing.
type mockInteractionServer struct {
	taskpb.UnimplementedAgentServiceServer
	handler func(context.Context, *taskpb.ClosedInteractionsRequest) (*taskpb.ClosedInteractionsResponse, error)
}

func (m *mockInteractionServer) GetClosedInteractions(ctx context.Context, req *taskpb.ClosedInteractionsRequest) (*taskpb.ClosedInteractionsResponse, error) {
	return m.handler(ctx, req)
}

// setupInteractionTestEnv creates a bufconn-based environment for the
// closed-interaction reader, mirroring setupChatTestEnv.
func setupInteractionTestEnv(t *testing.T, handler func(context.Context, *taskpb.ClosedInteractionsRequest) (*taskpb.ClosedInteractionsResponse, error)) (*GRPCInteractionReader, *registry.InMemoryRegistry) {
	t.Helper()

	lis := bufconn.Listen(bufSize)
	srv := grpc.NewServer()
	taskpb.RegisterAgentServiceServer(srv, &mockInteractionServer{handler: handler})

	go func() {
		_ = srv.Serve(lis)
	}()
	t.Cleanup(func() {
		srv.GracefulStop()
		lis.Close()
	})

	reg := registry.NewInMemoryRegistry(zap.NewNop())
	bufDialer := WithInteractionDialOptions(
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(ctx)
		}),
	)
	reader := NewGRPCInteractionReader(reg, zap.NewNop(), bufDialer)
	return reader, reg
}

func TestGetClosedInteractions_Success(t *testing.T) {
	var gotReq *taskpb.ClosedInteractionsRequest
	reader, reg := setupInteractionTestEnv(t, func(_ context.Context, req *taskpb.ClosedInteractionsRequest) (*taskpb.ClosedInteractionsResponse, error) {
		gotReq = req
		return &taskpb.ClosedInteractionsResponse{
			Interactions: []*taskpb.ClosedInteraction{{
				InteractionId: "i-1",
				Scope:         "group:room-7",
				CloseReason:   "cost",
				Summary:       "converged on Thursday",
				TurnCount:     4,
			}},
		}, nil
	})
	registerHealthyAgent(t, reg, "test-agent")

	resp, err := reader.GetClosedInteractions(context.Background(), "test-agent", &taskpb.ClosedInteractionsRequest{
		AgentId:  "test-agent",
		Scope:    "group:room-7",
		Limit:    20,
		MinTurns: 2,
	})

	require.NoError(t, err)
	require.Len(t, resp.GetInteractions(), 1)
	assert.Equal(t, "i-1", resp.GetInteractions()[0].GetInteractionId())
	assert.Equal(t, "cost", resp.GetInteractions()[0].GetCloseReason())
	// The request reaches the agent verbatim (scope / limit / min_turns).
	require.NotNil(t, gotReq)
	assert.Equal(t, "group:room-7", gotReq.GetScope())
	assert.Equal(t, int32(20), gotReq.GetLimit())
	assert.Equal(t, int32(2), gotReq.GetMinTurns())
}

func TestGetClosedInteractions_AgentNotFound(t *testing.T) {
	reader, _ := setupInteractionTestEnv(t, func(_ context.Context, _ *taskpb.ClosedInteractionsRequest) (*taskpb.ClosedInteractionsResponse, error) {
		return nil, nil
	})

	_, err := reader.GetClosedInteractions(context.Background(), "unknown-agent", &taskpb.ClosedInteractionsRequest{AgentId: "unknown-agent"})

	require.Error(t, err)
	assert.ErrorIs(t, err, registry.ErrAgentNotFound)
}

func TestGetClosedInteractions_AgentNotHealthy(t *testing.T) {
	reader, reg := setupInteractionTestEnv(t, func(_ context.Context, _ *taskpb.ClosedInteractionsRequest) (*taskpb.ClosedInteractionsResponse, error) {
		return nil, nil
	})
	err := reg.Register(context.Background(), registry.AgentInfo{
		ID:      "sick-agent",
		Name:    "Sick Agent",
		Address: "passthrough:///bufconn",
		Status:  registry.StatusOffline,
	})
	require.NoError(t, err)

	_, err = reader.GetClosedInteractions(context.Background(), "sick-agent", &taskpb.ClosedInteractionsRequest{AgentId: "sick-agent"})

	require.Error(t, err)
	assert.ErrorIs(t, err, ErrAgentNotReady)
}

// The reader returns the agent-side gRPC status verbatim; the REST handler
// (interactions_handler.go) is what maps it to an HTTP code. Pin that the
// status is passed through unwrapped so status.FromError keeps working.
func TestGetClosedInteractions_GRPCStatusPassthrough(t *testing.T) {
	reader, reg := setupInteractionTestEnv(t, func(_ context.Context, _ *taskpb.ClosedInteractionsRequest) (*taskpb.ClosedInteractionsResponse, error) {
		return nil, status.Error(codes.NotFound, "agent has no such interaction")
	})
	registerHealthyAgent(t, reg, "test-agent")

	_, err := reader.GetClosedInteractions(context.Background(), "test-agent", &taskpb.ClosedInteractionsRequest{AgentId: "test-agent"})

	require.Error(t, err)
	st, ok := status.FromError(err)
	require.True(t, ok)
	assert.Equal(t, codes.NotFound, st.Code())
}
