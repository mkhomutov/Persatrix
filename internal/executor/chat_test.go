package executor

import (
	"context"
	"net"
	"testing"
	"time"

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

// mockChatServer implements the SendChatMessage RPC for testing.
type mockChatServer struct {
	taskpb.UnimplementedAgentServiceServer
	handler func(context.Context, *taskpb.ChatRequest) (*taskpb.ChatResponse, error)
}

func (m *mockChatServer) SendChatMessage(ctx context.Context, req *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
	return m.handler(ctx, req)
}

// setupChatTestEnv creates a bufconn-based test environment for chat executor tests.
func setupChatTestEnv(t *testing.T, handler func(context.Context, *taskpb.ChatRequest) (*taskpb.ChatResponse, error), opts ...ChatOption) (*GRPCChatExecutor, *registry.InMemoryRegistry) {
	t.Helper()

	lis := bufconn.Listen(bufSize)
	srv := grpc.NewServer()
	taskpb.RegisterAgentServiceServer(srv, &mockChatServer{handler: handler})

	go func() {
		_ = srv.Serve(lis)
	}()
	t.Cleanup(func() {
		srv.GracefulStop()
		lis.Close()
	})

	reg := registry.NewInMemoryRegistry(zap.NewNop())

	bufDialer := WithChatDialOptions(
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(ctx)
		}),
	)

	allOpts := append([]ChatOption{bufDialer}, opts...)
	exec := NewGRPCChatExecutor(reg, zap.NewNop(), allOpts...)

	return exec, reg
}

func TestSendChatMessage_Success(t *testing.T) {
	exec, reg := setupChatTestEnv(t, func(_ context.Context, req *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
		return &taskpb.ChatResponse{
			Reply:       "Hello, human!",
			SessionId:   "sess-123",
			AgentId:     req.AgentId,
			Timestamp:   1713600000,
			ReplyStatus: "ok",
		}, nil
	})

	registerHealthyAgent(t, reg, "test-agent")

	resp, err := exec.SendChatMessage(context.Background(), "test-agent", &taskpb.ChatRequest{
		AgentId: "test-agent",
		UserId:  "local",
		Message: "Hi there",
	})

	require.NoError(t, err)
	assert.Equal(t, "Hello, human!", resp.Reply)
	assert.Equal(t, "sess-123", resp.SessionId)
	assert.Equal(t, "test-agent", resp.AgentId)
	assert.Equal(t, "ok", resp.ReplyStatus)
}

func TestSendChatMessage_AgentNotFound(t *testing.T) {
	exec, _ := setupChatTestEnv(t, func(_ context.Context, _ *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
		return nil, nil
	})

	_, err := exec.SendChatMessage(context.Background(), "unknown-agent", &taskpb.ChatRequest{
		AgentId: "unknown-agent",
		Message: "hello",
	})

	require.Error(t, err)
	assert.ErrorIs(t, err, registry.ErrAgentNotFound)
}

func TestSendChatMessage_AgentNotHealthy(t *testing.T) {
	exec, reg := setupChatTestEnv(t, func(_ context.Context, _ *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
		return nil, nil
	})

	err := reg.Register(context.Background(), registry.AgentInfo{
		ID:      "sick-agent",
		Name:    "Sick Agent",
		Address: "passthrough:///bufconn",
		Status:  registry.StatusOffline,
	})
	require.NoError(t, err)

	_, err = exec.SendChatMessage(context.Background(), "sick-agent", &taskpb.ChatRequest{
		AgentId: "sick-agent",
		Message: "hello",
	})

	require.Error(t, err)
	assert.ErrorIs(t, err, ErrAgentNotReady)
}

func TestSendChatMessage_GRPCInternalError(t *testing.T) {
	exec, reg := setupChatTestEnv(t, func(_ context.Context, _ *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
		return nil, status.Error(codes.Internal, "agent crashed")
	})

	registerHealthyAgent(t, reg, "crash-agent")

	_, err := exec.SendChatMessage(context.Background(), "crash-agent", &taskpb.ChatRequest{
		AgentId: "crash-agent",
		Message: "hello",
	})

	require.Error(t, err)
	st, ok := status.FromError(err)
	require.True(t, ok)
	assert.Equal(t, codes.Internal, st.Code())
}

