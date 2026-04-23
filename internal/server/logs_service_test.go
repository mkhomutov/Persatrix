// Tests for the LogService gRPC server (RFC 0018 PR 5).
package server

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
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/mkhomutov/persatrix/internal/generated/logpb"
	"github.com/mkhomutov/persatrix/internal/observability/logbuffer"
)

// startBufconnLogService stands up an in-process gRPC server backed by
// a real Buffer + bufconn listener.  Returns a connected client + the
// underlying buffer so tests can assert admit semantics.
func startBufconnLogService(t *testing.T) (logpb.LogServiceClient, *logbuffer.Buffer, func()) {
	t.Helper()
	logger := zap.NewNop()
	buf, err := logbuffer.New(logbuffer.Config{
		Dir:           t.TempDir(),
		PerExecution:  100,
		MaxExecutions: 8,
		DiskCapBytes:  1 << 20,
		DropLevel:     "DEBUG",
		RatePerExec:   10000,
	}, logger)
	require.NoError(t, err)

	lis := bufconn.Listen(1 << 16)
	srv := grpc.NewServer()
	logpb.RegisterLogServiceServer(srv, NewLogServiceServer(buf, logger))
	go func() { _ = srv.Serve(lis) }()

	conn, err := grpc.NewClient("passthrough:///bufnet",
		grpc.WithContextDialer(func(_ context.Context, _ string) (net.Conn, error) {
			return lis.Dial()
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	require.NoError(t, err)

	cleanup := func() {
		_ = conn.Close()
		srv.Stop()
		_ = buf.Close()
	}
	return logpb.NewLogServiceClient(conn), buf, cleanup
}

func TestLogService_AdmitsBatchAndAcksOnEOF(t *testing.T) {
	client, buf, stop := startBufconnLogService(t)
	defer stop()

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	stream, err := client.StreamLogs(ctx)
	require.NoError(t, err)

	require.NoError(t, stream.Send(&logpb.LogBatch{
		AgentId: "agent-x",
		Entries: []*logpb.LogEntry{
			{
				SchemaVersion: "0.1",
				Timestamp:     timestamppb.Now(),
				Level:         "INFO",
				Message:       "first",
				ExecutionId:   "exec-1",
			},
			{
				SchemaVersion: "0.1",
				Timestamp:     timestamppb.Now(),
				Level:         "WARN",
				Message:       "second",
				ExecutionId:   "exec-1",
			},
		},
	}))
	require.NoError(t, stream.CloseSend())

	// Final ack on EOF — receive until the stream closes.
	var lastAck uint64
	for {
		ack, err := stream.Recv()
		if err != nil {
			break
		}
		lastAck = ack.GetReceivedThroughSeq()
	}
	assert.Equal(t, uint64(2), lastAck)

	snap := buf.Snapshot("exec-1")
	require.Len(t, snap, 2)
	assert.Equal(t, "first", snap[0].Message)
	assert.Equal(t, "agent-x", snap[0].AgentID, "batch-level agent_id should populate per-entry AgentID")
	assert.Equal(t, "second", snap[1].Message)
}

func TestLogService_PerEntryAgentIDOverridesBatch(t *testing.T) {
	client, buf, stop := startBufconnLogService(t)
	defer stop()

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	stream, err := client.StreamLogs(ctx)
	require.NoError(t, err)

	require.NoError(t, stream.Send(&logpb.LogBatch{
		AgentId: "batch-default",
		Entries: []*logpb.LogEntry{
			{
				SchemaVersion: "0.1",
				Timestamp:     timestamppb.Now(),
				Level:         "INFO",
				Message:       "override",
				ExecutionId:   "exec-1",
				AgentId:       "per-entry",
			},
		},
	}))
	require.NoError(t, stream.CloseSend())
	for {
		if _, err := stream.Recv(); err != nil {
			break
		}
	}

	snap := buf.Snapshot("exec-1")
	require.Len(t, snap, 1)
	assert.Equal(t, "per-entry", snap[0].AgentID)
}

func TestLogService_BatchOverCap_ReturnsInvalidArgument(t *testing.T) {
	client, _, stop := startBufconnLogService(t)
	defer stop()

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	stream, err := client.StreamLogs(ctx)
	require.NoError(t, err)

	entries := make([]*logpb.LogEntry, maxEntriesPerBatch+1)
	for i := range entries {
		entries[i] = &logpb.LogEntry{
			SchemaVersion: "0.1",
			Timestamp:     timestamppb.Now(),
			Level:         "INFO",
			Message:       "x",
			ExecutionId:   "exec-1",
		}
	}
	// Send may succeed (write-side buffering) — the error surfaces
	// on the next Recv when the server returns InvalidArgument.
	_ = stream.Send(&logpb.LogBatch{Entries: entries})

	_, err = stream.Recv()
	require.Error(t, err)
	st, ok := status.FromError(err)
	require.True(t, ok)
	assert.Equal(t, codes.InvalidArgument, st.Code())
}
