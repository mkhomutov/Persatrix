package executor

import (
	"context"
	"net"
	"testing"

	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"google.golang.org/grpc"
	"google.golang.org/grpc/test/bufconn"

	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
	"github.com/mkhomutov/persatrix/internal/registry"
)

const bufSize = 1024 * 1024

// mockAgentServer implements taskpb.AgentServiceServer for testing.
type mockAgentServer struct {
	taskpb.UnimplementedAgentServiceServer
	handler func(context.Context, *taskpb.TaskRequest) (*taskpb.TaskResponse, error)
}

func (m *mockAgentServer) ExecuteTask(ctx context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
	return m.handler(ctx, req)
}

// testEnv holds the test infrastructure: bufconn listener, gRPC server, registry, and executor.
type testEnv struct {
	lis      *bufconn.Listener
	srv      *grpc.Server
	reg      *registry.InMemoryRegistry
	executor *GRPCExecutor
}

// setupTestEnv creates a bufconn-based test environment with a mock gRPC agent server.
func setupTestEnv(t *testing.T, handler func(context.Context, *taskpb.TaskRequest) (*taskpb.TaskResponse, error), opts ...Option) *testEnv {
	t.Helper()

	lis := bufconn.Listen(bufSize)

	srv := grpc.NewServer()
	taskpb.RegisterAgentServiceServer(srv, &mockAgentServer{handler: handler})

	go func() {
		_ = srv.Serve(lis) // error expected on GracefulStop
	}()

	t.Cleanup(func() {
		srv.GracefulStop()
		lis.Close()
	})

	reg := registry.NewInMemoryRegistry(zap.NewNop())
	logger := zap.NewNop()

	// Bufconn dialer option. Transport credentials are provided by the executor
	// base (N-06 additive dial options), so only the context dialer is needed here.
	bufDialer := WithDialOptions(
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(ctx)
		}),
	)

	allOpts := append([]Option{bufDialer}, opts...)
	exec := NewGRPCExecutor(reg, logger, allOpts...)

	return &testEnv{
		lis:      lis,
		srv:      srv,
		reg:      reg,
		executor: exec,
	}
}

// registerHealthyAgent registers a healthy agent in the test registry.
// Uses passthrough:///bufconn so grpc.NewClient bypasses DNS resolution.
func registerHealthyAgent(t *testing.T, reg *registry.InMemoryRegistry, agentID string) {
	t.Helper()
	err := reg.Register(context.Background(), registry.AgentInfo{
		ID:      agentID,
		Name:    agentID,
		Address: "passthrough:///bufconn",
		Status:  registry.StatusHealthy,
	})
	require.NoError(t, err)
}
