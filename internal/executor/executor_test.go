package executor

import (
	"context"
	"errors"
	"net"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	"github.com/orchestr8/orchestr8/internal/generated/taskpb"
	"github.com/orchestr8/orchestr8/internal/registry"
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
		if err := srv.Serve(lis); err != nil {
			// Server was stopped — expected during cleanup.
		}
	}()

	t.Cleanup(func() {
		srv.GracefulStop()
		lis.Close()
	})

	reg := registry.NewInMemoryRegistry(zap.NewNop())
	logger := zap.NewNop()

	// Bufconn dialer option.
	bufDialer := WithDialOptions(
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(ctx)
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
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

func TestExecuteTask_Success(t *testing.T) {
	env := setupTestEnv(t, func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
		return &taskpb.TaskResponse{
			TaskId: req.TaskId,
			Status: taskpb.TaskStatus_COMPLETED,
			Result: "task output",
			Metadata: map[string]string{
				"tokens_used": "100",
			},
		}, nil
	})

	registerHealthyAgent(t, env.reg, "test-agent")

	result, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		TaskID:     "task-1",
		WorkflowID: "wf-1",
		AgentID:    "test-agent",
		Payload:    "do something",
	})

	require.NoError(t, err)
	assert.Equal(t, "task-1", result.TaskID)
	assert.Equal(t, "task output", result.Output)
	assert.Equal(t, "100", result.Metadata["tokens_used"])
}

func TestExecuteTask_AgentNotFound(t *testing.T) {
	env := setupTestEnv(t, nil) // handler never called

	_, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "nonexistent",
	})

	require.Error(t, err)
	assert.True(t, errors.Is(err, ErrAgentNotFound))
}

func TestExecuteTask_AgentUnhealthy(t *testing.T) {
	env := setupTestEnv(t, nil) // handler never called

	// Register agent with Offline status.
	err := env.reg.Register(context.Background(), registry.AgentInfo{
		ID:      "offline-agent",
		Name:    "offline-agent",
		Address: "passthrough:///bufconn",
		Status:  registry.StatusOffline,
	})
	require.NoError(t, err)

	_, err = env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "offline-agent",
	})

	require.Error(t, err)
	assert.True(t, errors.Is(err, ErrAgentNotReady))
}

func TestExecuteTask_AgentDegraded(t *testing.T) {
	env := setupTestEnv(t, nil) // handler never called

	// Register agent with Degraded status — should be rejected like Offline.
	err := env.reg.Register(context.Background(), registry.AgentInfo{
		ID:      "degraded-agent",
		Name:    "degraded-agent",
		Address: "passthrough:///bufconn",
		Status:  registry.StatusDegraded,
	})
	require.NoError(t, err)

	_, err = env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "degraded-agent",
	})

	require.Error(t, err)
	assert.True(t, errors.Is(err, ErrAgentNotReady))
	assert.Contains(t, err.Error(), "Degraded")
}

func TestExecuteTask_Timeout(t *testing.T) {
	env := setupTestEnv(t,
		func(ctx context.Context, _ *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			// Block until context is cancelled (timeout).
			<-ctx.Done()
			return nil, status.Error(codes.DeadlineExceeded, "deadline exceeded")
		},
		WithTimeout(100*time.Millisecond),
		WithMaxRetries(0), // no retries — we want immediate timeout
	)

	registerHealthyAgent(t, env.reg, "slow-agent")

	_, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "slow-agent",
		Payload: "slow task",
	})

	require.Error(t, err)
	// DeadlineExceeded is permanent — should not be retried.
	assert.Contains(t, err.Error(), "permanent failure")
}

func TestExecuteTask_FailedStatus(t *testing.T) {
	env := setupTestEnv(t, func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
		return &taskpb.TaskResponse{
			TaskId:       req.TaskId,
			Status:       taskpb.TaskStatus_FAILED,
			ErrorMessage: "LLM rate limit exceeded",
		}, nil
	})

	registerHealthyAgent(t, env.reg, "failing-agent")

	_, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "failing-agent",
		Payload: "fail me",
	})

	require.Error(t, err)
	assert.True(t, errors.Is(err, ErrTaskFailed))
	assert.Contains(t, err.Error(), "LLM rate limit exceeded")
}

func TestExecuteTask_FailedStatus_EmptyErrorMessage(t *testing.T) {
	env := setupTestEnv(t, func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
		return &taskpb.TaskResponse{
			TaskId: req.TaskId,
			Status: taskpb.TaskStatus_FAILED,
		}, nil
	})

	registerHealthyAgent(t, env.reg, "failing-agent")

	_, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "failing-agent",
	})

	require.Error(t, err)
	assert.True(t, errors.Is(err, ErrTaskFailed))
	assert.Contains(t, err.Error(), "unknown error")
}

