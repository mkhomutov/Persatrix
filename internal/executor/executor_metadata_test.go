package executor

import (
	"context"
	"errors"
	"math"
	"net"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
	"github.com/mkhomutov/persatrix/internal/registry"
)

func TestExecuteTask_MetadataWallTime(t *testing.T) {
	env := setupTestEnv(t, func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
		return &taskpb.TaskResponse{
			TaskId: req.TaskId,
			Status: taskpb.TaskStatus_COMPLETED,
			Result: "ok",
		}, nil
	})

	registerHealthyAgent(t, env.reg, "test-agent")

	result, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		TaskID:  "task-wall",
		AgentID: "test-agent",
	})

	require.NoError(t, err)
	assert.GreaterOrEqual(t, result.WallTimeMs, int64(0))
	assert.Equal(t, 0, result.RetryCount)
}

func TestExecuteTask_MetadataRetryCount(t *testing.T) {
	var calls atomic.Int32
	env := setupTestEnv(t, func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
		n := calls.Add(1)
		if n <= 2 {
			return nil, status.Errorf(codes.Unavailable, "transient error %d", n)
		}
		return &taskpb.TaskResponse{
			TaskId: req.TaskId,
			Status: taskpb.TaskStatus_COMPLETED,
			Result: "ok after retries",
		}, nil
	}, WithMaxRetries(3), WithTimeout(5*time.Second))

	registerHealthyAgent(t, env.reg, "retry-agent")

	result, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		TaskID:  "task-retry",
		AgentID: "retry-agent",
	})

	require.NoError(t, err)
	assert.Equal(t, "ok after retries", result.Output)
	assert.Equal(t, 2, result.RetryCount)
	assert.Greater(t, result.WallTimeMs, int64(0))
}

func TestExecuteTask_MetadataFailure_NoMetadata(t *testing.T) {
	env := setupTestEnv(t, func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
		return &taskpb.TaskResponse{
			TaskId:       req.TaskId,
			Status:       taskpb.TaskStatus_FAILED,
			ErrorMessage: "something broke",
		}, nil
	})

	registerHealthyAgent(t, env.reg, "fail-agent")

	result, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		TaskID:  "task-fail",
		AgentID: "fail-agent",
	})

	// FAILED with no metadata: error is set, result is non-nil but has no token data.
	require.Error(t, err)
	assert.True(t, errors.Is(err, ErrTaskFailed))
	require.NotNil(t, result, "result should be non-nil so callers can attempt cost recording")
	assert.Empty(t, result.Metadata)
}

// TestExecuteTask_FailedStatus_PreservesMetadata verifies that when an agent
// responds with TaskStatus_FAILED and includes response metadata (e.g. token
// counts from the LLM call that triggered the failure), ExecuteTask returns
// a non-nil result carrying that metadata alongside the error. This enables
// the scheduler to record partial token usage for cost tracking even when a
// step fails (MT-COST-002 fix).
func TestExecuteTask_FailedStatus_PreservesMetadata(t *testing.T) {
	env := setupTestEnv(t, func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
		return &taskpb.TaskResponse{
			TaskId:       req.TaskId,
			Status:       taskpb.TaskStatus_FAILED,
			ErrorMessage: "LLM response truncated: max_tokens limit reached",
			Metadata: map[string]string{
				"input_tokens":  "120",
				"output_tokens": "50",
				"model":         "claude-sonnet",
			},
		}, nil
	})

	registerHealthyAgent(t, env.reg, "truncating-agent")

	result, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		TaskID:  "task-truncated",
		AgentID: "truncating-agent",
		Payload: "write a very long report",
	})

	require.Error(t, err)
	assert.True(t, errors.Is(err, ErrTaskFailed))
	assert.Contains(t, err.Error(), "max_tokens limit reached")

	// Metadata must be preserved so the scheduler can record token usage.
	require.NotNil(t, result, "non-nil result required for partial cost recording")
	assert.Equal(t, "120", result.Metadata["input_tokens"])
	assert.Equal(t, "50", result.Metadata["output_tokens"])
	assert.Equal(t, "claude-sonnet", result.Metadata["model"])
	assert.Empty(t, result.Output, "output should be empty on failure")
}

// TestExecuteTask_Int32OverflowGuard verifies that limit values exceeding
// math.MaxInt32 are clamped rather than silently wrapping to negative, and
// that a warning log is emitted for each clamped field. (PR 5a, F-02)
func TestExecuteTask_Int32OverflowGuard(t *testing.T) {
	// Set up zap observer to capture warning logs for clamping assertions.
	core, logs := observer.New(zap.WarnLevel)
	observedLogger := zap.New(core)

	// Construct the test env manually (instead of setupTestEnv) to inject
	// the observer logger — setupTestEnv hardcodes zap.NewNop().
	lis := bufconn.Listen(bufSize)
	srv := grpc.NewServer()

	var receivedConfig *taskpb.TaskConfig
	taskpb.RegisterAgentServiceServer(srv, &mockAgentServer{
		handler: func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			receivedConfig = req.Config
			return &taskpb.TaskResponse{
				TaskId: req.TaskId,
				Status: taskpb.TaskStatus_COMPLETED,
				Result: "clamped",
			}, nil
		},
	})

	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(func() { srv.GracefulStop(); lis.Close() })

	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, observedLogger,
		WithDialOptions(grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(ctx)
		})),
	)

	registerHealthyAgent(t, reg, "test-agent")

	_, err := exec.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "test-agent",
		Payload: "overflow test",
		Limits: StepLimits{
			MaxLLMCalls:    1<<31 + 100,  // exceeds int32 max
			MaxTokens:      1<<31 + 1000, // exceeds int32 max
			TimeoutSeconds: 1<<31 + 500,  // exceeds int32 max
		},
	})

	require.NoError(t, err)
	require.NotNil(t, receivedConfig)
	assert.Equal(t, int32(math.MaxInt32), receivedConfig.MaxLlmCalls,
		"MaxLLMCalls should be clamped to MaxInt32")
	assert.Equal(t, int32(math.MaxInt32), receivedConfig.MaxTokens,
		"MaxTokens should be clamped to MaxInt32")
	assert.Equal(t, int32(math.MaxInt32), receivedConfig.TimeoutSeconds,
		"TimeoutSeconds should be clamped to MaxInt32")

	// Verify all three clamping warnings were emitted — prevents accidental
	// removal of the diagnostic log statements without test failure.
	require.Equal(t, 3, logs.Len(), "expected exactly 3 clamping warnings")
	expectedFields := []string{"MaxLLMCalls", "MaxTokens", "TimeoutSeconds"}
	for i, entry := range logs.All() {
		assert.Equal(t, zap.WarnLevel, entry.Level)
		assert.Contains(t, entry.Message, expectedFields[i],
			"warning %d should reference %s", i, expectedFields[i])
		assert.Equal(t, int64(math.MaxInt32), entry.ContextMap()["clamped"],
			"warning %d should log clamped value as MaxInt32", i)
	}
}

