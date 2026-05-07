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
	"go.uber.org/zap/zapcore"
	"go.uber.org/zap/zaptest/observer"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
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
	env := DispatchEnvelope{
		Recipient:            Member{ParticipantID: "agent-b", RespondPolicy: RespondWhenMentioned},
		ThreadParentSenderID: "",
	}
	require.NoError(t, d.Dispatch(context.Background(), env, msg))

	require.NotNil(t, srv.gotEvent)
	assert.Equal(t, "m-1", srv.gotEvent.MessageId)
	assert.Equal(t, "group:planning", srv.gotEvent.ChannelId)
	assert.Equal(t, "group", srv.gotEvent.ChannelType, "channel_type derived from id prefix")
	assert.Equal(t, "agent-a", srv.gotEvent.SenderId)
	assert.Equal(t, "hi", srv.gotEvent.Content)
	assert.Equal(t, ts.Format(time.RFC3339Nano), srv.gotEvent.Timestamp)
	assert.Equal(t, []string{"agent-b"}, srv.gotEvent.Mentions)
	assert.Equal(t, "when_mentioned", srv.gotEvent.RespondPolicy,
		"PR 4b: per-recipient respond_policy must traverse the wire so the gate fires receiver-side")
	assert.Empty(t, srv.gotEvent.ThreadParentSenderId,
		"non-thread event leaves thread_parent_sender_id empty")
}

func TestGRPCMessageDispatcher_ThreadParentSenderIDPropagated(t *testing.T) {
	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())
	d.dial = dial

	msg := ChannelMessage{
		ID: "reply-1", ChannelID: "group:planning", SenderID: "agent-a",
		Content: "thread reply", Timestamp: time.Now().UTC(), ThreadID: "parent-1",
	}
	env := DispatchEnvelope{
		Recipient:            Member{ParticipantID: "agent-b", RespondPolicy: RespondWhenMentioned},
		ThreadParentSenderID: "agent-b",
	}
	require.NoError(t, d.Dispatch(context.Background(), env, msg))

	require.NotNil(t, srv.gotEvent)
	assert.Equal(t, "parent-1", srv.gotEvent.ThreadId)
	assert.Equal(t, "agent-b", srv.gotEvent.ThreadParentSenderId,
		"thread_parent_sender_id MUST flow through so the receiver gate can fire thread-reply-to-self")
}

func TestGRPCMessageDispatcher_UnknownParticipantIsNoop(t *testing.T) {
	// A participant present in a channel but not yet registered MUST NOT
	// surface as an error — the channel contract is at-most-once
	// best-effort, with reconnect-via-history covering the gap.
	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())

	err := d.Dispatch(context.Background(), DispatchEnvelope{
		Recipient: Member{ParticipantID: "ghost", RespondPolicy: RespondWhenMentioned},
	}, ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a",
	})
	assert.NoError(t, err, "unknown participant must be silently dropped")
}

func TestGRPCMessageDispatcher_DegradedAgentReturnsError(t *testing.T) {
	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusDegraded},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())

	err := d.Dispatch(context.Background(), DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	}, ChannelMessage{
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

	err := d.Dispatch(context.Background(), DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	}, ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a",
	})
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrAgentNotReady)
}

func TestGRPCMessageDispatcher_ResolverErrorPropagates(t *testing.T) {
	resolver := &stubResolver{err: errors.New("boom")}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())

	err := d.Dispatch(context.Background(), DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	}, ChannelMessage{
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
	d := &GRPCMessageDispatcher{logger: zap.NewNop()}
	ev := d.channelMessageToProto(msg, DispatchEnvelope{
		Recipient: Member{ParticipantID: "b", RespondPolicy: RespondAlways},
	})
	after := time.Now().UTC().Add(1 * time.Second)

	parsed, err := time.Parse(time.RFC3339Nano, ev.Timestamp)
	require.NoError(t, err)
	assert.True(t, parsed.After(before) && parsed.Before(after),
		"zero Timestamp must be replaced with now() at the dispatch boundary")
	assert.Equal(t, "dm", ev.ChannelType)
}

