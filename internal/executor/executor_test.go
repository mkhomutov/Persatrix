package executor

import (
	"context"
	"errors"
	"fmt"
	"net"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	"github.com/mkhomutov/persatrix/internal/defaults"
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
	callCount := 0
	env := setupTestEnv(t, func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
		callCount++
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
	// FAILED is a permanent agent-side failure — must not be retried.
	assert.Equal(t, 1, callCount, "FAILED response should not be retried")
}

func TestExecuteTask_FailedStatus_EmptyErrorMessage(t *testing.T) {
	callCount := 0
	env := setupTestEnv(t, func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
		callCount++
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
	assert.Equal(t, 1, callCount, "FAILED response should not be retried")
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
		Limits: StepLimits{
			MaxLLMCalls:    5,
			MaxTokens:      8192,
			TimeoutSeconds: 15,
		},
	})

	require.NoError(t, err)
	require.NotNil(t, receivedConfig, "TaskConfig should be populated in gRPC request")
	assert.Equal(t, int32(5), receivedConfig.MaxLlmCalls)
	assert.Equal(t, int32(8192), receivedConfig.MaxTokens)
	assert.Equal(t, int32(15), receivedConfig.TimeoutSeconds)
}

func TestExecuteTask_PopulatesTaskConfig_AllFields(t *testing.T) {
	var receivedConfig *taskpb.TaskConfig
	env := setupTestEnv(t, func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
		receivedConfig = req.Config
		return &taskpb.TaskResponse{
			TaskId: req.TaskId,
			Status: taskpb.TaskStatus_COMPLETED,
			Result: "done",
		}, nil
	})

	registerHealthyAgent(t, env.reg, "test-agent")

	_, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "test-agent",
		Payload: "full config",
		Limits: StepLimits{
			MaxLLMCalls:    3,
			MaxTokens:      4096,
			TimeoutSeconds: 120,
		},
	})

	require.NoError(t, err)
	require.NotNil(t, receivedConfig)
	assert.Equal(t, int32(3), receivedConfig.MaxLlmCalls, "MaxLlmCalls should match Limits.MaxLLMCalls")
	assert.Equal(t, int32(4096), receivedConfig.MaxTokens, "MaxTokens should match Limits.MaxTokens")
	assert.Equal(t, int32(120), receivedConfig.TimeoutSeconds, "TimeoutSeconds should match Limits.TimeoutSeconds")
}

func TestExecuteTask_ZeroLimits_SentAsZero(t *testing.T) {
	// Zero limits in ExecuteRequest are sent as-is — the scheduler is responsible
	// for resolving defaults before calling the executor.
	var receivedConfig *taskpb.TaskConfig
	env := setupTestEnv(t, func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
		receivedConfig = req.Config
		return &taskpb.TaskResponse{
			TaskId: req.TaskId,
			Status: taskpb.TaskStatus_COMPLETED,
			Result: "done",
		}, nil
	})

	registerHealthyAgent(t, env.reg, "test-agent")

	_, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "test-agent",
		Payload: "zero limits",
		// Limits left as zero-value StepLimits
	})

	require.NoError(t, err)
	require.NotNil(t, receivedConfig)
	assert.Equal(t, int32(0), receivedConfig.MaxLlmCalls)
	assert.Equal(t, int32(0), receivedConfig.MaxTokens)
	assert.Equal(t, int32(0), receivedConfig.TimeoutSeconds)
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
			callCount := 0
			env := setupTestEnv(t, func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
				callCount++
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
			// Unexpected status is permanent — should not be retried.
			assert.Equal(t, 1, callCount, "unexpected status should not be retried")
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

func TestWithTimeout_FiveMinutes(t *testing.T) {
	// Validates the production config: 5-minute timeout for multi-iteration
	// LLM tool loops that exceed the default 30s.
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, zap.NewNop(), WithTimeout(5*time.Minute))

	assert.Equal(t, 5*time.Minute, exec.timeout)
}

// --- PR 2b: isTransient table-driven tests, retry edge cases ---

