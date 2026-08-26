package channels

import (
	"context"
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// ISSUE-0124 (R-2) PR 1 — the responder election reaching the envelope.
//
// The causal-attribution table records a stimulus only for a recipient the
// router ELECTED to take a turn, not for every recipient a dispatch reached.
// The two sets are genuinely different: [ChannelRouter.dispatchConcurrent]
// delivers to members whose reply the agent-side gate will suppress, so they
// ingest the message into memory without ever answering it (fanout.go's
// "un-addressed participants amnesiac" note). An entry for one of those would
// stay live for a full turn budget and be inherited by whatever that agent
// published next.
//
// [orderResponders] is the orchestrator's own superset of the receiver gate's
// respond-true set, maintained in lockstep with agents/response_gate.py and
// already used for the RFC 0048 presence signal ("the members orderResponders
// expects to reply, NOT the ingestion-only recipients"). These tests pin that
// the same election reaches [DispatchEnvelope.ExpectsReply].

// callFor finds the dispatch to one participant.
func callFor(t *testing.T, calls []dispatchCall, participantID string) dispatchCall {
	t.Helper()
	for _, c := range calls {
		if c.participantID == participantID {
			return c
		}
	}
	t.Fatalf("no dispatch recorded for %q", participantID)
	return dispatchCall{}
}

// TestFanout_UnmentionedWhenMentionedMemberIsIngestionOnly pins the
// `not_mentioned` class. A `when_mentioned` member still receives the
// dispatch — it must ingest the room's history — but the gate suppresses its
// reply, so the router must not claim a turn was asked of it.
func TestFanout_UnmentionedWhenMentionedMemberIsIngestionOnly(t *testing.T) {
	router, disp, store := newRouterTest(t)
	ctx := context.Background()
	id := "group:planning"
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: id, Name: "planning", Type: ChannelTypeGroup,
	}))
	require.NoError(t, store.AddMember(ctx, id, "alice", RespondAlways))
	require.NoError(t, store.AddMember(ctx, id, "iron-fox", RespondAlways))
	require.NoError(t, store.AddMember(ctx, id, "nova-sparrow", RespondWhenMentioned))

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "open floor",
	}, ""))

	calls := disp.snapshot()
	require.Len(t, calls, 2, "both non-sender members receive the message")

	assert.True(t, callFor(t, calls, "iron-fox").expectsReply,
		"an `always` member on an open floor is a responder")
	assert.False(t, callFor(t, calls, "nova-sparrow").expectsReply,
		"an unmentioned `when_mentioned` member is delivered to for ingestion only")
}

// TestFanout_DirectedElsewhereMemberIsIngestionOnly pins the
// `directed_elsewhere` class — the one fanout.go calls out by name. A message
// naming iron-fox is directed, so ember-owl stays silent despite `always`,
// but is still dispatched so it does not go amnesiac about the room.
func TestFanout_DirectedElsewhereMemberIsIngestionOnly(t *testing.T) {
	router, disp, store := newRouterTest(t)
	ctx := context.Background()
	id := "group:planning"
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: id, Name: "planning", Type: ChannelTypeGroup,
	}))
	require.NoError(t, store.AddMember(ctx, id, "alice", RespondAlways))
	require.NoError(t, store.AddMember(ctx, id, "iron-fox", RespondAlways))
	require.NoError(t, store.AddMember(ctx, id, "ember-owl", RespondAlways))

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
		Content: "@iron-fox thoughts?", Mentions: []string{"iron-fox"},
	}, ""))

	calls := disp.snapshot()
	require.Len(t, calls, 2)

	assert.True(t, callFor(t, calls, "iron-fox").expectsReply,
		"the named member is the responder")
	assert.False(t, callFor(t, calls, "ember-owl").expectsReply,
		"a directed-elsewhere `always` member is delivered to for ingestion only")
}

// TestFanout_CloseNotificationExpectsNoReply pins the control-fan side of the
// same rule. The four orchestrator-authored FORCED turns (chair escalation,
// its resynthesize refinement, convene, synthesis) exist precisely to draw a
// reply; the close notification is the one that does not, so it must never
// leave an entry behind on every member of a closing room.
func TestFanout_CloseNotificationExpectsNoReply(t *testing.T) {
	assert.False(t, dispatchControl{marker: markerCloseNotification}.expectsReply(),
		"a close notification is told, not asked")

	for _, m := range []dispatchMarker{
		markerChairEscalation,
		markerChairEscalationResynthesize,
		markerConvene,
		markerSynthesisTurn,
	} {
		assert.True(t, dispatchControl{marker: m}.expectsReply(),
			"a forced turn is a turn the orchestrator asked for (marker %d)", m)
	}

	assert.False(t, dispatchControl{}.expectsReply(),
		"an ordinary dispatch is ingestion-only unless the router elected the recipient")
	assert.True(t, dispatchControl{respondersTurn: true}.expectsReply(),
		"an ordinary dispatch to an elected responder is a turn")
}
