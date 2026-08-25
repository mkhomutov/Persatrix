package channels

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
	"github.com/mkhomutov/persatrix/internal/registry"
)

// ISSUE-0124 (R-2) PR 1 — the write site, and the dormancy that goes with it.
//
// `Dispatch` is the one place the orchestrator knows both halves of the true
// statement the table holds: which agent it handed a stimulus to, and which
// principal that stimulus descended from. These tests pin that it writes there
// and nowhere else, that it writes only what it can stand behind, and that
// having written it changes nothing on the wire — the whole PR is a producer
// with no consumer until PR 2 lands the re-stamp.

// refusingAgentServer acks the RPC but REFUSES the event — the agent
// servicer's queue-full backpressure and its pre-ingest validation both take
// this shape, and neither ingests the stimulus.
type refusingAgentServer struct {
	taskpb.UnimplementedAgentServiceServer
}

func (r *refusingAgentServer) ReceiveChannelMessage(context.Context, *taskpb.ChannelMessageEvent) (*taskpb.TaskAck, error) {
	return &taskpb.TaskAck{Success: false, ErrorMessage: "queue full"}, nil
}

// dispatchWithAttribution runs one dispatch through a bufconn server with an
// attribution table wired, and returns the table. `expectsReply` is the
// router's responder election ([DispatchEnvelope.ExpectsReply]) — true for a
// turn the orchestrator asked for, false for an ingestion-only delivery.
func dispatchWithAttribution(t *testing.T, ctx context.Context, srv taskpb.AgentServiceServer, expectsReply bool) (*PrincipalAttributionTable, error) {
	t.Helper()
	dial, cleanup := startBufconnServer(t, srv)
	t.Cleanup(cleanup)

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"iron-fox": {ID: "iron-fox", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	table := NewPrincipalAttributionTable()
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop(), WithPrincipalAttribution(table))
	d.dial = dial

	err := d.Dispatch(ctx, DispatchEnvelope{
		Recipient:    Member{ParticipantID: "iron-fox", RespondPolicy: RespondAlways},
		ExpectsReply: expectsReply,
	}, ChannelMessage{ID: "m-1", ChannelID: "group:planning", SenderID: "alice", Timestamp: time.Now().UTC()})
	return table, err
}

// TestDispatch_RecordsCausalAttribution pins the write: a dispatch made under
// an authenticated person's principal records that the recipient agent was
// handed this stimulus under it. The key is the RECIPIENT, not the sender —
// the reply PR 2 will re-stamp comes back from the recipient.
func TestDispatch_RecordsCausalAttribution(t *testing.T) {
	ctx := WithPrincipal(context.Background(), "alice-person")
	table, err := dispatchWithAttribution(t, ctx, &recordingAgentServer{}, true)
	require.NoError(t, err)

	got, ok := table.Lookup("group:planning", "iron-fox")
	require.True(t, ok, "a delivered dispatch under a principal must be recorded")
	assert.Equal(t, "alice-person", got)

	_, ok = table.Lookup("group:planning", "alice")
	assert.False(t, ok, "the table is keyed on the recipient, never the sender")
}

// TestDispatch_NoPrincipalRecordsNothing pins the gate that makes
// `auth.mode: disabled` and every agent/autonomous-origin turn a no-op here:
// with no principal on the context there is no true statement to record, and
// an entry keyed on "" would hand PR 2's re-stamp a hit that means nothing.
func TestDispatch_NoPrincipalRecordsNothing(t *testing.T) {
	table, err := dispatchWithAttribution(t, context.Background(), &recordingAgentServer{}, true)
	require.NoError(t, err)

	assert.Equal(t, 0, table.len(), "an unauthenticated dispatch must record nothing")
}

// TestDispatch_RefusedDeliveryRecordsNothing pins that the record follows the
// stimulus, not the attempt. A refused event was never ingested, so it can
// never cause a reply — recording it would leave a live entry that the
// agent's next UNRELATED publish (an autonomous tick, say) would inherit.
func TestDispatch_RefusedDeliveryRecordsNothing(t *testing.T) {
	ctx := WithPrincipal(context.Background(), "alice-person")
	table, err := dispatchWithAttribution(t, ctx, &refusingAgentServer{}, true)
	require.Error(t, err, "a refused ack is not a delivery")
	assert.True(t, errors.Is(err, ErrDeliveryRefused))

	assert.Equal(t, 0, table.len(), "a stimulus the agent refused must not be attributed to anyone")
}