func TestIsTransient_TableDriven(t *testing.T) {
	tests := []struct {
		code     codes.Code
		expected bool
	}{
		{codes.OK, false},
		{codes.Canceled, false},
		{codes.Unknown, false},
		{codes.InvalidArgument, false},
		{codes.DeadlineExceeded, false},
		{codes.NotFound, false},
		{codes.AlreadyExists, false},
		{codes.PermissionDenied, false},
		{codes.ResourceExhausted, true},
		{codes.FailedPrecondition, false},
		{codes.Aborted, true},
		{codes.OutOfRange, false},
		{codes.Unimplemented, false},
		{codes.Internal, false},
		{codes.Unavailable, true},
		{codes.DataLoss, false},
		{codes.Unauthenticated, false},
	}

	for _, tt := range tests {
		t.Run(tt.code.String(), func(t *testing.T) {
			err := status.Error(tt.code, "test error")
			assert.Equal(t, tt.expected, isTransient(err),
				"isTransient(%s) should be %v", tt.code, tt.expected)
		})
	}
}

func TestIsTransient_NonGRPCError(t *testing.T) {
	// Non-gRPC errors (DNS, connection refused) should be transient.
	err := errors.New("dial tcp: connection refused")
	assert.True(t, isTransient(err))
}

func TestIsTransient_TaskFailed(t *testing.T) {
	// Application-level ErrTaskFailed should NOT be transient.
	err := fmt.Errorf("agent error: %w", ErrTaskFailed)
	assert.False(t, isTransient(err))
}

func TestIsTransient_UnexpectedStatus(t *testing.T) {
	// Application-level ErrUnexpectedStatus should NOT be transient.
	err := fmt.Errorf("bad status: %w", ErrUnexpectedStatus)
	assert.False(t, isTransient(err))
}

func TestExecuteTask_TransientRetrySuccess(t *testing.T) {
	callCount := 0
	env := setupTestEnv(t,
		func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			callCount++
			if callCount <= 2 {
				return nil, status.Error(codes.Unavailable, "temporarily unavailable")
			}
			return &taskpb.TaskResponse{
				TaskId: req.TaskId,
				Status: taskpb.TaskStatus_COMPLETED,
				Result: "recovered",
			}, nil
		},
		WithMaxRetries(3),
		WithTimeout(5*time.Second),
	)

	registerHealthyAgent(t, env.reg, "flaky-agent")

	result, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "flaky-agent",
		Payload: "will recover",
	})

	require.NoError(t, err)
	assert.Equal(t, "recovered", result.Output)
	assert.Equal(t, 3, callCount, "should succeed on 3rd attempt after 2 transient failures")
}

// TestExecuteTask_TransientThenPermanent verifies that a permanent error mid-retry
// aborts immediately without further attempts. Covers the !isTransient(err) check
// inside the retry loop (executor.go ExecuteTask).
func TestExecuteTask_TransientThenPermanent(t *testing.T) {
	callCount := 0
	env := setupTestEnv(t,
		func(_ context.Context, _ *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			callCount++
			if callCount == 1 {
				return nil, status.Error(codes.Unavailable, "temporarily unavailable")
			}
			// Second attempt returns permanent error — should stop immediately.
			return nil, status.Error(codes.InvalidArgument, "bad request")
		},
		WithMaxRetries(3),
		WithTimeout(5*time.Second),
	)

	registerHealthyAgent(t, env.reg, "mixed-agent")

	_, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "mixed-agent",
		Payload: "transient then permanent",
	})

	require.Error(t, err)
	assert.Contains(t, err.Error(), "permanent failure")
	assert.Equal(t, 2, callCount, "should stop after transient (attempt 1) + permanent (attempt 2), no 3rd attempt")
}

func TestExecuteTask_PermanentFailureNoRetry(t *testing.T) {
	callCount := 0
	env := setupTestEnv(t,
		func(_ context.Context, _ *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			callCount++
			return nil, status.Error(codes.InvalidArgument, "bad request")
		},
		WithMaxRetries(3),
	)

	registerHealthyAgent(t, env.reg, "strict-agent")

	_, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "strict-agent",
		Payload: "invalid",
	})

	require.Error(t, err)
	assert.Contains(t, err.Error(), "permanent failure")
	assert.Equal(t, 1, callCount, "permanent failure should not be retried")
}

