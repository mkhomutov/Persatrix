package channels

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/observability/grpcmeta"
	"github.com/mkhomutov/persatrix/internal/registry"
)

// ISSUE-0082 PR 2 — `persatrix-session` emission on the dispatch path.
//
// These tests live in their own file (not grpc_dispatcher_test.go) only to
// keep each file under the review-friendly size limit; they exercise the
// same [GRPCMessageDispatcher] and reuse its `recordingAgentServer`
// (whose `gotMD` captures the incoming gRPC metadata the receiver observed)
// and `startBufconnServer` helpers.

// stubSessionBinder implements [SessionBinder] for unit tests. `fn` lets a
// test shape the per-`(agent, channel)` id (or return an error) without
// standing up a real SQLite-backed SessionResolver.
type stubSessionBinder struct {
	fn func(agentID, channelID string) (string, error)
}

func (s *stubSessionBinder) Resolve(_ context.Context, agentID, channelID string) (string, error) {
	return s.fn(agentID, channelID)
}

// TestGRPCMessageDispatcher_EmitsSessionHeader pins the ISSUE-0082 PR 2
// activation: when a SessionResolver is wired, Dispatch resolves the
// per-request session for the `(recipient-agent, channel)` unit (RFC 0031 §A
// scope-axes amendment — the sender axis was dropped in ISSUE-0083) and emits
// it as the `persatrix-session` gRPC header so the persona side re-establishes
// the session_scope ISSUE-0081 built. The wire key MUST match
// grpcmeta.MDSession (the cross-language contract with
// agents.session_id.SESSION_METADATA_GRPC_KEY).
func TestGRPCMessageDispatcher_EmitsSessionHeader(t *testing.T) {
	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	// Deterministic per-pair id so the assertion can recompute the expected
	// value from the dispatched (agent, channel) — and so it stays the same
	// regardless of which sender's message triggered the dispatch.
	binder := &stubSessionBinder{fn: func(a, c string) (string, error) {
		return "sess|" + a + "|" + c, nil
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop(), WithSessionResolver(binder))
	d.dial = dial

	msg := ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a",
		Content: "hi", Timestamp: time.Now().UTC(),
	}
	require.NoError(t, d.Dispatch(context.Background(), DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondWhenMentioned},
	}, msg))

	require.NotNil(t, srv.gotMD, "receiver must observe incoming metadata")
	assert.Equal(t, []string{"sess|agent-b|group:planning"}, srv.gotMD.Get(grpcmeta.MDSession),
		"persatrix-session must carry the resolver's id for the (recipient-agent, channel) pair — the sender is not part of the key")
}

// TestGRPCMessageDispatcher_SendersInOneChannelShareSession pins the
// post-ISSUE-0083 room-continuity grain (inverting the pre-reframing
// "distinct senders → distinct sessions" assertion): two senders in one
// channel for one recipient agent resolve to the SAME session, so the agent's
// episodic memory of one room is not fragmented by who spoke. When Alice
// references something Bob said, the agent recalls under one shared room
// session that saw both turns.
func TestGRPCMessageDispatcher_SendersInOneChannelShareSession(t *testing.T) {
	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	// Vary the id by (agent, channel) only — the sender is not an input, so a
	// binder that tried to vary by sender could not, by construction.
	binder := &stubSessionBinder{fn: func(a, c string) (string, error) {
		return "sess:" + a + ":" + c, nil
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop(), WithSessionResolver(binder))
	d.dial = dial

	base := DispatchEnvelope{Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways}}

	require.NoError(t, d.Dispatch(context.Background(), base, ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "alice", Timestamp: time.Now().UTC(),
	}))
	first := srv.gotMD.Get(grpcmeta.MDSession)

	require.NoError(t, d.Dispatch(context.Background(), base, ChannelMessage{
		ID: "m-2", ChannelID: "group:planning", SenderID: "bob", Timestamp: time.Now().UTC(),
	}))
	second := srv.gotMD.Get(grpcmeta.MDSession)

	require.Equal(t, []string{"sess:agent-b:group:planning"}, first)
	assert.Equal(t, first, second,
		"two senders in one channel must resolve to ONE shared room session (ISSUE-0083: sender axis dropped)")
}