// TestDispatch_UnregisteredRecipientRecordsNothing pins the same rule on the
// other miss path: the message was dropped before the wire, so no agent holds
// a stimulus to reply to.
func TestDispatch_UnregisteredRecipientRecordsNothing(t *testing.T) {
	dial, cleanup := startBufconnServer(t, &recordingAgentServer{})
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{}}
	table := NewPrincipalAttributionTable()
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop(), WithPrincipalAttribution(table))
	d.dial = dial

	ctx := WithPrincipal(context.Background(), "alice-person")
	err := d.Dispatch(ctx, DispatchEnvelope{
		Recipient:    Member{ParticipantID: "ghost-agent", RespondPolicy: RespondAlways},
		ExpectsReply: true,
	}, ChannelMessage{ID: "m-1", ChannelID: "group:planning", SenderID: "alice", Timestamp: time.Now().UTC()})
	require.Error(t, err)
	assert.True(t, errors.Is(err, registry.ErrAgentNotFound))

	assert.Equal(t, 0, table.len(), "a dispatch that never reached an agent must record nothing")
}

// TestDispatch_WithoutAttributionTableIsSafe pins the nil wiring: a
// channels-disabled or partially-wired deployment has no table, and the
// dispatch path must not care.
func TestDispatch_WithoutAttributionTableIsSafe(t *testing.T) {
	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"iron-fox": {ID: "iron-fox", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())
	d.dial = dial

	ctx := WithPrincipal(context.Background(), "alice-person")
	require.NoError(t, d.Dispatch(ctx, DispatchEnvelope{
		Recipient:    Member{ParticipantID: "iron-fox", RespondPolicy: RespondAlways},
		ExpectsReply: true,
	}, ChannelMessage{ID: "m-1", ChannelID: "group:planning", SenderID: "alice", Timestamp: time.Now().UTC()}))
}

// TestDispatch_AttributionIsDormantOnTheWire is the PR's no-delta pin, and the
// reason it is worth writing rather than assuming: the table is server-held
// state whose whole purpose is to be read later, so the only thing that could
// make this PR observable is a leak of it onto the wire. A dispatch made with
// a POPULATED table must send byte-identical metadata to one made with no
// table at all.
func TestDispatch_AttributionIsDormantOnTheWire(t *testing.T) {
	// One fixed timestamp for both dispatches: the payload carries it
	// verbatim, so a per-call time.Now() would make the two events differ for
	// a reason that has nothing to do with the table.
	sentAt := time.Date(2026, 8, 25, 12, 0, 0, 0, time.UTC)

	dispatch := func(t *testing.T, table *PrincipalAttributionTable) *recordingAgentServer {
		t.Helper()
		srv := &recordingAgentServer{}
		dial, cleanup := startBufconnServer(t, srv)
		t.Cleanup(cleanup)

		resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
			"iron-fox": {ID: "iron-fox", Address: "ignored:0", Status: registry.StatusHealthy},
		}}
		var opts []DispatcherOption
		if table != nil {
			opts = append(opts, WithPrincipalAttribution(table))
		}
		d := NewGRPCMessageDispatcher(resolver, zap.NewNop(), opts...)
		d.dial = dial

		ctx := WithPrincipal(context.Background(), "alice-person")
		require.NoError(t, d.Dispatch(ctx, DispatchEnvelope{
			Recipient:    Member{ParticipantID: "iron-fox", RespondPolicy: RespondAlways},
			ExpectsReply: true,
		}, ChannelMessage{ID: "m-1", ChannelID: "group:planning", SenderID: "alice", Timestamp: sentAt}))
		return srv
	}

	// A table that already holds an entry for this exact (channel, agent)
	// pair — the state PR 2 will read — under a DIFFERENT principal, so a
	// leak would be visible as a changed value rather than only a changed
	// key set.
	populated := NewPrincipalAttributionTable()
	populated.Record("group:planning", "iron-fox", "bob-person")

	withTable := dispatch(t, populated)
	withoutTable := dispatch(t, nil)

	assert.Equal(t, withoutTable.gotMD, withTable.gotMD,
		"a populated attribution table must not change one byte of the outbound metadata")
	assert.Equal(t, withoutTable.gotEvent.String(), withTable.gotEvent.String(),
		"nor one byte of the event payload")
}

// TestDispatch_IngestionOnlyDeliveryRecordsNothing pins the rule that delivery
// alone is not causation. The ack is PRE-INGEST — `ReceiveChannelMessage`
// returns as soon as the wake is accepted and the response gate runs later,
// inside the event loop — and the router deliberately delivers to members that
// gate will silence, so they ingest the room rather than going amnesiac. Such
// a member holds the stimulus and never answers it, so an entry for it would
// be inherited by its next, unrelated publish: an autonomous tick attributed
// to a person who never caused it.
func TestDispatch_IngestionOnlyDeliveryRecordsNothing(t *testing.T) {
	ctx := WithPrincipal(context.Background(), "alice-person")
	table, err := dispatchWithAttribution(t, ctx, &recordingAgentServer{}, false)
	require.NoError(t, err, "an ingestion-only delivery is still a delivery")

	assert.Equal(t, 0, table.len(),
		"a stimulus the router never asked a turn for must not be attributed to anyone")
}