func TestExecuteTask_RetryExhaustion(t *testing.T) {
	callCount := 0
	maxRetries := 2
	env := setupTestEnv(t,
		func(_ context.Context, _ *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			callCount++
			return nil, status.Error(codes.Unavailable, "still unavailable")
		},
		WithMaxRetries(maxRetries),
		WithTimeout(5*time.Second),
	)

	registerHealthyAgent(t, env.reg, "down-agent")

	_, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "down-agent",
		Payload: "keep failing",
	})

	require.Error(t, err)
	assert.Contains(t, err.Error(), "retries exhausted")
	assert.Contains(t, err.Error(), fmt.Sprintf("%d attempts", maxRetries+1))
	// Verify the last transient error is preserved in the wrapped error chain,
	// so callers can inspect the root cause.
	assert.Contains(t, err.Error(), "Unavailable")
	assert.Equal(t, maxRetries+1, callCount, "should attempt initial + maxRetries calls")
}

func TestExecuteTask_ResourceExhaustedRetry(t *testing.T) {
	callCount := 0
	env := setupTestEnv(t,
		func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			callCount++
			if callCount == 1 {
				return nil, status.Error(codes.ResourceExhausted, "rate limited")
			}
			return &taskpb.TaskResponse{
				TaskId: req.TaskId,
				Status: taskpb.TaskStatus_COMPLETED,
				Result: "ok after rate limit",
			}, nil
		},
		WithMaxRetries(2),
		WithTimeout(5*time.Second),
	)

	registerHealthyAgent(t, env.reg, "busy-agent")

	result, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "busy-agent",
		Payload: "rate limited",
	})

	require.NoError(t, err)
	assert.Equal(t, "ok after rate limit", result.Output)
	assert.Equal(t, 2, callCount)
}

func TestExecuteTask_AbortedRetry(t *testing.T) {
	callCount := 0
	env := setupTestEnv(t,
		func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			callCount++
			if callCount == 1 {
				return nil, status.Error(codes.Aborted, "transaction aborted")
			}
			return &taskpb.TaskResponse{
				TaskId: req.TaskId,
				Status: taskpb.TaskStatus_COMPLETED,
				Result: "ok after abort",
			}, nil
		},
		WithMaxRetries(2),
		WithTimeout(5*time.Second),
	)

	registerHealthyAgent(t, env.reg, "aborting-agent")

	result, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "aborting-agent",
		Payload: "aborted",
	})

	require.NoError(t, err)
	assert.Equal(t, "ok after abort", result.Output)
	assert.Equal(t, 2, callCount)
}

func TestExecuteTask_StatusUnknownRejected(t *testing.T) {
	env := setupTestEnv(t, nil) // handler never called

	// Register agent with zero-value status (StatusUnknown).
	err := env.reg.Register(context.Background(), registry.AgentInfo{
		ID:      "unknown-agent",
		Name:    "unknown-agent",
		Address: "passthrough:///bufconn",
		Status:  registry.StatusUnknown,
	})
	require.NoError(t, err)

	_, err = env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "unknown-agent",
		Payload: "should be rejected",
	})

	require.Error(t, err)
	assert.True(t, errors.Is(err, ErrAgentNotReady))
	assert.Contains(t, err.Error(), "Unknown")
}

func TestExecuteTask_ConcurrentDispatch(t *testing.T) {
	env := setupTestEnv(t,
		func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			return &taskpb.TaskResponse{
				TaskId: req.TaskId,
				Status: taskpb.TaskStatus_COMPLETED,
				Result: "concurrent-" + req.TaskId,
			}, nil
		},
		WithTimeout(5*time.Second),
		WithMaxRetries(0),
	)

	registerHealthyAgent(t, env.reg, "concurrent-agent")

	const numGoroutines = 10
	var wg sync.WaitGroup
	results := make([]*ExecuteResult, numGoroutines)
	errs := make([]error, numGoroutines)

	for i := range numGoroutines {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			results[idx], errs[idx] = env.executor.ExecuteTask(
				context.Background(),
				ExecuteRequest{
					TaskID:  fmt.Sprintf("task-%d", idx),
					AgentID: "concurrent-agent",
					Payload: fmt.Sprintf("payload-%d", idx),
				},
			)
		}(i)
	}

	wg.Wait()

	for i := range numGoroutines {
		require.NoError(t, errs[i], "goroutine %d should not error", i)
		assert.Equal(t, fmt.Sprintf("concurrent-task-%d", i), results[i].Output)
	}
}

