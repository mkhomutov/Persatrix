package channels

// bounded_close_revival_test.go — the RFC 0052 bounded-close REVIVAL
// regressions: on a bound-crossing round, neither the chair-stall escalation
// nor the ISSUE-0099 resynthesize re-force may dispatch a turn whose reply
// would revive the terminated discussion. Split out of bounded_close_test.go
// when PR 4b-ii's close-on-reply updates pushed it past the 500-line review
// cap (`scripts/checks/file_size.py --strict`); these two are the tests the
// 4b-ii ordering changed the most (a chaired bound now ARMS the synthesis
// close instead of closing inline), so they carry the new full arc.

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// TestBoundedClose_SuppressesResynthesizeOnBoundingRound is the regression for
// the THIRD reopen vector (the sibling of the escalation- and concurrent-path
// fixes): when the bounding round is the chair's own MISFIRED forced-turn reply,
// the ISSUE-0099 resynthesize must NOT re-force a synthesize-only chair turn. A
// resynthesize forced turn is dispatched off any floor round, so its reply
// re-enters Publish and — with the id already retired by the close — mints a
// FRESH interaction, reopening the just-closed discussion. PR 4a makes the
// escalation chair MANDATORY on every armed channel, so this path is live: a
// stall escalates, the chair hands off to the RespondNever operator (a routine
// misfire the §D framing invites), and that reply both crosses the bound and
// would re-force. The fix gates the re-force DISPATCH on the bounded-close
// outcome (the CLAIM still runs at the fanout head — review round 5 — so the
// once-bound keeps its first-publish ordering; a bounding round consumes the
// arm and drops the pending re-force).
//
// Discriminating: pre-fix the chair's reply (round 2) produces a resynthesize
// dispatch regardless of the close outcome; the fix leaves none.
func TestBoundedClose_SuppressesResynthesizeOnBoundingRound(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &envelopeRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ch := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"alex":         RespondNever,  // stimulus author + the misfire hand-off target
			"nova-sparrow": RespondAlways, // the escalation chair
			"ember-owl":    RespondAlways,
			"iron-fox":     RespondAlways,
		}, "alex", "nova-sparrow", "ember-owl", "iron-fox")
	router.SetFloorControl(ch, true, time.Millisecond)
	router.SetEscalationChair(ch, "nova-sparrow")
	router.SetAutonomous(ch, AutonomousConfig{Enabled: true, MaxRounds: 2, Convener: "ember-owl"})

	// Round 1: a stalled floor round (recorder never replies) escalates → forced
	// turn to the chair, arming the ISSUE-0099 stash. Sub-bound (round 1 < 2).
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "alex", Content: "thoughts?",
	}, ""))
	router.WaitForPendingFanout()
	forced := 0
	for _, env := range disp.snapshot() {
		if env.ChairEscalation && !env.ChairEscalationResynthesize {
			forced++
		}
	}
	require.Equal(t, 1, forced, "round 1 escalates and arms the resynthesize stash")

	// Round 2 (round 2 == max_rounds, the bounding round): the chair's reply
	// misfires — it @-mentions only the RespondNever operator, so its floor-mention
	// subset is empty. It crosses the bound; the resynthesize must be suppressed.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "nova-sparrow",
		Content: "alex, your call?", Mentions: []string{"alex"},
	}, ""))
	router.WaitForPendingFanout()

	assert.Empty(t, resynthesizeEnvelopes(disp),
		"the bounding round terminated the discussion — no resynthesize forced turn to reopen it")
	// PR 4b-ii: on a chaired channel the bounding round no longer closes
	// immediately — it arms the close-on-reply and dispatches the synthesis
	// turn; the chair's claimed reply is what retires the id.
	openID, _, tracked := router.openInteractionEscalationState(ch)
	require.True(t, tracked, "armed: the close waits for the chair's synthesis reply")
	synthTurns := 0
	for _, env := range disp.snapshot() {
		if env.SynthesisTurn {
			synthTurns++
		}
	}
	assert.Equal(t, 1, synthTurns, "the bounding round dispatched the synthesis turn instead")
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "nova-sparrow",
		Content: "Synthesis: no consensus reached.",
		// The persona reply-echo pair (PR #718 review): the id claim plus the
		// synthesis_reply marker — the claim's discriminating conjunct.
		Metadata: map[string]any{
			"interaction_id":          openID,
			synthesisReplyMetadataKey: true,
		},
	}, ""))
	router.WaitForPendingFanout()
	assert.Empty(t, resynthesizeEnvelopes(disp),
		"the synthesis reply closes — still no resynthesize re-force")
	_, _, tracked = router.openInteractionEscalationState(ch)
	assert.False(t, tracked, "the chair's synthesis reply closed the interaction and retired its id")
}

// TestBoundedClose_SuppressesEscalationOnBoundingRound pins the deep-review
// ordering fix: when the bounding round is also a stall, the bounded close runs
// BEFORE the chair-stall escalation and retires the id, so the escalation tail
// no-ops. Otherwise the forced chair turn is dispatched onto a closing
// interaction and its reply would mint a FRESH interaction, reopening the
// discussion the close just terminated.
func TestBoundedClose_SuppressesEscalationOnBoundingRound(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &envelopeRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ch := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"operator":     RespondNever, // stimulus author
			"ember-owl":    RespondAlways,
			"nova-sparrow": RespondAlways, // the escalation chair
		}, "operator", "ember-owl", "nova-sparrow")
	router.SetFloorControl(ch, true, time.Millisecond)
	router.SetEscalationChair(ch, "nova-sparrow")
	router.SetAutonomous(ch, AutonomousConfig{Enabled: true, MaxRounds: 1, Convener: "ember-owl"})

	// Personas never reply (recorder swallows) → the round stalls, and it is the
	// bounding round (max_rounds=1). The close must win the tail.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "operator", Content: "thoughts?",
	}, ""))
	router.WaitForPendingFanout()

	// PR 4b-ii: with a chair configured the bounding round arms the
	// close-on-reply (the synthesis turn, its OWN marker lane) instead of
	// closing inline; the stall-escalation suppression this test pins is
	// unchanged — no ChairEscalation-marked dispatch may follow the bound.
	openID, _, tracked := router.openInteractionEscalationState(ch)
	require.True(t, tracked, "armed: the close waits for the chair's synthesis reply")
	for _, env := range disp.snapshot() {
		assert.False(t, env.ChairEscalation,
			"no forced chair turn on the bounding round — the bound terminated the discussion first")
	}
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "nova-sparrow",
		Content: "Synthesis: closing.",
		// The persona reply-echo pair (PR #718 review): the id claim plus the
		// synthesis_reply marker — the claim's discriminating conjunct.
		Metadata: map[string]any{
			"interaction_id":          openID,
			synthesisReplyMetadataKey: true,
		},
	}, ""))
	router.WaitForPendingFanout()
	_, _, tracked = router.openInteractionEscalationState(ch)
	assert.False(t, tracked, "the chair's synthesis reply closes the interaction")
	for _, env := range disp.snapshot() {
		assert.False(t, env.ChairEscalation,
			"still no forced chair turn after the close — the reply closes, never revives")
	}
}
