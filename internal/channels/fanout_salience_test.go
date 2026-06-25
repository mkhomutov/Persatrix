package channels

// RFC 0030 Tier B (v0.3.8) PR 2b — the fanout→envelope half of the wire. The
// grpc_dispatcher_salience_test.go sibling proves channelMessageToProto renders a
// hand-built envelope; this file closes the one remaining untested seam between
// "cap resolved on the router" and "envelope stamped" — that fanout captures
// the channel's member count and stamps the router-resolved cap onto every
// dispatch (a per-publish value identical across recipients).

import (
	"context"
	"sync"
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// envelopeRecorder is a MessageDispatcher that captures the DispatchEnvelope of
// every fanout call. Kept local to this file so the shared recordingDispatcher
// (router_test.go) need not grow Tier B fields the rest of its callers ignore.
type envelopeRecorder struct {
	mu    sync.Mutex
	calls []DispatchEnvelope
}

func (d *envelopeRecorder) Dispatch(_ context.Context, env DispatchEnvelope, _ ChannelMessage) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.calls = append(d.calls, env)
	return nil
}

func (d *envelopeRecorder) snapshot() []DispatchEnvelope {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]DispatchEnvelope, len(d.calls))
	copy(out, d.calls)
	return out
}

// TestFanout_StampsChannelSizeAndCapOnEnvelope pins that fanout stamps the
// per-publish Tier B inputs onto every dispatch envelope:
//
//   - ChannelSize is the channel's *total* member count (every membership row,
//     including the sender), not the filtered candidate-responder set — the
//     contract the `channel_size` proto comment documents. A 3-member channel
//     fans out to 2 recipients but each still sees ChannelSize == 3.
//   - SalienceMaxChannelMembers is whatever the router resolved for the channel
//     (here an explicit non-default cap), identical across recipients.
func TestFanout_StampsChannelSizeAndCapOnEnvelope(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &envelopeRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ctx := context.Background()

	id := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"alice": RespondParticipant, // sender; salience-gated
			"bob":   RespondParticipant,
			"carol": RespondAlways, // legacy always; not gated
		}, "alice", "bob", "carol")

	// Resolve a non-default cap so the assertion proves the router value flows
	// through, not just the DefaultSalienceMaxChannelMembers fallback.
	router.SetSalienceMaxChannelMembers(id, 7)

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "hi",
	}, ""))

	calls := disp.snapshot()
	require.Len(t, calls, 2, "fanout to bob+carol, sender filtered")
	byID := map[string]DispatchEnvelope{}
	for _, c := range calls {
		byID[c.Recipient.ParticipantID] = c
		assert.Equal(t, 3, c.ChannelSize,
			"channel_size is the total member count (sender included), identical across recipients")
		assert.Equal(t, 7, c.SalienceMaxChannelMembers,
			"the router-resolved cap rides every dispatch")
	}
	// And the per-recipient bid signals ride the recipient's membership row, so
	// the gated participant and the legacy always recipient differ on the wire.
	assert.True(t, byID["bob"].Recipient.SalienceGated, "participant recipient is salience-gated")
	assert.False(t, byID["carol"].Recipient.SalienceGated, "legacy always recipient is not")
}

// TestFanout_StampsDefaultCapWhenUnresolved pins that a channel with no
// explicitly resolved cap still gets a sensible positive value on the wire —
// the agent-side default — rather than a zero that the seam would read as
// "unknown / cap disabled".
func TestFanout_StampsDefaultCapWhenUnresolved(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &envelopeRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ctx := context.Background()

	// No SetSalienceMaxChannelMembers / ResolveSalienceCaps call for this channel.
	id := mustCreateGroup(t, store, "planning", "alice", "bob")

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "hi",
	}, ""))

	calls := disp.snapshot()
	require.Len(t, calls, 1, "fanout to bob")
	assert.Equal(t, DefaultSalienceMaxChannelMembers, calls[0].SalienceMaxChannelMembers,
		"an unresolved channel falls back to the default cap, never zero")
	assert.Equal(t, 2, calls[0].ChannelSize)
}

// TestFanout_StampsResolvedReasoningModeOnEnvelope is the RFC 0051 PR 6 go-live
// end-to-end: fanout stamps the channel's router-resolved reasoning rung onto
// every dispatch envelope, so the (flip-aware) ReasoningFor value reaches the
// agent-side seam. A governed channel resolved through ResolveReasoning carries
// the bid default; without it the agent never leaves the dark `off` scalar gate.
func TestFanout_StampsResolvedReasoningModeOnEnvelope(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &envelopeRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ctx := context.Background()

	id := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"alice": RespondParticipant, // sender; salience-gated → governed
			"bob":   RespondParticipant,
		}, "alice", "bob")

	// Boot resolve: a governed channel flips to the bid default on the router.
	require.NoError(t, router.ResolveReasoning(ctx, &Config{}))

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "hi",
	}, ""))

	calls := disp.snapshot()
	require.Len(t, calls, 1, "fanout to bob")
	assert.Equal(t, ReasoningModeBid, calls[0].ReasoningMode,
		"the resolved governed default (bid) is stamped onto the dispatch envelope")
}

// TestFanout_StampsResolvedReasoningReviseOnEnvelope is the RFC 0051 PR 8
// (Phase 5a) wire end-to-end: fanout stamps the channel's router-resolved
// reflexion round count onto every dispatch envelope, so an operator's `plan` +
// `revise` opt-in reaches the agent-side reflexion loop.
func TestFanout_StampsResolvedReasoningReviseOnEnvelope(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &envelopeRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ctx := context.Background()

	id := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"alice": RespondParticipant, // sender; salience-gated → governed
			"bob":   RespondParticipant,
		}, "alice", "bob")
	require.NoError(t, router.ResolveReasoning(ctx, &Config{}))

	// Promote to the plan rung with 2 reflexion rounds (the operator opt-in).
	router.SetReasoning(id, ReasoningConfig{
		Mode: ReasoningModePlan, Model: ReasoningModelFast,
		Depth: ReasoningDepthShallow, Revise: 2,
	})

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "hi",
	}, ""))

	calls := disp.snapshot()
	require.Len(t, calls, 1, "fanout to bob")
	assert.Equal(t, 2, calls[0].ReasoningRevise,
		"the resolved revise count is stamped onto the dispatch envelope")
}