// --- PR 6: Executor Hardening (N-06, N-12, N-13) ---

// TestExecuteTask_ContextCancellationMidDispatch verifies behavior when the context
// is cancelled during an active gRPC dispatch (not during backoff sleep). The gRPC
// layer returns codes.Canceled which isTransient classifies as permanent. This test
// documents that the error surfaces as "permanent failure ... Canceled" rather than
// a bare context.Canceled. (N-12)
func TestExecuteTask_ContextCancellationMidDispatch(t *testing.T) {
	var callCount int32
	env := setupTestEnv(t,
		func(ctx context.Context, _ *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			atomic.AddInt32(&callCount, 1)
			// Block until the context is cancelled — simulates a slow agent.
			<-ctx.Done()
			return nil, status.Error(codes.Canceled, "context canceled")
		},
		WithMaxRetries(3),
		WithTimeout(5*time.Second),
	)

	registerHealthyAgent(t, env.reg, "slow-agent")

	ctx, cancel := context.WithCancel(context.Background())
	// Cancel after a brief delay so the dispatch is in-flight.
	go func() {
		time.Sleep(50 * time.Millisecond)
		cancel()
	}()

	_, err := env.executor.ExecuteTask(ctx, ExecuteRequest{
		AgentID: "slow-agent",
		Payload: "will be cancelled mid-dispatch",
	})

	require.Error(t, err)
	// codes.Canceled is classified as permanent by isTransient, so the error
	// surfaces as "permanent failure" rather than context.Canceled.
	assert.Contains(t, err.Error(), "permanent failure")
	assert.Equal(t, int32(1), atomic.LoadInt32(&callCount), "should not retry after codes.Canceled")
}

// TestExecuteTask_ConcurrentRetryStress exercises the backoff/retry path under the
// race detector by having multiple goroutines all encounter transient errors before
// eventually succeeding. This validates that the per-dispatch timer/select/backoff
// is safe under concurrent use. (N-13)
func TestExecuteTask_ConcurrentRetryStress(t *testing.T) {
	var totalAttempts sync.Map // goroutine index → attempt count

	env := setupTestEnv(t,
		func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			// Track per-task attempts via the totalAttempts map.
			key := req.TaskId
			val, _ := totalAttempts.LoadOrStore(key, new(int32))
			count := val.(*int32)
			attempt := atomic.AddInt32(count, 1)

			// First attempt always fails with transient error.
			if attempt <= 1 {
				return nil, status.Error(codes.Unavailable, "temporarily unavailable")
			}
			return &taskpb.TaskResponse{
				TaskId: req.TaskId,
				Status: taskpb.TaskStatus_COMPLETED,
				Result: "recovered-" + req.TaskId,
			}, nil
		},
		WithMaxRetries(3),
		WithTimeout(5*time.Second),
	)

	registerHealthyAgent(t, env.reg, "retry-agent")

	const numGoroutines = 8
	var wg sync.WaitGroup
	results := make([]*ExecuteResult, numGoroutines)
	errs := make([]error, numGoroutines)

	for i := range numGoroutines {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			results[idx], errs[idx] = env.executor.ExecuteTask(
				context.Background(),
				ExecuteRequest{
					TaskID:  fmt.Sprintf("stress-%d", idx),
					AgentID: "retry-agent",
					Payload: fmt.Sprintf("stress-payload-%d", idx),
				},
			)
		}(i)
	}

	wg.Wait()

	for i := range numGoroutines {
		require.NoError(t, errs[i], "goroutine %d should not error", i)
		assert.Equal(t, fmt.Sprintf("recovered-stress-%d", i), results[i].Output)

		// Verify each goroutine retried at least once.
		key := fmt.Sprintf("stress-%d", i)
		val, ok := totalAttempts.Load(key)
		require.True(t, ok, "attempt counter should exist for %s", key)
		assert.Equal(t, int32(2), *val.(*int32), "goroutine %d should have made exactly 2 attempts", i)
	}
}