func TestSendChatMessage_GRPCDeadlineExceeded(t *testing.T) {
	exec, reg := setupChatTestEnv(t, func(ctx context.Context, _ *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
		// Block until context is cancelled (simulates timeout).
		<-ctx.Done()
		return nil, status.Error(codes.DeadlineExceeded, "deadline exceeded")
	}, WithChatTimeout(100*time.Millisecond))

	registerHealthyAgent(t, reg, "slow-agent")

	_, err := exec.SendChatMessage(context.Background(), "slow-agent", &taskpb.ChatRequest{
		AgentId: "slow-agent",
		Message: "hello",
	})

	require.Error(t, err)
	st, ok := status.FromError(err)
	require.True(t, ok)
	assert.Equal(t, codes.DeadlineExceeded, st.Code())
}

func TestSendChatMessage_RequestTimeoutOverridesDefault(t *testing.T) {
	called := make(chan time.Duration, 1)
	exec, reg := setupChatTestEnv(t, func(ctx context.Context, _ *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
		deadline, ok := ctx.Deadline()
		if ok {
			called <- time.Until(deadline)
		}
		return &taskpb.ChatResponse{ReplyStatus: "ok"}, nil
	}, WithChatTimeout(60*time.Second))

	registerHealthyAgent(t, reg, "test-agent")

	_, err := exec.SendChatMessage(context.Background(), "test-agent", &taskpb.ChatRequest{
		AgentId:        "test-agent",
		Message:        "hello",
		TimeoutSeconds: 5,
	})
	require.NoError(t, err)

	timeout := <-called
	// Should be close to 5 seconds, not 60 seconds.
	assert.Less(t, timeout, 10*time.Second)
}

func TestSendChatMessage_EmptyReply(t *testing.T) {
	exec, reg := setupChatTestEnv(t, func(_ context.Context, _ *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
		return &taskpb.ChatResponse{
			Reply:       "",
			ReplyStatus: "empty",
			SessionId:   "sess-456",
		}, nil
	})

	registerHealthyAgent(t, reg, "quiet-agent")

	resp, err := exec.SendChatMessage(context.Background(), "quiet-agent", &taskpb.ChatRequest{
		AgentId: "quiet-agent",
		Message: "hello",
	})

	require.NoError(t, err)
	assert.Equal(t, "", resp.Reply)
	assert.Equal(t, "empty", resp.ReplyStatus)
}

func TestNewGRPCChatExecutor_NilLogger(t *testing.T) {
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCChatExecutor(reg, nil)
	assert.NotNil(t, exec)
	assert.NotNil(t, exec.logger)
}

// TestSendChatMessage_TimeoutCappedAtMax verifies that request timeout_seconds
// exceeding chatMaxTimeoutSeconds is clamped to the maximum, preventing resource
// exhaustion from malicious or buggy clients. (PR #123 review finding F-03)
func TestSendChatMessage_TimeoutCappedAtMax(t *testing.T) {
	called := make(chan time.Duration, 1)
	exec, reg := setupChatTestEnv(t, func(ctx context.Context, _ *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
		deadline, ok := ctx.Deadline()
		if ok {
			called <- time.Until(deadline)
		}
		return &taskpb.ChatResponse{ReplyStatus: "ok"}, nil
	}, WithChatTimeout(10*time.Second))

	registerHealthyAgent(t, reg, "test-agent")

	// Request 999999 seconds — should be capped to chatMaxTimeoutSeconds (300s).
	_, err := exec.SendChatMessage(context.Background(), "test-agent", &taskpb.ChatRequest{
		AgentId:        "test-agent",
		Message:        "hello",
		TimeoutSeconds: 999999,
	})
	require.NoError(t, err)

	timeout := <-called
	// Should be close to 300 seconds (chatMaxTimeoutSeconds), not 999999.
	assert.LessOrEqual(t, timeout, time.Duration(chatMaxTimeoutSeconds+5)*time.Second)
	assert.Greater(t, timeout, 200*time.Second)
}