// TestGRPCMessageDispatcher_UnknownChannelPrefixLogsWarn covers PR #250
// review (Medium #4). Before the fix, channelMessageToProto silently
// discarded the channelTypeFromID error and shipped an empty
// ChannelType on the wire — a regression in router-side prefix
// validation would surface only at the receiver, where the
// programmer-error origin is opaque. The dispatcher now warns at the
// translation site so an unexpected prefix is visible in the sender's
// logs at the moment of dispatch.
//
// The router validates channel prefixes on the publish path, so this
// branch should never fire in production. The Warn exists to flag
// regressions in that contract loudly rather than letting them ride to
// the receiver as an empty string.
func TestGRPCMessageDispatcher_UnknownChannelPrefixLogsWarn(t *testing.T) {
	core, recorded := observer.New(zapcore.WarnLevel)
	logger := zap.New(core)

	d := &GRPCMessageDispatcher{logger: logger}
	ev := d.channelMessageToProto(ChannelMessage{
		ID:        "m-1",
		ChannelID: "unknown-prefix:foo", // not group: / dm: / thread:
		SenderID:  "agent-a",
	}, DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	})

	assert.Empty(t, ev.ChannelType,
		"contract preserved: unknown prefix still yields empty ChannelType")

	logs := recorded.FilterMessageSnippet("unknown channel_id prefix").All()
	require.Len(t, logs, 1,
		"unknown channel id prefix must produce exactly one Warn at the "+
			"dispatch translation site (saw %d entries)", len(logs))
	assert.Equal(t, zapcore.WarnLevel, logs[0].Level)

	fields := logs[0].ContextMap()
	assert.Equal(t, "unknown-prefix:foo", fields["channel_id"],
		"channel_id field must be present so operators can locate the offender")
	assert.Equal(t, "m-1", fields["message_id"])
}

// TestGRPCMessageDispatcher_KnownChannelPrefixDoesNotLog is the
// negative case: a well-formed channel id (the production path) must
// NOT produce a warn log. Without this guard, a future refactor that
// drops the type check could leave the warn firing on every dispatch —
// silent log noise that operators learn to ignore.
func TestGRPCMessageDispatcher_KnownChannelPrefixDoesNotLog(t *testing.T) {
	core, recorded := observer.New(zapcore.WarnLevel)
	logger := zap.New(core)

	d := &GRPCMessageDispatcher{logger: logger}
	_ = d.channelMessageToProto(ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a",
	}, DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	})

	assert.Equal(t, 0, recorded.Len(),
		"happy-path translation must be silent at Warn level")
}