// TestGRPCMessageDispatcher_DistinctChannelsGetDistinctSessions pins the
// isolation that survives the sender-axis drop: the channel axis alone keeps
// distinct rooms (and the two DM-thread channel ids `dm:a:b` vs `dm:c:b`)
// isolated. Concurrent conversations stay independent — the property
// ISSUE-0081/0082 shipped — now keyed on (agent, channel) instead of the
// triple.
func TestGRPCMessageDispatcher_DistinctChannelsGetDistinctSessions(t *testing.T) {
	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	binder := &stubSessionBinder{fn: func(a, c string) (string, error) {
		return "sess:" + a + ":" + c, nil
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop(), WithSessionResolver(binder))
	d.dial = dial

	base := DispatchEnvelope{Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways}}

	require.NoError(t, d.Dispatch(context.Background(), base, ChannelMessage{
		ID: "m-1", ChannelID: "dm:a:b", SenderID: "alice", Timestamp: time.Now().UTC(),
	}))
	first := srv.gotMD.Get(grpcmeta.MDSession)

	require.NoError(t, d.Dispatch(context.Background(), base, ChannelMessage{
		ID: "m-2", ChannelID: "dm:c:b", SenderID: "carol", Timestamp: time.Now().UTC(),
	}))
	second := srv.gotMD.Get(grpcmeta.MDSession)

	require.Equal(t, []string{"sess:agent-b:dm:a:b"}, first)
	require.Equal(t, []string{"sess:agent-b:dm:c:b"}, second)
	assert.NotEqual(t, first, second,
		"distinct channels must resolve to distinct sessions (per-room isolation preserved)")
}

// TestGRPCMessageDispatcher_ExplicitOverrideBeatsAutoBinding pins the
// RFC 0031 Phase 3 PR 4 reconciliation: when the request context carries an
// explicit `--session` override, Dispatch emits THAT id as `persatrix-session`,
// not the resolver's auto-binding. The explicit operator signal is the
// highest-precedence one (OQ #6 amendment) — the override wins for the one
// request it accompanies.
func TestGRPCMessageDispatcher_ExplicitOverrideBeatsAutoBinding(t *testing.T) {
	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	// The binder would auto-bind to this id; the override must beat it.
	binder := &stubSessionBinder{fn: func(a, c string) (string, error) {
		return "auto|" + a + "|" + c, nil
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop(), WithSessionResolver(binder))
	d.dial = dial

	ctx := WithSessionOverride(context.Background(), "run-arc-3")
	require.NoError(t, d.Dispatch(ctx, DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	}, ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "alice", Timestamp: time.Now().UTC(),
	}))

	assert.Equal(t, []string{"run-arc-3"}, srv.gotMD.Get(grpcmeta.MDSession),
		"an explicit --session override must beat the auto-binding for this request")
}

// TestGRPCMessageDispatcher_OverrideEmittedWithoutResolver pins that the
// override is honoured even when no SessionResolver is wired — the override
// path reads the context directly and does not depend on the auto-binding
// machinery. (A `--session` invocation against a dispatcher with no resolver,
// e.g. a single-conversation deployment, still routes to the named session.)
func TestGRPCMessageDispatcher_OverrideEmittedWithoutResolver(t *testing.T) {
	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop()) // no WithSessionResolver
	d.dial = dial

	ctx := WithSessionOverride(context.Background(), "run-arc-3")
	require.NoError(t, d.Dispatch(ctx, DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	}, ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "alice", Timestamp: time.Now().UTC(),
	}))

	assert.Equal(t, []string{"run-arc-3"}, srv.gotMD.Get(grpcmeta.MDSession),
		"the override must emit even with no resolver wired")
}

// TestGRPCMessageDispatcher_NoOverrideKeepsAutoBinding pins the no-regression
// half of the reconciliation: absent an override on the context, emission is
// byte-identical to today — the resolver's auto-binding id is emitted. This is
// the property the override must not weaken (Phase 2 + ISSUE-0082 concurrent
// isolation).
func TestGRPCMessageDispatcher_NoOverrideKeepsAutoBinding(t *testing.T) {
	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	binder := &stubSessionBinder{fn: func(a, c string) (string, error) {
		return "auto|" + a + "|" + c, nil
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop(), WithSessionResolver(binder))
	d.dial = dial

	// context.Background() carries no override — the auto-binding must stand.
	require.NoError(t, d.Dispatch(context.Background(), DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	}, ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "alice", Timestamp: time.Now().UTC(),
	}))

	assert.Equal(t, []string{"auto|agent-b|group:planning"}, srv.gotMD.Get(grpcmeta.MDSession),
		"absent an override the auto-binding must be emitted unchanged (no concurrent-isolation regression)")
}

// TestGRPCMessageDispatcher_OverrideAndAutoBindingStayIndependent pins that an
// overridden dispatch and a non-overridden dispatch through the SAME
// dispatcher emit independent ids — the override does not leak into a sibling
// request that did not carry one. Two recording servers isolate the captured
// metadata so the assertion needs no shared-state synchronisation.
func TestGRPCMessageDispatcher_OverrideAndAutoBindingStayIndependent(t *testing.T) {
	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	binder := &stubSessionBinder{fn: func(a, c string) (string, error) {
		return "auto|" + a + "|" + c, nil
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop(), WithSessionResolver(binder))

	// Overridden dispatch.
	srvOverride := &recordingAgentServer{}
	dialO, cleanupO := startBufconnServer(t, srvOverride)
	defer cleanupO()
	d.dial = dialO
	require.NoError(t, d.Dispatch(WithSessionOverride(context.Background(), "run-arc-3"),
		DispatchEnvelope{Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways}},
		ChannelMessage{ID: "m-1", ChannelID: "group:planning", SenderID: "alice", Timestamp: time.Now().UTC()}))

	// Non-overridden dispatch through the same dispatcher.
	srvAuto := &recordingAgentServer{}
	dialA, cleanupA := startBufconnServer(t, srvAuto)
	defer cleanupA()
	d.dial = dialA
	require.NoError(t, d.Dispatch(context.Background(),
		DispatchEnvelope{Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways}},
		ChannelMessage{ID: "m-2", ChannelID: "group:planning", SenderID: "bob", Timestamp: time.Now().UTC()}))

	assert.Equal(t, []string{"run-arc-3"}, srvOverride.gotMD.Get(grpcmeta.MDSession))
	assert.Equal(t, []string{"auto|agent-b|group:planning"}, srvAuto.gotMD.Get(grpcmeta.MDSession),
		"a sibling request without an override must still get the auto-binding")
}