// TestDialOptions_Additive verifies that caller-provided dial options are additive
// with the base transport credentials, not a replacement. (N-06)
func TestDialOptions_Additive(t *testing.T) {
	// This test verifies the fix works end-to-end: even when WithDialOptions is
	// used (as in setupTestEnv for bufconn), transport credentials are still
	// included by the executor. If they weren't, gRPC would refuse to dial.
	env := setupTestEnv(t,
		func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			return &taskpb.TaskResponse{
				TaskId: req.TaskId,
				Status: taskpb.TaskStatus_COMPLETED,
				Result: "additive-ok",
			}, nil
		},
	)

	registerHealthyAgent(t, env.reg, "additive-agent")

	result, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "additive-agent",
		Payload: "verify additive dial options",
	})

	require.NoError(t, err)
	assert.Equal(t, "additive-ok", result.Output)
}

// --- PR 2: Derived deadline, shared retry budget, minimum budget cutoff ---

func TestDerivedDeadline_RPCTimeoutFromStepTimeout(t *testing.T) {
	// In derived mode, the RPC timeout should be step.TimeoutSeconds + transport margin.
	// A step with 60s timeout → RPC timeout of 65s (60 + DefaultTransportMargin).
	var callDeadlineOK bool
	env := setupTestEnv(t,
		func(ctx context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			deadline, ok := ctx.Deadline()
			if ok {
				remaining := time.Until(deadline)
				// Should be close to 65s (60s step + 5s transport margin).
				// Allow generous tolerance for test scheduling variance.
				expected := time.Duration(60+defaults.DefaultTransportMargin) * time.Second
				callDeadlineOK = remaining > expected-2*time.Second && remaining <= expected+time.Second
			}
			return &taskpb.TaskResponse{
				TaskId: req.TaskId,
				Status: taskpb.TaskStatus_COMPLETED,
				Result: "done",
			}, nil
		},
		WithDeadlineMode(DeadlineModeDerived),
		WithMaxRetries(0),
	)

	registerHealthyAgent(t, env.reg, "test-agent")

	result, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "test-agent",
		Payload: "derived deadline test",
		Limits: StepLimits{
			MaxLLMCalls:    5,
			MaxTokens:      8192,
			TimeoutSeconds: 60,
		},
	})

	require.NoError(t, err)
	assert.Equal(t, "done", result.Output)
	assert.True(t, callDeadlineOK, "RPC deadline should be ~65s (step 60s + transport margin 5s)")
}

func TestDerivedDeadline_SharedRetryBudget(t *testing.T) {
	// First attempt takes most of the step deadline. Second attempt should get
	// only the remaining time.
	var secondCallDeadline time.Duration
	callCount := 0
	stepTimeout := 2 // 2 seconds for fast test

	env := setupTestEnv(t,
		func(ctx context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			callCount++
			if callCount == 1 {
				// Simulate a slow first attempt that consumes ~1s of the 2s budget.
				time.Sleep(time.Duration(float64(stepTimeout)*0.5) * time.Second)
				return nil, status.Error(codes.Unavailable, "temporarily unavailable")
			}
			// Second call: check remaining deadline.
			deadline, ok := ctx.Deadline()
			if ok {
				secondCallDeadline = time.Until(deadline)
			}
			return &taskpb.TaskResponse{
				TaskId: req.TaskId,
				Status: taskpb.TaskStatus_COMPLETED,
				Result: "recovered",
			}, nil
		},
		WithDeadlineMode(DeadlineModeDerived),
		WithMaxRetries(2),
	)

	registerHealthyAgent(t, env.reg, "retry-agent")

	result, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "retry-agent",
		Payload: "shared deadline test",
		Limits: StepLimits{
			MaxLLMCalls:    5,
			MaxTokens:      8192,
			TimeoutSeconds: stepTimeout,
		},
	})

	require.NoError(t, err)
	assert.Equal(t, "recovered", result.Output)
	assert.Equal(t, 2, callCount)
	// Second call should have less time than the original deadline + margin.
	fullDeadline := time.Duration(stepTimeout+defaults.DefaultTransportMargin) * time.Second
	assert.Less(t, secondCallDeadline, fullDeadline,
		"second attempt should have less time than original deadline")
}