// TestGRPCMessageDispatcher_ContextCancelledMidCall pins the deadline
// contract from PR #250 review (Should-Fix #2). [ChannelRouter.fanout]
// supplies a 5 s per-recipient context deadline (router.go ~line 187);
// the dispatcher MUST surface that cancellation through the gRPC call
// rather than swallowing it or stacking a second timeout on top.
//
// Regression scenario this catches: a future refactor that wraps the
// dial or call site in a fresh `context.Background()` (or
// `context.WithTimeout(ctx, X)` where X > the inbound deadline) would
// quietly extend the per-recipient ceiling, breaking the fan-out SLO
// the router enforces.
//
// Test shape:
//   - Server handler blocks until its own context is done, then returns.
//   - Caller cancels the dispatch context mid-flight.
//   - Dispatch must return a non-nil error and that error MUST wrap
//     [context.Canceled] so the router records `status="error"` and
//     does not retry.
func TestGRPCMessageDispatcher_ContextCancelledMidCall(t *testing.T) {
	// Server blocks until its (server-side) context is cancelled, which
	// happens when the client cancels the parent context. We can't
	// inspect the server ctx via `respond` (its signature has no ctx),
	// so we use a channel + bounded sleep to guarantee the call is
	// in-flight when we cancel.
	released := make(chan struct{})
	srv := &recordingAgentServer{
		respond: func() error {
			<-released
			return nil
		},
	}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()
	defer close(released) // unblock the server even if the assertion below trips early

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())
	d.dial = dial

	ctx, cancel := context.WithCancel(context.Background())

	// Cancel after a short delay so Dispatch is past dial+RPC-send and
	// genuinely waiting on the server response. The 50 ms window is
	// generous for an in-process bufconn round trip; if it ever turns
	// flaky on a slow CI runner, a `time.AfterFunc` keyed off a server
	// signal would be the next step.
	time.AfterFunc(50*time.Millisecond, cancel)

	err := d.Dispatch(ctx, DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	}, ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a",
		Content: "hi", Timestamp: time.Now().UTC(),
	})

	require.Error(t, err, "cancelled parent context must surface as a Dispatch error")
	// gRPC encodes context cancellation as a status error with
	// codes.Canceled rather than as a wrapped context.Canceled
	// sentinel — `errors.Is(err, context.Canceled)` would NOT match
	// here. The router (cmd/orchestrator/channels.go ChannelRouter
	// fanout) treats any non-nil error as `status="error"` regardless
	// of code, so the contract this test pins is: cancellation must
	// reach the wire and come back as a gRPC Canceled status, not as
	// a successful dispatch (which would happen if the dispatcher
	// quietly substituted context.Background()) and not as a
	// DeadlineExceeded (which would mean a second timeout was stacked
	// on top of the inbound deadline).
	assert.Equal(t, codes.Canceled, status.Code(err),
		"Dispatch must propagate ctx cancellation as gRPC Canceled; "+
			"any other code means the router's per-recipient 5s deadline "+
			"(router.go fanout) is silently bypassed or replaced. err=%v", err)
}

// TestGRPCMessageDispatcher_RPCStatusErrorPropagates closes ISSUE-0030.
// Prior coverage exercised the dial happy path, registry/address gating,
// and ctx-cancel-mid-call, but the case where ReceiveChannelMessage
// itself returns a gRPC status error (the receiver is up and reachable,
// but unhealthy / not implementing the RPC) was untested. Without this
// guard, a regression that swallowed Unavailable / Unimplemented (e.g.
// a defensive `return nil` on transient errors) would silently degrade
// channel delivery to a black hole — the router would record
// status="ok" and skip the retry-via-history fallback.
//
// Table-driven over the two codes the v0.3.0 dispatcher contract cares
// about:
//
//   - Unavailable: receiver is reachable but not ready (restarting,
//     handshake failed). Must surface so the router records status="error".
//   - Unimplemented: receiver is up but does not implement the RPC
//     (binary-skew window during a rolling deploy). Must propagate
//     rather than degrade to silent drop.
func TestGRPCMessageDispatcher_RPCStatusErrorPropagates(t *testing.T) {
	cases := []struct {
		name string
		code codes.Code
	}{
		{"unavailable", codes.Unavailable},
		{"unimplemented", codes.Unimplemented},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			srv := &recordingAgentServer{
				respond: func() error {
					return status.Error(tc.code, "boom")
				},
			}
			dial, cleanup := startBufconnServer(t, srv)
			defer cleanup()

			resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
				"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
			}}
			d := NewGRPCMessageDispatcher(resolver, zap.NewNop())
			d.dial = dial

			err := d.Dispatch(context.Background(), DispatchEnvelope{
				Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
			}, ChannelMessage{
				ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a",
				Content: "hi", Timestamp: time.Now().UTC(),
			})

			require.Error(t, err,
				"%s status from ReceiveChannelMessage must surface as a "+
					"non-nil Dispatch error; nil here would let the router "+
					"record status=\"ok\" against a dropped message", tc.name)
			assert.Equal(t, tc.code, status.Code(err),
				"Dispatch must propagate the gRPC status code unchanged so "+
					"the router (or future retry policy) can branch on it; "+
					"got %v, want %v. err=%v", status.Code(err), tc.code, err)
		})
	}
}
