package integration

import (
	"context"
	"net"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap/zaptest"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"

	"github.com/persatrix/persatrix/internal/executor"
	"github.com/persatrix/persatrix/internal/generated/taskpb"
	"github.com/persatrix/persatrix/internal/planner"
	"github.com/persatrix/persatrix/internal/registry"
	"github.com/persatrix/persatrix/internal/scheduler"
	"github.com/persatrix/persatrix/internal/state"
)

const bufSize = 1024 * 1024

// mockAgentServer echoes back the agent ID and payload as output.
type mockAgentServer struct {
	taskpb.UnimplementedAgentServiceServer
}

func (m *mockAgentServer) ExecuteTask(_ context.Context, req *taskpb.TaskRequest) (*taskpb.TaskResponse, error) {
	return &taskpb.TaskResponse{
		TaskId: req.TaskId,
		Status: taskpb.TaskStatus_COMPLETED,
		Result: "output from " + req.AgentId + ": processed",
	}, nil
}

// TestSchedulerExecutorIntegration verifies the full pipeline:
// HTTP submit → scheduler poll → planner parse → executor gRPC dispatch → run completion.
// Uses real InMemoryStore, InMemoryRegistry, YAMLPlanner, and a bufconn gRPC mock server.
func TestSchedulerExecutorIntegration(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	// N-48: Use zaptest.NewLogger to surface diagnostic output on test failure.
	logger := zaptest.NewLogger(t)

	// --- Infrastructure ---
	store := state.NewInMemoryStore(logger)
	reg := registry.NewInMemoryRegistry(logger)
	plan := planner.NewYAMLPlanner(logger)

	// Start mock gRPC agent server via bufconn.
	lis := bufconn.Listen(bufSize)
	srv := grpc.NewServer()
	taskpb.RegisterAgentServiceServer(srv, &mockAgentServer{})
	go func() {
		_ = srv.Serve(lis)
	}()
	t.Cleanup(func() {
		srv.GracefulStop()
		lis.Close()
	})

	// Create executor with bufconn dialer.
	exec := executor.NewGRPCExecutor(reg, logger,
		executor.WithDialOptions(
			grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
				return lis.DialContext(ctx)
			}),
			grpc.WithTransportCredentials(insecure.NewCredentials()),
		),
		executor.WithTimeout(5*time.Second),
		executor.WithMaxRetries(1),
	)
	defer exec.Close() //nolint:errcheck

	// Register the 3 agents used by feature-builder.yaml.
	// The "revise" step reuses "code-writer".
	agents := []string{"planner", "code-writer", "code-reviewer"}
	for _, id := range agents {
		require.NoError(t, reg.Register(ctx, registry.AgentInfo{
			ID:      id,
			Name:    id,
			Address: "passthrough:///bufconn",
			Status:  registry.StatusHealthy,
		}))
	}

	// Resolve workflows dir relative to this test file → repo root/workflows/.
	_, thisFile, _, _ := runtime.Caller(0)
	workflowsDir := filepath.Join(filepath.Dir(thisFile), "..", "..", "workflows")

	// Create scheduler with fast poll interval.
	sched := scheduler.NewWorkflowScheduler(
		store, reg, plan, exec, logger, workflowsDir,
		scheduler.WithPollInterval(50*time.Millisecond),
		scheduler.WithMaxConcurrent(5),
	)

	// Start scheduler in background.
	schedCtx, schedCancel := context.WithCancel(ctx)
	defer schedCancel()
	go func() {
		_ = sched.Run(schedCtx)
	}()

	// --- Submit a workflow run ---
	run := &state.WorkflowRun{
		ID:         "integ-run-1",
		WorkflowID: "feature-builder",
		Status:     state.RunPending,
		Inputs:     map[string]string{"user_request": "build a login page"},
	}
	require.NoError(t, store.CreateRun(ctx, run))

	// --- Wait for run to complete ---
	var finalRun *state.WorkflowRun
	require.Eventually(t, func() bool {
		r, err := store.GetRun(ctx, "integ-run-1")
		if err != nil {
			return false
		}
		finalRun = r
		return r.Status == state.RunCompleted || r.Status == state.RunFailed
	}, 8*time.Second, 100*time.Millisecond, "run did not reach terminal state")

	// --- Assertions ---
	require.Equal(t, state.RunCompleted, finalRun.Status, "run should complete; error: %s", finalRun.Error)
	assert.NotNil(t, finalRun.StartedAt, "StartedAt should be set")
	assert.NotNil(t, finalRun.FinishedAt, "FinishedAt should be set")

	// Verify all 4 steps executed: plan, implement, review, revise.
	expectedSteps := []string{"plan", "implement", "review", "revise"}
	for _, stepID := range expectedSteps {
		step, ok := finalRun.Steps[stepID]
		require.True(t, ok, "step %q should exist", stepID)
		assert.Equal(t, state.RunCompleted, step.Status, "step %q should be completed", stepID)
		assert.NotEmpty(t, step.Output, "step %q should have output", stepID)
	}
}
