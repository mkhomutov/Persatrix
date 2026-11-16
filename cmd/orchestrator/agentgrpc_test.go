package main

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

	"github.com/mkhomutov/persatrix/internal/generated/logpb"
	"github.com/mkhomutov/persatrix/internal/generated/walletpb"
	"github.com/mkhomutov/persatrix/internal/observability/logbuffer"
)

// agentCallLimit bounds every call these tests make on the agent-facing
// port, so a server that never answers fails a test instead of hanging it.
const agentCallLimit = 10 * time.Second

// dialAgentPort connects to addr as an agent does: plain gRPC, no TLS.
func dialAgentPort(t *testing.T, addr string) *grpc.ClientConn {
	t.Helper()
	cc, err := grpc.NewClient(addr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	require.NoError(t, err)
	t.Cleanup(func() { _ = cc.Close() })
	return cc
}

// acquireProbeLease asks the wallet for a lease, as an agent does before
// every model call, and returns the wallet's answer or the call's error.
func acquireProbeLease(cc *grpc.ClientConn) (*walletpb.LeaseResponse, error) {
	ctx, cancel := context.WithTimeout(context.Background(), agentCallLimit)
	defer cancel()
	return walletpb.NewWalletServiceClient(cc).AcquireLease(ctx, &walletpb.LeaseRequest{
		AgentId:                  "probe-agent",
		Model:                    "probe-model",
		EstimatedInputTokens:     10,
		EstimatedMaxOutputTokens: 10,
		Cause:                    walletpb.Cause_CAUSE_CHAT,
	})
}

// shipProbeBatch sends one batch of log entries, as an agent's log shipper
// does, and returns nil once the orchestrator acknowledged it, or the error
// the stream ended with.
func shipProbeBatch(cc *grpc.ClientConn) error {
	ctx, cancel := context.WithTimeout(context.Background(), agentCallLimit)
	defer cancel()
	stream, err := logpb.NewLogServiceClient(cc).StreamLogs(ctx)
	if err != nil {
		return err
	}
	entries := make([]*logpb.LogEntry, 32) // LogService acks every 32 entries
	for i := range entries {
		entries[i] = &logpb.LogEntry{Message: "probe"}
	}
	// A send to a stream the server already ended reports io.EOF and leaves
	// the status to Recv, so Recv's error is the one to return.
	_ = stream.Send(&logpb.LogBatch{AgentId: "probe-agent", Entries: entries})
	_, err = stream.Recv()
	return err
}

// TestListenForAgents: the agent-facing port opens, and the wallet answers on
// it, whether or not the log buffer started (ISSUE-0204). main() used to open
// the port only when the buffer had started, so a log folder that could not
// be created left every agent without a wallet, and an agent that cannot
// reach a wallet refuses each model call.
func TestListenForAgents(t *testing.T) {
	tests := []struct {
		name          string
		withLogBuffer bool
		// wantLogCode is how a shipped batch ends: OK is an acknowledgement.
		wantLogCode codes.Code
	}{
		{name: "the log buffer started", withLogBuffer: true, wantLogCode: codes.OK},
		{name: "the log buffer did not start", wantLogCode: codes.Unimplemented},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var buf *logbuffer.Buffer
			if tt.withLogBuffer {
				var err error
				buf, err = logbuffer.New(logbuffer.Config{Dir: t.TempDir()}, zap.NewNop())
				require.NoError(t, err)
				t.Cleanup(func() { _ = buf.Close() })
			}

			srv, lis, err := listenForAgents("127.0.0.1:0", buf, nil, nil, testWalletService(t), zap.NewNop())

			require.NoError(t, err)
			stop := runAgentGRPC(srv, lis, grpcStopGrace, zap.NewNop(), func(err error) { t.Errorf("Serve failed: %v", err) })
			t.Cleanup(stop)
			cc := dialAgentPort(t, lis.Addr().String())

			lease, err := acquireProbeLease(cc)
			require.NoError(t, err, "the wallet answers on the port")
			assert.NotEmpty(t, lease.GetGrant().GetLeaseId(), "and grants a lease inside its limits")

			assert.Equal(t, tt.wantLogCode, status.Code(shipProbeBatch(cc)))
		})
	}
}

// TestListenForAgents_APortInUseIsAnError: a port another process holds is
// an error for main() to stop on, with no server left behind.
func TestListenForAgents_APortInUseIsAnError(t *testing.T) {
	held, err := net.Listen("tcp", "127.0.0.1:0")
	require.NoError(t, err)
	t.Cleanup(func() { _ = held.Close() })

	srv, lis, err := listenForAgents(held.Addr().String(), nil, nil, nil, testWalletService(t), zap.NewNop())

	require.Error(t, err)
	assert.Nil(t, srv)
	assert.Nil(t, lis)
}