func TestDerivedDeadline_MinimumBudgetCutoff(t *testing.T) {
	// If less than MinRetryBudgetFraction of time remains, retry should be skipped.
	callCount := 0
	stepTimeout := 1 // 1 second

	env := setupTestEnv(t,
		func(ctx context.Context, _ *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			callCount++
			// Consume most of the time budget on first attempt.
			time.Sleep(850 * time.Millisecond)
			return nil, status.Error(codes.Unavailable, "unavailable")
		},
		WithDeadlineMode(DeadlineModeDerived),
		WithMaxRetries(3),
	)

	registerHealthyAgent(t, env.reg, "slow-agent")

	_, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "slow-agent",
		Payload: "budget cutoff test",
		Limits: StepLimits{
			MaxLLMCalls:    5,
			MaxTokens:      8192,
			TimeoutSeconds: stepTimeout,
		},
	})

	require.Error(t, err)
	assert.Contains(t, err.Error(), "retries exhausted")
	// Should have done 1 attempt, then skipped retries due to insufficient budget.
	assert.Equal(t, 1, callCount, "should skip retry when <25%% of time budget remains")
}

func TestDerivedDeadline_TokenBudgetCutoff(t *testing.T) {
	// Verify that parseTokensUsed returns 0 for transport errors (current behavior)
	// and the executor still respects the token budget check path.
	assert.Equal(t, int64(0), parseTokensUsed(errors.New("some error")))
	assert.Equal(t, int64(0), parseTokensUsed(status.Error(codes.Unavailable, "unavailable")))
}

// TestDerivedDeadline_TokenBudgetCutoff_WithInjectedParser exercises the actual
// token budget cutoff logic by injecting a tokenParser that reports high token
// usage. When cumulative tokens exceed (1 - MinRetryBudgetFraction) of MaxTokens,
// the retry should be skipped.
func TestDerivedDeadline_TokenBudgetCutoff_WithInjectedParser(t *testing.T) {
	callCount := 0
	maxTokens := 8192

	// Inject a tokenParser via WithTokenParser that reports 7000 tokens per
	// failed attempt. After 1 attempt: cumulativeTokens = 7000, remaining = 1192.
	// minTokens = 8192 * 0.25 = 2048. remaining (1192) < minTokens (2048) → skip.
	env := setupTestEnv(t,
		func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			callCount++
			return nil, status.Error(codes.Unavailable, "unavailable")
		},
		WithDeadlineMode(DeadlineModeDerived),
		WithMaxRetries(3),
		WithTokenParser(func(_ error) int64 {
			return 7000
		}),
	)

	registerHealthyAgent(t, env.reg, "token-agent")

	_, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "token-agent",
		Payload: "token budget test",
		Limits: StepLimits{
			MaxLLMCalls:    5,
			MaxTokens:      maxTokens,
			TimeoutSeconds: 60, // long enough that time budget is not the constraint
		},
	})

	require.Error(t, err)
	assert.Contains(t, err.Error(), "retries exhausted")
	assert.Equal(t, 1, callCount, "should skip retry when token budget is insufficient")
}

func TestStaticMode_PreservesOriginalBehavior(t *testing.T) {
	// In static mode, each dispatch gets the per-executor timeout, not derived.
	var callDeadline time.Duration
	env := setupTestEnv(t,
		func(ctx context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			deadline, ok := ctx.Deadline()
			if ok {
				callDeadline = time.Until(deadline)
			}
			return &taskpb.TaskResponse{
				TaskId: req.TaskId,
				Status: taskpb.TaskStatus_COMPLETED,
				Result: "static",
			}, nil
		},
		WithDeadlineMode(DeadlineModeStatic),
		WithTimeout(10*time.Second),
		WithMaxRetries(0),
	)

	registerHealthyAgent(t, env.reg, "test-agent")

	result, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "test-agent",
		Payload: "static mode test",
		Limits: StepLimits{
			MaxLLMCalls:    5,
			MaxTokens:      8192,
			TimeoutSeconds: 60, // this should be ignored in static mode
		},
	})

	require.NoError(t, err)
	assert.Equal(t, "static", result.Output)
	// Deadline should be close to the executor's static timeout (10s), not 65s.
	assert.Less(t, callDeadline, 12*time.Second, "static mode should use executor timeout")
	assert.Greater(t, callDeadline, 8*time.Second, "static mode should use executor timeout")
}

