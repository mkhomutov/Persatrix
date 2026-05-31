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

// ISSUE-0085 PR 5 — `--epoch` override on the dispatch path.
//
// PR 4 emits the boot-resolved process epoch ([WithEpoch]) on every dispatch.
// These tests pin the PR-5 reconciliation: an explicit per-request override
// (threaded onto ctx by the REST handler — [WithEpochOverride]) takes
// precedence above the boot epoch for the one request it accompanies, mirroring
// the `--session` override (grpc_dispatcher_session_test.go). They reuse the
// package's recordingAgentServer / startBufconnServer / stubResolver helpers.

// TestGRPCMessageDispatcher_EpochOverrideBeatsBootEpoch pins that an explicit
// override on the context emits as `persatrix-epoch`, not the boot epoch.
func TestGRPCMessageDispatcher_EpochOverrideBeatsBootEpoch(t *testing.T) {
	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop(), WithEpoch("live"))
	d.dial = dial

	ctx := WithEpochOverride(context.Background(), "ci-run-5")
	require.NoError(t, d.Dispatch(ctx, DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	}, ChannelMessage{ID: "m-1", ChannelID: "group:planning", SenderID: "alice", Timestamp: time.Now().UTC()}))

	assert.Equal(t, []string{"ci-run-5"}, srv.gotMD.Get(grpcmeta.MDEpoch),
		"an explicit --epoch override must beat the boot epoch for this request")
}

// TestGRPCMessageDispatcher_EpochOverrideEmittedWithoutBootEpoch pins that the
// override is honoured even when no boot epoch is wired — the override path
// reads the context directly and does not depend on WithEpoch.
func TestGRPCMessageDispatcher_EpochOverrideEmittedWithoutBootEpoch(t *testing.T) {
	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop()) // no WithEpoch
	d.dial = dial

	ctx := WithEpochOverride(context.Background(), "ci-run-5")
	require.NoError(t, d.Dispatch(ctx, DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	}, ChannelMessage{ID: "m-1", ChannelID: "group:planning", SenderID: "alice", Timestamp: time.Now().UTC()}))

	assert.Equal(t, []string{"ci-run-5"}, srv.gotMD.Get(grpcmeta.MDEpoch),
		"the override must emit even with no boot epoch wired")
}

// TestGRPCMessageDispatcher_NoEpochOverrideKeepsBootEpoch pins the
// no-regression half: absent an override, the boot epoch is emitted unchanged
// (PR 4 behaviour preserved).
func TestGRPCMessageDispatcher_NoEpochOverrideKeepsBootEpoch(t *testing.T) {
	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop(), WithEpoch("live"))
	d.dial = dial

	require.NoError(t, d.Dispatch(context.Background(), DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	}, ChannelMessage{ID: "m-1", ChannelID: "group:planning", SenderID: "alice", Timestamp: time.Now().UTC()}))

	assert.Equal(t, []string{"live"}, srv.gotMD.Get(grpcmeta.MDEpoch),
		"absent an override the boot epoch must be emitted unchanged (no PR-4 regression)")
}

// TestGRPCMessageDispatcher_EpochOverridePinnedOnSpan pins that the override id
// — like the boot epoch — lands on the channel.dispatch span as `epoch.id`
// (RFC 0031 OQ #7), so trace correlation works regardless of which path
// supplied the epoch.
func TestGRPCMessageDispatcher_EpochOverridePinnedOnSpan(t *testing.T) {
	exporter := installSpanRecorder(t)

	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "agent-b:9090", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop(), WithEpoch("live"))
	d.dial = dial

	require.NoError(t, d.Dispatch(WithEpochOverride(context.Background(), "ci-run-5"),
		DispatchEnvelope{Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways}},
		ChannelMessage{ID: "m-1", ChannelID: "group:planning", SenderID: "alice", Timestamp: time.Now().UTC()}))

	dispatchSpans := filterSpansByName(exporter.GetSpans(), "channel.dispatch")
	require.Len(t, dispatchSpans, 1)
	assert.Equal(t, "ci-run-5", spanAttrMap(dispatchSpans[0])["epoch.id"],
		"the override id must be pinned on the channel.dispatch span")
}