func TestExecuteTask_ContextCancellation(t *testing.T) {
	callCount := 0
	env := setupTestEnv(t,
		func(ctx context.Context, _ *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			callCount++
			return nil, status.Error(codes.Unavailable, "unavailable")
		},
		WithMaxRetries(5), // many retries to ensure cancellation interrupts
	)

	registerHealthyAgent(t, env.reg, "test-agent")

	ctx, cancel := context.WithCancel(context.Background())
	// Cancel after a short delay to allow at least one attempt.
	go func() {
		time.Sleep(50 * time.Millisecond)
		cancel()
	}()

	_, err := env.executor.ExecuteTask(ctx, ExecuteRequest{
		AgentID: "test-agent",
		Payload: "cancelable task",
	})

	require.Error(t, err)
	assert.True(t, errors.Is(err, context.Canceled))
	// Should have been interrupted before exhausting all retries.
	assert.Less(t, callCount, 6)
}

func TestExecuteTask_PopulatesTaskConfig(t *testing.T) {
	var receivedConfig *taskpb.TaskConfig
	env := setupTestEnv(t, func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
		receivedConfig = req.Config
		return &taskpb.TaskResponse{
			TaskId: req.TaskId,
			Status: taskpb.TaskStatus_COMPLETED,
			Result: "done",
		}, nil
	}, WithTimeout(15*time.Second))

	registerHealthyAgent(t, env.reg, "test-agent")

	_, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "test-agent",
		Payload: "config check",
	})

	require.NoError(t, err)
	require.NotNil(t, receivedConfig, "TaskConfig should be populated in gRPC request")
	assert.Equal(t, int32(15), receivedConfig.TimeoutSeconds)
}

func TestExecuteTask_GeneratesTaskID(t *testing.T) {
	var receivedTaskID string
	env := setupTestEnv(t, func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
		receivedTaskID = req.TaskId
		return &taskpb.TaskResponse{
			TaskId: req.TaskId,
			Status: taskpb.TaskStatus_COMPLETED,
			Result: "done",
		}, nil
	})

	registerHealthyAgent(t, env.reg, "test-agent")

	result, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "test-agent",
		Payload: "no task id",
	})

	require.NoError(t, err)
	assert.NotEmpty(t, receivedTaskID)
	assert.Equal(t, receivedTaskID, result.TaskID)
}

func TestExecuteTask_PassesContext(t *testing.T) {
	var receivedContext map[string]string
	env := setupTestEnv(t, func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
		receivedContext = req.Context
		return &taskpb.TaskResponse{
			TaskId: req.TaskId,
			Status: taskpb.TaskStatus_COMPLETED,
			Result: "done",
		}, nil
	})

	registerHealthyAgent(t, env.reg, "test-agent")

	stepOutputs := map[string]string{
		"plan":   "the plan output",
		"review": "review output",
	}

	_, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "test-agent",
		Payload: "use context",
		Context: stepOutputs,
	})

	require.NoError(t, err)
	assert.Equal(t, stepOutputs, receivedContext)
}

func TestNewGRPCExecutor_Defaults(t *testing.T) {
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, nil)

	assert.Equal(t, 30*time.Second, exec.timeout)
	assert.Equal(t, 3, exec.maxRetries)
	assert.NotNil(t, exec.logger)
}

func TestNewGRPCExecutor_WithOptions(t *testing.T) {
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, zap.NewNop(),
		WithTimeout(5*time.Second),
		WithMaxRetries(1),
	)

	assert.Equal(t, 5*time.Second, exec.timeout)
	assert.Equal(t, 1, exec.maxRetries)
}

func TestGRPCExecutor_Close(t *testing.T) {
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, zap.NewNop())

	err := exec.Close()
	assert.NoError(t, err)
}

func TestExecuteTask_UnexpectedStatus(t *testing.T) {
	tests := []struct {
		name   string
		status taskpb.TaskStatus
	}{
		{"PENDING (proto default)", taskpb.TaskStatus_PENDING},
		{"RUNNING", taskpb.TaskStatus_RUNNING},
		{"CANCELLED", taskpb.TaskStatus_CANCELLED},
		{"RETRYING", taskpb.TaskStatus_RETRYING},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			env := setupTestEnv(t, func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
				return &taskpb.TaskResponse{
					TaskId: req.TaskId,
					Status: tt.status,
					Result: "should be ignored",
				}, nil
			})

			registerHealthyAgent(t, env.reg, "test-agent")

			_, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
				AgentID: "test-agent",
				Payload: "test",
			})

			require.Error(t, err)
			assert.Contains(t, err.Error(), "unexpected task status from agent")
		})
	}
}

func TestWithMaxRetries_NegativeClamped(t *testing.T) {
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, zap.NewNop(), WithMaxRetries(-1))

	assert.Equal(t, 0, exec.maxRetries)
}

func TestWithTimeout_ZeroClamped(t *testing.T) {
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, zap.NewNop(), WithTimeout(0))

	assert.Equal(t, time.Second, exec.timeout)
}

func TestWithTimeout_NegativeClamped(t *testing.T) {
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, zap.NewNop(), WithTimeout(-5*time.Second))

	assert.Equal(t, time.Second, exec.timeout)
}