// TestDerivedDeadline_TokenBudgetCutoff_TimeAllowed verifies that the token
// budget cutoff fires independently of the time budget. With a very long step
// timeout, the retry should be skipped due to token budget exhaustion, not time.
// This tests that both budget constraints are independently evaluated. (PR 5a, S7)
func TestDerivedDeadline_TokenBudgetCutoff_TimeAllowed(t *testing.T) {
	callCount := 0

	env := setupTestEnv(t,
		func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			callCount++
			return nil, status.Error(codes.Unavailable, "unavailable")
		},
		WithDeadlineMode(DeadlineModeDerived),
		WithMaxRetries(3),
		// Report 7000 tokens per attempt. After 1 failure: remaining = 8192 - 7000 = 1192.
		// minTokens = 8192 * 0.25 = 2048. 1192 < 2048 → skip retry.
		WithTokenParser(func(_ error) int64 { return 7000 }),
	)

	registerHealthyAgent(t, env.reg, "token-agent")

	_, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "token-agent",
		Payload: "time budget not the constraint",
		Limits: StepLimits{
			MaxLLMCalls:    5,
			MaxTokens:      8192,
			TimeoutSeconds: 3600, // very long — time is NOT the constraint
		},
	})

	require.Error(t, err)
	assert.Contains(t, err.Error(), "retries exhausted")
	// Should have only 1 dispatch: the initial attempt fails, token budget
	// prevents retry despite plenty of time remaining.
	assert.Equal(t, 1, callCount,
		"retry should be skipped due to token budget, not time budget")
}

// TestDerivedDeadline_MaxTokensZero verifies that MaxTokens = 0 disables
// token budget evaluation entirely. Even with a token parser reporting high
// usage, retries should proceed based on time budget only. (PR 5a, S12)
func TestDerivedDeadline_MaxTokensZero(t *testing.T) {
	callCount := 0

	env := setupTestEnv(t,
		func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
			callCount++
			if callCount <= 2 {
				return nil, status.Error(codes.Unavailable, "unavailable")
			}
			return &taskpb.TaskResponse{
				TaskId: req.TaskId,
				Status: taskpb.TaskStatus_COMPLETED,
				Result: "success after retries",
			}, nil
		},
		WithDeadlineMode(DeadlineModeDerived),
		WithMaxRetries(3),
		// Even though parser reports high tokens, MaxTokens=0 should skip the check.
		WithTokenParser(func(_ error) int64 { return 99999 }),
	)

	registerHealthyAgent(t, env.reg, "zero-tokens-agent")

	result, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "zero-tokens-agent",
		Payload: "zero max tokens test",
		Limits: StepLimits{
			MaxLLMCalls:    5,
			MaxTokens:      0,  // zero = not configured → skip token budget
			TimeoutSeconds: 60, // long enough for retries
		},
	})

	require.NoError(t, err)
	assert.Equal(t, "success after retries", result.Output)
	// All 3 calls should execute: 2 failures + 1 success.
	// Token budget check was skipped because MaxTokens = 0.
	assert.Equal(t, 3, callCount,
		"MaxTokens=0 should skip token budget evaluation, allowing retries")
}

// TestExecuteTask_WallTimeMs_Accuracy verifies that WallTimeMs accurately
// measures the dispatch duration by injecting a mock agent with a fixed delay.
// (PR 5a, N-02)
func TestExecuteTask_WallTimeMs_Accuracy(t *testing.T) {
	const agentDelay = 50 * time.Millisecond

	env := setupTestEnv(t, func(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
		time.Sleep(agentDelay)
		return &taskpb.TaskResponse{
			TaskId: req.TaskId,
			Status: taskpb.TaskStatus_COMPLETED,
			Result: "delayed",
		}, nil
	})

	registerHealthyAgent(t, env.reg, "delay-agent")

	result, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		TaskID:  "wall-time-test",
		AgentID: "delay-agent",
		Payload: "measure wall time",
	})

	require.NoError(t, err)
	assert.GreaterOrEqual(t, result.WallTimeMs, int64(agentDelay.Milliseconds()),
		"WallTimeMs should be at least the injected agent delay")
	// Upper bound: allow generous margin for CI overhead, but should be well under 5s.
	assert.Less(t, result.WallTimeMs, int64(5000),
		"WallTimeMs should not be unreasonably large")
}
