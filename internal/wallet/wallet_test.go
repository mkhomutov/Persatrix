// Tests for the WalletService gRPC skeleton (RFC 0023 PR 1).
//
// PR 1 is the proto-surface + always-grant skeleton: AcquireLease always
// returns a LeaseGrant with a server-issued ULID lease_id, and SettleLease /
// ReleaseLease always return SettlementAck{success: true}. Real budget
// enforcement, provisional charges, and the reaper land in PR 2 — these tests
// pin only the skeleton contract.
package wallet

import (
	"context"
	"net"
	"testing"
	"time"

	"github.com/oklog/ulid/v2"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"

	"github.com/mkhomutov/persatrix/internal/generated/walletpb"
)

// startBufconnWalletService stands up an in-process gRPC server hosting the
// WalletService skeleton over a bufconn listener. Returns a connected client
// and a cleanup func. Mirrors startBufconnLogService in
// internal/server/logs_service_test.go so registration is exercised the same
// way the orchestrator exercises it.
func startBufconnWalletService(t *testing.T) (walletpb.WalletServiceClient, func()) {
	t.Helper()

	lis := bufconn.Listen(1 << 16)
	srv := grpc.NewServer()
	walletpb.RegisterWalletServiceServer(srv, NewWalletService(zap.NewNop()))
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
	}
	return walletpb.NewWalletServiceClient(conn), cleanup
}

func testContext(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	t.Cleanup(cancel)
	return ctx
}

// TestAcquireLease_GrantsWithULIDLeaseID pins the always-grant contract: every
// AcquireLease call returns the grant arm of the LeaseResponse oneof, never the
// denied arm, with a server-issued ULID lease_id and a positive TTL.
func TestAcquireLease_GrantsWithULIDLeaseID(t *testing.T) {
	client, cleanup := startBufconnWalletService(t)
	defer cleanup()

	resp, err := client.AcquireLease(testContext(t), &walletpb.LeaseRequest{
		WorkflowId:               "wf-1",
		AgentId:                  "agent-1",
		Model:                    "claude-sonnet-4-6",
		EstimatedInputTokens:     1000,
		EstimatedMaxOutputTokens: 2000,
		Cause:                    walletpb.Cause_CAUSE_WORKFLOW_TASK,
	})
	require.NoError(t, err)

	grant := resp.GetGrant()
	require.NotNil(t, grant, "skeleton must always return the grant arm of the oneof")
	assert.Nil(t, resp.GetDenied(), "skeleton must never return the denied arm")

	_, parseErr := ulid.Parse(grant.GetLeaseId())
	assert.NoError(t, parseErr, "lease_id %q must parse as a ULID", grant.GetLeaseId())
	assert.Positive(t, grant.GetTtlSeconds(), "grant must carry a positive ttl_seconds")
}

// TestAcquireLease_GrantedTokensEchoEstimates pins that the skeleton grant
// echoes the request's token estimates verbatim (RFC 0023 § C — granted_*_tokens
// == estimated_* in Phase 1).
func TestAcquireLease_GrantedTokensEchoEstimates(t *testing.T) {
	client, cleanup := startBufconnWalletService(t)
	defer cleanup()

	tests := []struct {
		name         string
		estInput     int64
		estMaxOutput int64
	}{
		{name: "typical", estInput: 1500, estMaxOutput: 4096},
		{name: "zero estimates", estInput: 0, estMaxOutput: 0},
		{name: "large estimates", estInput: 200000, estMaxOutput: 8192},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			resp, err := client.AcquireLease(testContext(t), &walletpb.LeaseRequest{
				AgentId:                  "agent-1",
				Model:                    "claude-sonnet-4-6",
				EstimatedInputTokens:     tc.estInput,
				EstimatedMaxOutputTokens: tc.estMaxOutput,
				Cause:                    walletpb.Cause_CAUSE_CHAT,
			})
			require.NoError(t, err)
			grant := resp.GetGrant()
			require.NotNil(t, grant)
			assert.Equal(t, tc.estInput, grant.GetGrantedInputTokens())
			assert.Equal(t, tc.estMaxOutput, grant.GetGrantedOutputTokens())
		})
	}
}

// TestAcquireLease_IssuesUniqueLeaseIDs pins that each acquisition gets a
// distinct lease_id — the skeleton's Settle/Release already exercise the real
// server-issued ID shape, so collisions must not occur.
func TestAcquireLease_IssuesUniqueLeaseIDs(t *testing.T) {
	client, cleanup := startBufconnWalletService(t)
	defer cleanup()

	seen := make(map[string]struct{})
	for i := 0; i < 50; i++ {
		resp, err := client.AcquireLease(testContext(t), &walletpb.LeaseRequest{
			AgentId: "agent-1",
			Model:   "claude-sonnet-4-6",
			Cause:   walletpb.Cause_CAUSE_AUTONOMOUS_TICK,
		})
		require.NoError(t, err)
		id := resp.GetGrant().GetLeaseId()
		require.NotEmpty(t, id)
		_, dup := seen[id]
		require.False(t, dup, "lease_id %q issued twice", id)
		seen[id] = struct{}{}
	}
}

// TestSettleLease_AlwaysSucceeds pins the skeleton SettleLease contract: any
// lease_id settles successfully. PR 2 makes this reject unknown IDs.
func TestSettleLease_AlwaysSucceeds(t *testing.T) {
	client, cleanup := startBufconnWalletService(t)
	defer cleanup()

	for _, leaseID := range []string{ulid.Make().String(), "not-a-real-lease", ""} {
		ack, err := client.SettleLease(testContext(t), &walletpb.SettlementRequest{
			LeaseId:            leaseID,
			ActualInputTokens:  900,
			ActualOutputTokens: 1800,
		})
		require.NoError(t, err)
		assert.True(t, ack.GetSuccess(), "skeleton SettleLease must succeed for lease_id %q", leaseID)
	}
}

// TestReleaseLease_AlwaysSucceeds pins the skeleton ReleaseLease contract: any
// lease_id releases successfully. PR 2 makes this reject unknown IDs.
func TestReleaseLease_AlwaysSucceeds(t *testing.T) {
	client, cleanup := startBufconnWalletService(t)
	defer cleanup()

	for _, leaseID := range []string{ulid.Make().String(), "not-a-real-lease", ""} {
		ack, err := client.ReleaseLease(testContext(t), &walletpb.ReleaseRequest{
			LeaseId: leaseID,
			Reason:  "aborted",
		})
		require.NoError(t, err)
		assert.True(t, ack.GetSuccess(), "skeleton ReleaseLease must succeed for lease_id %q", leaseID)
	}
}

// TestAcquireLease_RoundTripsThroughSettle pins the end-to-end skeleton path:
// a lease acquired from AcquireLease can be settled with its own lease_id.
func TestAcquireLease_RoundTripsThroughSettle(t *testing.T) {
	client, cleanup := startBufconnWalletService(t)
	defer cleanup()

	resp, err := client.AcquireLease(testContext(t), &walletpb.LeaseRequest{
		AgentId: "agent-1",
		Model:   "claude-sonnet-4-6",
		Cause:   walletpb.Cause_CAUSE_SUB_AGENT,
	})
	require.NoError(t, err)
	leaseID := resp.GetGrant().GetLeaseId()
	require.NotEmpty(t, leaseID)

	ack, err := client.SettleLease(testContext(t), &walletpb.SettlementRequest{LeaseId: leaseID})
	require.NoError(t, err)
	assert.True(t, ack.GetSuccess())
}
