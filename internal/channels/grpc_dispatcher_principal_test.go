package channels

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/observability/grpcmeta"
	"github.com/mkhomutov/persatrix/internal/registry"
)

// ISSUE-0082 Part 2 PR 1 (v0.3.14) — principal emission at the dispatch
// chokepoint.
//
// The rail lands dormant: no production code stamps a principal onto the
// request context yet, so these tests are the only callers. They pin the two
// halves of the contract the producer (PR 2) will rely on: a ctx-carried
// principal emits as `persatrix-principal` beside the session and epoch
// headers, and an absent principal emits NOTHING — the no-delta half that
// keeps `auth.mode: disabled`, unauthenticated callers, and
// agent/autonomous-origin turns byte-identical to pre-activation behaviour.
// They reuse the package's recordingAgentServer / startBufconnServer /
// stubResolver helpers.

// TestGRPCMessageDispatcher_PrincipalEmittedFromContext pins the emission
// half: a principal stamped by WithPrincipal rides the wire as
// `persatrix-principal`.
func TestGRPCMessageDispatcher_PrincipalEmittedFromContext(t *testing.T) {
	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())
	d.dial = dial

	ctx := WithPrincipal(context.Background(), "user-alice")
	require.NoError(t, d.Dispatch(ctx, DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	}, ChannelMessage{ID: "m-1", ChannelID: "group:planning", SenderID: "alice", Timestamp: time.Now().UTC()}))

	assert.Equal(t, []string{"user-alice"}, srv.gotMD.Get(grpcmeta.MDPrincipal),
		"a ctx-carried principal must emit as persatrix-principal")
}

// TestGRPCMessageDispatcher_NoPrincipalEmitsNoHeader pins the no-delta half:
// absent a ctx principal the header is entirely absent — not empty — so the
// persona resolves its 'local' default exactly as before this PR. This is
// the dormant-rail guarantee and the future disabled-mode contract in one.
func TestGRPCMessageDispatcher_NoPrincipalEmitsNoHeader(t *testing.T) {
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
	}, ChannelMessage{ID: "m-1", ChannelID: "group:planning", SenderID: "alice", Timestamp: time.Now().UTC()}))

	assert.Empty(t, srv.gotMD.Get(grpcmeta.MDPrincipal),
		"absent a ctx principal no persatrix-principal header may ride the wire")
}

// TestGRPCMessageDispatcher_PrincipalCoexistsWithSessionAndEpoch pins that
// the third axis stacks beside the other two on one dispatch — the wire
// shape PR 2's producer and the persona-side binder both assume.
func TestGRPCMessageDispatcher_PrincipalCoexistsWithSessionAndEpoch(t *testing.T) {
	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop(), WithEpoch("live"))
	d.dial = dial

	ctx := WithSessionOverride(context.Background(), "run-arc-3")
	ctx = WithPrincipal(ctx, "user-alice")
	require.NoError(t, d.Dispatch(ctx, DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	}, ChannelMessage{ID: "m-1", ChannelID: "group:planning", SenderID: "alice", Timestamp: time.Now().UTC()}))

	assert.Equal(t, []string{"run-arc-3"}, srv.gotMD.Get(grpcmeta.MDSession))
	assert.Equal(t, []string{"live"}, srv.gotMD.Get(grpcmeta.MDEpoch))
	assert.Equal(t, []string{"user-alice"}, srv.gotMD.Get(grpcmeta.MDPrincipal),
		"session, epoch, and principal must all ride one dispatch")
}

// TestGRPCMessageDispatcher_PrincipalPinnedOnSpan pins that the emitted
// principal lands on the channel.dispatch span as `principal.id`
// (low-cardinality-on-span, RFC 0031 OQ #7) — the provenance signal the
// v0.3.14 live MT reads to prove which person a turn served.
func TestGRPCMessageDispatcher_PrincipalPinnedOnSpan(t *testing.T) {
	exporter := installSpanRecorder(t)

	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "agent-b:9090", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())
	d.dial = dial

	require.NoError(t, d.Dispatch(WithPrincipal(context.Background(), "user-alice"),
		DispatchEnvelope{Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways}},
		ChannelMessage{ID: "m-1", ChannelID: "group:planning", SenderID: "alice", Timestamp: time.Now().UTC()}))

	dispatchSpans := filterSpansByName(exporter.GetSpans(), "channel.dispatch")
	require.Len(t, dispatchSpans, 1)
	assert.Equal(t, "user-alice", spanAttrMap(dispatchSpans[0])["principal.id"],
		"the emitted principal must be pinned on the channel.dispatch span")
}

// TestGRPCMessageDispatcher_NoPrincipalNoSpanAttribute completes the
// no-delta pin at the span layer: absent a principal the attribute is
// absent, so pre-activation traces are byte-identical.
func TestGRPCMessageDispatcher_NoPrincipalNoSpanAttribute(t *testing.T) {
	exporter := installSpanRecorder(t)

	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "agent-b:9090", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())
	d.dial = dial

	require.NoError(t, d.Dispatch(context.Background(),
		DispatchEnvelope{Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways}},
		ChannelMessage{ID: "m-1", ChannelID: "group:planning", SenderID: "alice", Timestamp: time.Now().UTC()}))

	dispatchSpans := filterSpansByName(exporter.GetSpans(), "channel.dispatch")
	require.Len(t, dispatchSpans, 1)
	_, present := spanAttrMap(dispatchSpans[0])["principal.id"]
	assert.False(t, present,
		"absent a principal the span must carry no principal.id attribute")
}