func TestDerivedDeadline_ZeroTimeoutFallsBackToStatic(t *testing.T) {
	// If step TimeoutSeconds is 0, derived mode falls back to per-executor timeout.
	var callDeadline time.Duration
	env := setupTestEnv(t,
		func(ctx context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			deadline, ok := ctx.Deadline()
			if ok {
				callDeadline = time.Until(deadline)
			}
			return &taskpb.TaskResponse{
				TaskId: req.TaskId,
				Status: taskpb.TaskStatus_COMPLETED,
				Result: "fallback",
			}, nil
		},
		WithDeadlineMode(DeadlineModeDerived),
		WithTimeout(10*time.Second),
		WithMaxRetries(0),
	)

	registerHealthyAgent(t, env.reg, "test-agent")

	_, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "test-agent",
		Payload: "zero timeout",
		Limits: StepLimits{
			MaxLLMCalls:    5,
			MaxTokens:      8192,
			TimeoutSeconds: 0, // zero = not configured
		},
	})

	require.NoError(t, err)
	// Should fall back to the static 10s timeout.
	assert.Less(t, callDeadline, 12*time.Second)
	assert.Greater(t, callDeadline, 8*time.Second)
}

func TestDerivedDeadline_DeadlineExceeded_NoRetry(t *testing.T) {
	// DeadlineExceeded from gRPC is permanent — should not be retried even in
	// derived mode (existing behavior preserved).
	var callCount int32
	env := setupTestEnv(t,
		func(ctx context.Context, _ *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			atomic.AddInt32(&callCount, 1)
			<-ctx.Done()
			return nil, status.Error(codes.DeadlineExceeded, "deadline exceeded")
		},
		WithDeadlineMode(DeadlineModeDerived),
		WithTimeout(100*time.Millisecond), // fallback for zero-timeout path
		WithMaxRetries(3),
	)

	registerHealthyAgent(t, env.reg, "slow-agent")

	_, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "slow-agent",
		Payload: "will timeout",
		Limits: StepLimits{
			TimeoutSeconds: 1, // 1s step timeout → 6s RPC timeout
		},
	})

	require.Error(t, err)
	assert.Contains(t, err.Error(), "permanent failure")
	assert.Equal(t, int32(1), atomic.LoadInt32(&callCount), "DeadlineExceeded should not be retried")
}

func TestResolveDeadline_Derived(t *testing.T) {
	e := &GRPCExecutor{
		logger:       zap.NewNop(),
		timeout:      30 * time.Second,
		deadlineMode: DeadlineModeDerived,
	}

	stepDeadline, dispatchTimeout := e.resolveDeadline(StepLimits{TimeoutSeconds: 60})

	assert.Equal(t, 60*time.Second, stepDeadline)
	expectedTimeout := time.Duration(60+defaults.DefaultTransportMargin) * time.Second
	assert.Equal(t, expectedTimeout, dispatchTimeout)
}

func TestResolveDeadline_Static(t *testing.T) {
	e := &GRPCExecutor{
		timeout:      5 * time.Minute,
		deadlineMode: DeadlineModeStatic,
	}

	stepDeadline, dispatchTimeout := e.resolveDeadline(StepLimits{TimeoutSeconds: 60})

	assert.Equal(t, 5*time.Minute, stepDeadline)
	assert.Equal(t, 5*time.Minute, dispatchTimeout)
}

func TestResolveDeadline_DerivedZeroTimeout(t *testing.T) {
	e := &GRPCExecutor{
		logger:       zap.NewNop(),
		timeout:      30 * time.Second,
		deadlineMode: DeadlineModeDerived,
	}

	stepDeadline, dispatchTimeout := e.resolveDeadline(StepLimits{TimeoutSeconds: 0})

	// Zero timeout falls back to static behavior.
	assert.Equal(t, 30*time.Second, stepDeadline)
	assert.Equal(t, 30*time.Second, dispatchTimeout)
}

