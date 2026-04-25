package executor

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
	"github.com/mkhomutov/persatrix/internal/registry"
)

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

// TestWithTokenParser_Nil verifies that passing nil to WithTokenParser preserves the
// default parseTokensUsed function rather than causing a nil-pointer panic on retry.
func TestWithTokenParser_Nil(t *testing.T) {
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, zap.NewNop(),
		WithDeadlineMode(DeadlineModeDerived),
		WithTokenParser(nil),
	)

	// The nil guard in WithTokenParser should have preserved the default parser.
	assert.NotNil(t, exec.tokenParser, "WithTokenParser(nil) should preserve default parser")

	// Verify the default parser returns 0 (not a panic).
	result := exec.tokenParser(errors.New("test error"))
	assert.Equal(t, int64(0), result, "default parser should return 0")
}