// TestGRPCMessageDispatcher_OverridePinnedOnSpan pins that the override id —
// like the auto-binding — lands on the channel.dispatch span as `session.id`,
// so trace correlation works regardless of which path supplied the session
// (RFC 0031 OQ #7).
func TestGRPCMessageDispatcher_OverridePinnedOnSpan(t *testing.T) {
	exporter := installSpanRecorder(t)

	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "agent-b:9090", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())
	d.dial = dial

	require.NoError(t, d.Dispatch(WithSessionOverride(context.Background(), "run-arc-3"),
		DispatchEnvelope{Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways}},
		ChannelMessage{ID: "m-1", ChannelID: "group:planning", SenderID: "alice", Timestamp: time.Now().UTC()}))

	dispatchSpans := filterSpansByName(exporter.GetSpans(), "channel.dispatch")
	require.Len(t, dispatchSpans, 1)
	assert.Equal(t, "run-arc-3", spanAttrMap(dispatchSpans[0])["session.id"],
		"the override id must be pinned on the channel.dispatch span")
}

// TestGRPCMessageDispatcher_SessionResolveErrorIsNonFatal pins the
// graceful-degradation contract: a session-resolution failure must NOT drop
// the message. Dispatch proceeds without the header, and the persona side
// falls back to its construction-time (legacy) snapshot — exactly the
// pre-activation behaviour. A session hiccup is never allowed to break
// delivery.
func TestGRPCMessageDispatcher_SessionResolveErrorIsNonFatal(t *testing.T) {
	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	binder := &stubSessionBinder{fn: func(_, _ string) (string, error) {
		return "", errors.New("db locked")
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop(), WithSessionResolver(binder))
	d.dial = dial

	require.NoError(t, d.Dispatch(context.Background(), DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	}, ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a", Timestamp: time.Now().UTC(),
	}), "session resolve failure must not fail delivery")

	require.NotNil(t, srv.gotEvent, "message must still be delivered on resolve failure")
	assert.Empty(t, srv.gotMD.Get(grpcmeta.MDSession),
		"no persatrix-session header when resolution failed (persona falls back to legacy)")
}

// TestGRPCMessageDispatcher_NoResolverEmitsNoSessionHeader pins the
// no-emission default: a dispatcher built without WithSessionResolver (the
// channels-disabled / pre-wiring path) ships no session header, so behaviour
// is byte-identical to the pre-ISSUE-0082 dispatch.
func TestGRPCMessageDispatcher_NoResolverEmitsNoSessionHeader(t *testing.T) {
	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())
	d.dial = dial

	require.NoError(t, d.Dispatch(context.Background(), DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	}, ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a", Timestamp: time.Now().UTC(),
	}))

	require.NotNil(t, srv.gotEvent)
	assert.Empty(t, srv.gotMD.Get(grpcmeta.MDSession),
		"no resolver wired → no persatrix-session header emitted")
}

// TestGRPCMessageDispatcher_SessionPinnedOnSpan pins the trace-correlation
// half of the emission: the resolved session id is attached to the
// `channel.dispatch` span as `session.id` (low-cardinality-on-span, never a
// metric label — RFC 0031 OQ #7 posture) so an operator can pivot a trace to
// the conversation it served.
func TestGRPCMessageDispatcher_SessionPinnedOnSpan(t *testing.T) {
	exporter := installSpanRecorder(t)

	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "agent-b:9090", Status: registry.StatusHealthy},
	}}
	binder := &stubSessionBinder{fn: func(_, _ string) (string, error) {
		return "sess-on-span", nil
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop(), WithSessionResolver(binder))
	d.dial = dial

	require.NoError(t, d.Dispatch(context.Background(), DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	}, ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a", Timestamp: time.Now().UTC(),
	}))

	dispatchSpans := filterSpansByName(exporter.GetSpans(), "channel.dispatch")
	require.Len(t, dispatchSpans, 1)
	assert.Equal(t, "sess-on-span", spanAttrMap(dispatchSpans[0])["session.id"],
		"resolved session id must be pinned on the channel.dispatch span for trace correlation")
}
