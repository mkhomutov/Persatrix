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
// test shape the per-triple id (or return an error) without standing up a
// real SQLite-backed SessionResolver.
type stubSessionBinder struct {
	fn func(agentID, channelID, userID string) (string, error)
}

func (s *stubSessionBinder) Resolve(_ context.Context, agentID, channelID, userID string) (string, error) {
	return s.fn(agentID, channelID, userID)
}

// TestGRPCMessageDispatcher_EmitsSessionHeader pins the ISSUE-0082 PR 2
// activation: when a SessionResolver is wired, Dispatch resolves the
// per-request session for the `(recipient-agent, channel, sender)` unit
// (RFC 0031 §B) and emits it as the `persatrix-session` gRPC header so the
// persona side re-establishes the session_scope ISSUE-0081 built. The wire
// key MUST match grpcmeta.MDSession (the cross-language contract with
// agents.session_id.SESSION_METADATA_GRPC_KEY).
func TestGRPCMessageDispatcher_EmitsSessionHeader(t *testing.T) {
	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	// Deterministic per-triple id so the assertion can recompute the
	// expected value from the dispatched (agent, channel, sender).
	binder := &stubSessionBinder{fn: func(a, c, u string) (string, error) {
		return "sess|" + a + "|" + c + "|" + u, nil
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
	assert.Equal(t, []string{"sess|agent-b|group:planning|agent-a"}, srv.gotMD.Get(grpcmeta.MDSession),
		"persatrix-session must carry the resolver's id for the (recipient-agent, channel, sender) triple")
}

// TestGRPCMessageDispatcher_DistinctSendersGetDistinctSessions pins the
// per-conversation grain: two senders in one channel for one recipient
// agent resolve to two different sessions, so the persona recalls each
// conversation in isolation rather than sharing one namespace.
func TestGRPCMessageDispatcher_DistinctSendersGetDistinctSessions(t *testing.T) {
	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	binder := &stubSessionBinder{fn: func(a, c, u string) (string, error) {
		return "sess:" + u, nil // vary by sender only
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

	require.Equal(t, []string{"sess:alice"}, first)
	require.Equal(t, []string{"sess:bob"}, second)
	assert.NotEqual(t, first, second,
		"two senders in one channel must resolve to distinct sessions (per-conversation isolation)")
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
	binder := &stubSessionBinder{fn: func(_, _, _ string) (string, error) {
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
	binder := &stubSessionBinder{fn: func(_, _, _ string) (string, error) {
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
