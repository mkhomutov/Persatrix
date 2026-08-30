package channels

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// continuationDispatcher plays a fleet of REAL agents for the RFC 0052
// productive-round continuation seam: every dispatched recipient replies
// WITHIN its floor turn (the floorDispatcher posture), and — mirroring the
// production agent (agents/dispatch.py) — stamps its reply's `cascade_depth`
// as the inbound stimulus depth + 1. The acceptance suite never exercised
// this shape: its 1ms turn timeout made every round all-silent and its
// replies were published manually BETWEEN rounds, so the in-round reply's
// floor-speaker fanout suppression (router_publish_async.go D1) was never
// composed with the autonomous tail — the live-provider stall this file
// pins (ISSUE-0110).
type continuationDispatcher struct {
	router *ChannelRouter

	mu    sync.Mutex
	order []string // recipient dispatch order across ALL rounds
}

func (d *continuationDispatcher) Dispatch(_ context.Context, env DispatchEnvelope, msg ChannelMessage) error {
	rid := env.Recipient.ParticipantID
	if env.InteractionCloseNotification {
		// A real agent ingests a close notification and closes its scope —
		// it never replies (close_notification.py). Recording it would also
		// conflate the close fan with discussion rounds in the assertions.
		return nil
	}
	d.mu.Lock()
	d.order = append(d.order, rid)
	d.mu.Unlock()

	depth := readCascadeDepth(msg.Metadata) + 1
	// The agent's asynchronous REST reply: depth-stamped, open-floor (the
	// production auto-mention targets the stimulus sender; mentions are
	// omitted here so every non-sender member stays a candidate responder —
	// the broadest continuation surface).
	go func() {
		_ = d.router.Publish(context.Background(), ChannelMessage{
			ID:        uuid.NewString(),
			ChannelID: msg.ChannelID,
			SenderID:  rid,
			Content:   rid + " reply",
			Metadata:  map[string]any{cascadeDepthMetadataKey: depth},
		}, "")
	}()
	return nil
}

func (d *continuationDispatcher) snapshot() []string {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]string(nil), d.order...)
}

// continuationHarness arms a floor-controlled 3-persona group. No chair is
// set, so a crossed bound takes the immediate artifact-bearing close
// (synthesisUnavailable) rather than arming a synthesis reply — the
// continuation mechanics under test are identical either way.
func continuationHarness(t *testing.T, autonomous bool, maxRounds int) (*ChannelRouter, *continuationDispatcher, string) {
	t.Helper()
	store := newTestStore(t, SQLiteOptions{})
	disp := &continuationDispatcher{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	disp.router = router

	ch := mustCreateGroupWithPolicies(t, store, "brainstorm", map[string]RespondPolicy{
		"nova": RespondAlways, "ember": RespondAlways, "iron": RespondAlways,
	}, "nova", "ember", "iron")
	router.SetFloorControl(ch, true, 5*time.Second)
	if autonomous {
		router.SetAutonomous(ch, AutonomousConfig{
			Enabled: true, MaxRounds: maxRounds, Convener: "nova",
			Topic: "Should we adopt a monorepo?",
			Goal:  "A synthesized recommendation.",
		})
	}
	return router, disp, ch
}

// TestAutonomousFloorRound_ProductiveRoundContinues pins the RFC 0052 §B
// continuation contract (the ISSUE-0110 live-provider stall): on an armed
// channel, a floor round in which speakers ACTUALLY REPLY must not end the
// discussion — the round's last reply is the next stimulus ("A's post wakes
// B", RFC 0052 §Risk), rounds keep minting, and the `max_rounds` bound
// closes the interaction deterministically.
//
// Pre-fix behaviour (the stall): exactly one round runs (ember, iron), the
// replies' fanout is floor-speaker-suppressed with nothing re-fanning them,
// the stall tail no-ops (the round replied), and the interaction stays open
// forever with the opener's sender (nova) never dispatched at all.
func TestAutonomousFloorRound_ProductiveRoundContinues(t *testing.T) {
	router, disp, ch := continuationHarness(t, true, 3)

	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "nova",
		Content:  "Opening: should we adopt a monorepo?",
		Metadata: map[string]any{cascadeDepthMetadataKey: 1},
	}, ""))
	router.WaitForPendingFanout()

	order := disp.snapshot()
	assert.Greater(t, len(order), 2,
		"a productive round must continue the discussion, not strand it after one round")
	assert.Contains(t, order, "nova",
		"the continuation stimulus (a member reply) must reach the opener's author")

	_, _, tracked := router.openInteractionEscalationState(ch)
	assert.False(t, tracked,
		"the continued discussion must reach the max_rounds bounded close, not stay open forever")
}

// TestHumanFloorRound_NoContinuation pins the human-channel regression bar:
// without `autonomous.enabled` the productive round ends exactly as shipped —
// one round, no re-fanout of in-round replies (the human is the continuation)
// — byte-for-byte the RFC 0030 floor-control contract.
func TestHumanFloorRound_NoContinuation(t *testing.T) {
	router, disp, ch := continuationHarness(t, false, 0)

	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "nova",
		Content:  "kickoff",
		Metadata: map[string]any{cascadeDepthMetadataKey: 1},
	}, ""))
	router.WaitForPendingFanout()

	assert.Equal(t, []string{"ember", "iron"}, disp.snapshot(),
		"a human channel's productive round must not continue autonomously")
	_, _, tracked := router.openInteractionEscalationState(ch)
	assert.True(t, tracked, "the human interaction stays open for the human's next turn")
}

