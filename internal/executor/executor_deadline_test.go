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

	"github.com/mkhomutov/persatrix/internal/defaults"
	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
)

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

// TestDerivedDeadline_ZeroTimeout_WithRetries verifies that zero-timeout steps in
// derived mode get fully static retry behavior — retries proceed without budget
// accounting. resolveDeadline returns (e.timeout, e.timeout) for zero-timeout, and
// the time budget check is skipped via the TimeoutSeconds > 0 guard so that the
// retry semantics are consistent with the static dispatch fallback.
func TestDerivedDeadline_ZeroTimeout_WithRetries(t *testing.T) {
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
		WithDeadlineMode(DeadlineModeDerived),
		WithTimeout(10*time.Second),
		WithMaxRetries(3),
	)

	registerHealthyAgent(t, env.reg, "zero-timeout-agent")

	result, err := env.executor.ExecuteTask(context.Background(), ExecuteRequest{
		AgentID: "zero-timeout-agent",
		Payload: "zero timeout with retries",
		Limits: StepLimits{
			MaxLLMCalls:    5,
			MaxTokens:      8192,
			TimeoutSeconds: 0, // zero = not configured → static fallback
		},
	})

	require.NoError(t, err)
	assert.Equal(t, "recovered", result.Output)
	// All 3 attempts should have executed: 2 transient failures + 1 success.
	// Without the TimeoutSeconds > 0 guard, the budget check would have used
	// e.timeout as stepDeadline in derived mode — potentially cutting off retries
	// inconsistently with the static dispatch fallback from resolveDeadline.
	assert.Equal(t, 3, callCount, "zero-timeout derived mode should retry like static mode")
}
