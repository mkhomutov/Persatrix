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

// ISSUE-0085 PR 4 — `persatrix-epoch` emission on the dispatch path.
//
// These tests live in their own file (not grpc_dispatcher_test.go) only to
// keep each file under the review-friendly size limit; they exercise the
// same [GRPCMessageDispatcher] and reuse its `recordingAgentServer`
// (whose `gotMD` captures the incoming gRPC metadata the receiver observed)
// and `startBufconnServer` helpers.

// TestGRPCMessageDispatcher_EmitsEpochHeader pins the ISSUE-0085 PR 4
// activation: when a boot epoch is wired (WithEpoch), Dispatch emits it as
// the `persatrix-epoch` gRPC header on every dispatch so the persona side
// re-establishes the epoch_scope the run-isolation filter consumes. Unlike
// the session id, the epoch is a single process-global value (not per-room),
// so it does not vary by (agent, channel). The wire key MUST match
// grpcmeta.MDEpoch (the cross-language contract with
// agents.epoch_id.EPOCH_METADATA_GRPC_KEY).
func TestGRPCMessageDispatcher_EmitsEpochHeader(t *testing.T) {
	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop(), WithEpoch("ci-job-1234"))
	d.dial = dial

	msg := ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a",
		Content: "hi", Timestamp: time.Now().UTC(),
	}
	require.NoError(t, d.Dispatch(context.Background(), DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondWhenMentioned},
	}, msg))

	require.NotNil(t, srv.gotMD, "receiver must observe incoming metadata")
	assert.Equal(t, []string{"ci-job-1234"}, srv.gotMD.Get(grpcmeta.MDEpoch),
		"persatrix-epoch must carry the boot-resolved process epoch")
}

// TestGRPCMessageDispatcher_EpochIndependentOfChannel pins that the epoch is
// process-global, not per-room: two dispatches to distinct channels carry the
// SAME epoch header (contrast the session id, which varies by (agent, channel)).
func TestGRPCMessageDispatcher_EpochIndependentOfChannel(t *testing.T) {
	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop(), WithEpoch("run-7"))
	d.dial = dial

	base := DispatchEnvelope{Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways}}

	require.NoError(t, d.Dispatch(context.Background(), base, ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "alice", Timestamp: time.Now().UTC(),
	}))
	first := srv.gotMD.Get(grpcmeta.MDEpoch)

	require.NoError(t, d.Dispatch(context.Background(), base, ChannelMessage{
		ID: "m-2", ChannelID: "dm:c:b", SenderID: "carol", Timestamp: time.Now().UTC(),
	}))
	second := srv.gotMD.Get(grpcmeta.MDEpoch)

	require.Equal(t, []string{"run-7"}, first)
	assert.Equal(t, first, second,
		"the epoch is process-global — it must not vary by channel")
}

// TestGRPCMessageDispatcher_NoEpochEmitsNoEpochHeader pins the no-emission
// default: a dispatcher built without WithEpoch (the channels-disabled /
// pre-wiring path and the unit tests that do not exercise emission) ships no
// epoch header, so behaviour is byte-identical to the pre-ISSUE-0085 dispatch.
func TestGRPCMessageDispatcher_NoEpochEmitsNoEpochHeader(t *testing.T) {
	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop()) // no WithEpoch
	d.dial = dial

	require.NoError(t, d.Dispatch(context.Background(), DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	}, ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a", Timestamp: time.Now().UTC(),
	}))

	require.NotNil(t, srv.gotEvent)
	assert.Empty(t, srv.gotMD.Get(grpcmeta.MDEpoch),
		"no epoch wired → no persatrix-epoch header emitted")
}

// TestGRPCMessageDispatcher_EpochPinnedOnSpan pins the trace-correlation half
// of the emission: the resolved epoch is attached to the `channel.dispatch`
// span as `epoch.id` (low-cardinality-on-span, never a metric label — the
// RFC 0031 OQ #7 posture the session id already follows) so an operator can
// pivot a trace to the logical run / branch it served.
func TestGRPCMessageDispatcher_EpochPinnedOnSpan(t *testing.T) {
	exporter := installSpanRecorder(t)

	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "agent-b:9090", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop(), WithEpoch("epoch-on-span"))
	d.dial = dial

	require.NoError(t, d.Dispatch(context.Background(), DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	}, ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a", Timestamp: time.Now().UTC(),
	}))

	dispatchSpans := filterSpansByName(exporter.GetSpans(), "channel.dispatch")
	require.Len(t, dispatchSpans, 1)
	assert.Equal(t, "epoch-on-span", spanAttrMap(dispatchSpans[0])["epoch.id"],
		"resolved epoch must be pinned on the channel.dispatch span for trace correlation")
}