// TestAutonomousContinuation_CascadeCapCloses pins the Layer-0 composition:
// when the would-be continuation stimulus sits at the cascade-depth cap, the
// autonomous path must not bypass the cap (RFC 0052 §Risk) — and with no
// human to continue past it, the discussion has crossed a terminal bound and
// must take the §D structural close (artifact-bearing), never wedge open.
// Also exercises the publishCommit cap-branch Notify fix: the at-cap
// in-round replies still satisfy the floor waiter, so the round advances on
// the reply instead of burning the full turn timeout per speaker.
func TestAutonomousContinuation_CascadeCapCloses(t *testing.T) {
	router, disp, ch := continuationHarness(t, true, 10)
	router.SetMaxCascadeDepth(3)

	start := time.Now()
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "nova",
		Content:  "Opening: should we adopt a monorepo?",
		Metadata: map[string]any{cascadeDepthMetadataKey: 1},
	}, ""))
	router.WaitForPendingFanout()
	elapsed := time.Since(start)

	// Round 1 (stimulus depth 1): ember, iron reply at depth 2 → continuation.
	// Round 2 (stimulus depth 2): nova, ember reply at depth 3 = the cap →
	// their publishes are cap-suppressed; the round still advances via Notify;
	// the tail finds the next stimulus at the cap and closes. No round 3.
	assert.Equal(t, []string{"ember", "iron", "nova", "ember"}, disp.snapshot(),
		"exactly two rounds: the at-cap reply must close, not continue or wedge")

	_, _, tracked := router.openInteractionEscalationState(ch)
	assert.False(t, tracked,
		"an autonomous discussion at the cascade cap must close (structural), not stay open")

	assert.Less(t, elapsed, 4*time.Second,
		"at-cap in-round replies must Notify the floor waiter, not burn per-turn timeouts")
}

// TestAutonomousConcurrentCascadeCap_Closes pins the same terminal bound on
// the CONCURRENT path (floor off): an at-cap publish on an armed channel is
// fanout-suppressed by the commit-path cap branch, and — with no human and no
// further stimulus possible on this chain — must close the interaction rather
// than leave it immortal-but-inert.
//
// The dispatcher is the silent recorder, NOT continuationDispatcher: this
// path's close runs synchronously inside the at-cap Publish, and the test
// scripts both publishes itself. An auto-replying dispatcher re-enters
// Publish on goroutines WaitForPendingFanout cannot see (unlike the floor
// path, where the round awaits each reply via the floor waiter), so on a
// slow runner its unstamped straggler reply — which the no-reopen latch
// cannot suppress — lands after the close, mints a fresh open interaction,
// and flakes the tracked assertion (CI run 30544507713).
func TestAutonomousConcurrentCascadeCap_Closes(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &recordingDispatcher{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ch := mustCreateGroupWithPolicies(t, store, "brainstorm", map[string]RespondPolicy{
		"nova": RespondAlways, "ember": RespondAlways,
	}, "nova", "ember")
	router.SetFloorControl(ch, false, 0)
	router.SetMaxCascadeDepth(3)
	router.SetAutonomous(ch, AutonomousConfig{
		Enabled: true, MaxRounds: 10, Convener: "nova",
		Topic: "monorepo?", Goal: "a recommendation",
	})

	// Mint the interaction with a live sub-cap publish first (§D artifact
	// guarantee: never close before the first live dispatch).
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "nova", Content: "opening",
		Metadata: map[string]any{cascadeDepthMetadataKey: 1},
	}, ""))
	router.WaitForPendingFanout()

	// An at-cap member reply: committed, cap-suppressed — and terminal.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "ember", Content: "final word",
		Metadata: map[string]any{cascadeDepthMetadataKey: 3},
	}, ""))
	router.WaitForPendingFanout()

	_, _, tracked := router.openInteractionEscalationState(ch)
	assert.False(t, tracked,
		"an at-cap publish on an armed concurrent channel must close the interaction")

	// The dispatch record pins both halves of the terminal shape: the opener
	// was the interaction's one LIVE dispatch (the §D artifact guarantee's
	// precondition), the at-cap publish drew NO stimulus dispatch (cap-
	// suppressed), and the close notification — its sole delivery — reached
	// every member, the sender included (bounded-close excludeSender=false).
	var stimuli, closes []string
	for _, c := range disp.snapshot() {
		if c.closeNotification {
			closes = append(closes, c.participantID)
		} else {
			stimuli = append(stimuli, c.participantID)
		}
	}
	assert.Equal(t, []string{"ember"}, stimuli,
		"exactly one live stimulus dispatch: the sub-cap opener; the at-cap publish must not fan")
	assert.ElementsMatch(t, []string{"nova", "ember"}, closes,
		"the structural close notification is the sole delivery and must reach every member")
}