func TestWithDeadlineMode(t *testing.T) {
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, zap.NewNop(), WithDeadlineMode(DeadlineModeDerived))
	assert.Equal(t, DeadlineModeDerived, exec.deadlineMode)
}

func TestNewGRPCExecutor_DefaultsToStaticMode(t *testing.T) {
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, zap.NewNop())
	assert.Equal(t, DeadlineModeStatic, exec.deadlineMode)
}

// TestNewGRPCExecutor_UnrecognizedDeadlineMode verifies that an unrecognized
// deadline mode string falls back to static mode with a warning log, matching
// the documented contract on WithDeadlineMode.
func TestNewGRPCExecutor_UnrecognizedDeadlineMode(t *testing.T) {
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, zap.NewNop(), WithDeadlineMode("invalid"))

	assert.Equal(t, DeadlineModeStatic, exec.deadlineMode,
		"unrecognized mode should fall back to static")
}

// TestNewGRPCExecutor_UnrecognizedDeadlineMode_BehavesAsStatic verifies end-to-end
// that an executor constructed with an unrecognized mode dispatches with static
// timeout semantics (uses the per-executor timeout, not step-derived deadline).
func TestNewGRPCExecutor_UnrecognizedDeadlineMode_BehavesAsStatic(t *testing.T) {
	var callDeadline time.Duration
	env := setupTestEnv(t,
		func(ctx context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			deadline, ok := ctx.Deadline()
			if ok {
				callDeadline = time.Until(deadline)
			}
			return &taskpb.TaskResponse{
				TaskId: req.TaskId,
				Status: taskpb.TaskStatus_COMPLETED,
				Result: "ok",
			}, nil
		},
		WithDeadlineMode("typo-derived"),
		WithTimeout(10*time.Second),
		WithMaxRetries(0),
	)

	registerHealthyAgent(t, env.reg, "test-agent")

	result, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "test-agent",
		Payload: "unrecognized mode test",
		Limits: StepLimits{
			TimeoutSeconds: 60, // would give 65s in derived mode
		},
	})

	require.NoError(t, err)
	assert.Equal(t, "ok", result.Output)
	// Should use static 10s timeout, not derived 65s.
	assert.Less(t, callDeadline, 12*time.Second, "unrecognized mode should behave as static")
	assert.Greater(t, callDeadline, 8*time.Second, "unrecognized mode should behave as static")
}

// TestDerivedDeadline_ConcurrentDispatch validates that derived-mode deadline
// computation is goroutine-safe under the race detector. Each goroutine tracks
// its own `time.Since(start)` for the shared retry budget — this test ensures
// no accidental state sharing across concurrent dispatches.
func TestDerivedDeadline_ConcurrentDispatch(t *testing.T) {
	env := setupTestEnv(t,
		func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			return &taskpb.TaskResponse{
				TaskId: req.TaskId,
				Status: taskpb.TaskStatus_COMPLETED,
				Result: "derived-concurrent-" + req.TaskId,
			}, nil
		},
		WithDeadlineMode(DeadlineModeDerived),
		WithTimeout(5*time.Second),
		WithMaxRetries(1),
	)

	registerHealthyAgent(t, env.reg, "concurrent-agent")

	const numGoroutines = 10
	var wg sync.WaitGroup
	results := make([]*ExecuteResult, numGoroutines)
	errs := make([]error, numGoroutines)

	for i := range numGoroutines {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			results[idx], errs[idx] = env.executor.ExecuteTask(
				context.Background(),
				ExecuteRequest{
					TaskID:  fmt.Sprintf("derived-task-%d", idx),
					AgentID: "concurrent-agent",
					Payload: fmt.Sprintf("payload-%d", idx),
					Limits: StepLimits{
						MaxLLMCalls:    5,
						MaxTokens:      8192,
						TimeoutSeconds: 30,
					},
				},
			)
		}(i)
	}

	wg.Wait()

	for i := range numGoroutines {
		require.NoError(t, errs[i], "goroutine %d should not error", i)
		assert.Equal(t, fmt.Sprintf("derived-concurrent-derived-task-%d", i), results[i].Output)
	}
}
