package channels

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
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"

	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
	"github.com/mkhomutov/persatrix/internal/registry"
)

// stubResolver implements [AgentResolver] for unit tests; no goroutine
// safety needed because each test owns its own instance.
type stubResolver struct {
	agents map[string]*registry.AgentInfo
	err    error
}

func (s *stubResolver) Get(_ context.Context, id string) (*registry.AgentInfo, error) {
	if s.err != nil {
		return nil, s.err
	}
	a, ok := s.agents[id]
	if !ok {
		return nil, registry.ErrAgentNotFound
	}
	return a, nil
}

// recordingAgentServer captures the most recent ReceiveChannelMessage
// payload so tests can assert wire-shape invariants.
type recordingAgentServer struct {
	taskpb.UnimplementedAgentServiceServer
	gotEvent *taskpb.ChannelMessageEvent
	respond  func() error
}

func (r *recordingAgentServer) ReceiveChannelMessage(_ context.Context, ev *taskpb.ChannelMessageEvent) (*taskpb.TaskAck, error) {
	r.gotEvent = ev
	if r.respond != nil {
		if err := r.respond(); err != nil {
			return nil, err
		}
	}
	return &taskpb.TaskAck{Success: true}, nil
}

// startBufconnServer spins up an in-process gRPC server bound to a
// bufconn listener and registers a recordingAgentServer. The returned
// dialFunc routes any grpc.NewClient call through the bufconn — the
// `target` argument is ignored, mirroring the production path's
// "registry → address → dial" indirection without requiring a real port.
func startBufconnServer(t *testing.T, srv *recordingAgentServer) (dialFunc, func()) {
	t.Helper()
	lis := bufconn.Listen(1 << 20)
	gsrv := grpc.NewServer()
	taskpb.RegisterAgentServiceServer(gsrv, srv)
	go func() { _ = gsrv.Serve(lis) }()

	dial := func(_ string, opts ...grpc.DialOption) (*grpc.ClientConn, error) {
		full := []grpc.DialOption{
			grpc.WithContextDialer(func(_ context.Context, _ string) (net.Conn, error) {
				return lis.DialContext(context.Background())
			}),
			grpc.WithTransportCredentials(insecure.NewCredentials()),
		}
		// `opts` from the dispatcher includes WithTransportCredentials —
		// duplicate options are deterministic-last-wins per gRPC docs, so
		// appending callers' opts after our defaults is safe.
		full = append(full, opts...)
		return grpc.NewClient("passthrough://bufconn", full...)
	}
	cleanup := func() {
		gsrv.Stop()
		_ = lis.Close()
	}
	return dial, cleanup
}

func TestGRPCMessageDispatcher_HappyPath(t *testing.T) {
	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())
	d.dial = dial

	ts := time.Date(2026, 5, 5, 12, 0, 0, 0, time.UTC)
	msg := ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a",
		Content: "hi", Timestamp: ts, Mentions: []string{"agent-b"},
	}
	require.NoError(t, d.Dispatch(context.Background(), "agent-b", msg))

	require.NotNil(t, srv.gotEvent)
	assert.Equal(t, "m-1", srv.gotEvent.MessageId)
	assert.Equal(t, "group:planning", srv.gotEvent.ChannelId)
	assert.Equal(t, "group", srv.gotEvent.ChannelType, "channel_type derived from id prefix")
	assert.Equal(t, "agent-a", srv.gotEvent.SenderId)
	assert.Equal(t, "hi", srv.gotEvent.Content)
	assert.Equal(t, ts.Format(time.RFC3339Nano), srv.gotEvent.Timestamp)
	assert.Equal(t, []string{"agent-b"}, srv.gotEvent.Mentions)
}

func TestGRPCMessageDispatcher_UnknownParticipantIsNoop(t *testing.T) {
	// A participant present in a channel but not yet registered MUST NOT
	// surface as an error — the channel contract is at-most-once
	// best-effort, with reconnect-via-history covering the gap.
	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())

	err := d.Dispatch(context.Background(), "ghost", ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a",
	})
	assert.NoError(t, err, "unknown participant must be silently dropped")
}

func TestGRPCMessageDispatcher_DegradedAgentReturnsError(t *testing.T) {
	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusDegraded},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())

	err := d.Dispatch(context.Background(), "agent-b", ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a",
	})
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrAgentNotReady)
}

func TestGRPCMessageDispatcher_EmptyAddressReturnsError(t *testing.T) {
	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())

	err := d.Dispatch(context.Background(), "agent-b", ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a",
	})
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrAgentNotReady)
}

func TestGRPCMessageDispatcher_ResolverErrorPropagates(t *testing.T) {
	resolver := &stubResolver{err: errors.New("boom")}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())

	err := d.Dispatch(context.Background(), "agent-b", ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a",
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "registry lookup")
}

func TestChannelMessageToProto_ZeroTimestampDefaultsToNow(t *testing.T) {
	msg := ChannelMessage{
		ID: "m-1", ChannelID: "dm:a:b", SenderID: "a",
	}
	before := time.Now().UTC().Add(-1 * time.Second)
	ev := channelMessageToProto(msg)
	after := time.Now().UTC().Add(1 * time.Second)

	parsed, err := time.Parse(time.RFC3339Nano, ev.Timestamp)
	require.NoError(t, err)
	assert.True(t, parsed.After(before) && parsed.Before(after),
		"zero Timestamp must be replaced with now() at the dispatch boundary")
	assert.Equal(t, "dm", ev.ChannelType)
}
