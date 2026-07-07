package channels

// synthesis_close_races_test.go — PR #718 follow-up review regressions: the
// interleavings AROUND the armed synthesis close. The claim runs on the COMMIT
// path before the end-vote hook (a reply cast AS a vote — the shape the
// directive invites — must neither be spam-suppressed into the timeout net nor
// relabelled `end_votes` off the metered wire shape); the consumed arm keeps
// withholding through the claim→tombstone teardown gap; a disable landing
// mid-arc never lets the bound (or the timeout net) force-close a channel the
// operator took manual control of; the shutdown drain cannot be out-raced by
// an in-flight fanout arming behind its sweep; and every disarm terminal —
// the fresh-mint reset included — releases the timer's synthesisWG count.

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

// chairVote publishes the chair's synthesis reply cast AS an end vote — the
// outcome-(a) shape end_vote_action.py explicitly stamps the reply echo on:
// the interaction-id claim, the `synthesis_reply` marker, AND the
// `end_interaction_vote` flag on one publish.
func chairVote(t *testing.T, router *ChannelRouter, ch, chairID, interactionID, content string) {
	t.Helper()
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: chairID, Content: content,
		Metadata: map[string]any{
			"interaction_id":          interactionID,
			synthesisReplyMetadataKey: true,
			endVoteMetadataKey:        true,
		},
	}, ""))
}

// memberVote publishes an ordinary end vote (no synthesis marker) from a
// non-chair member.
func memberVote(t *testing.T, router *ChannelRouter, ch, senderID, interactionID string) {
	t.Helper()
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: senderID, Content: "done here",
		Metadata: map[string]any{
			"interaction_id":   interactionID,
			endVoteMetadataKey: true,
		},
	}, ""))
}

// TestSynthesisClose_ReplyAsInWindowDuplicateVoteStillCloses — the chair holds
// a LIVE in-window end vote when the bound arms, then answers the directive
// with another vote (the duplicate-vote shape). processEndVote used to consume
// it first and suppress its fanout as spam, so the head claim never ran: the
// arm burned the full timeout and the close carried the bounding stimulus
// instead of the artifact. The commit-path claim now runs BEFORE the end-vote
// hook, so the reply closes the interaction with the synthesis as the closing
// message.
func TestSynthesisClose_ReplyAsInWindowDuplicateVoteStillCloses(t *testing.T) {
	router, disp, ch, reader := synthesisCloseHarness(t, 3)
	router.synthesisTimeout = time.Hour // a timeout fallback would hang, not pass

	tick(t, router, ch) // round 1
	openID, _, tracked := router.openInteractionEscalationState(ch)
	require.True(t, tracked)
	chairVote(t, router, ch, "iron-fox", openID, "leaning done") // live vote, round 2
	tick(t, router, ch)                                          // round 3 = bound → arm
	require.Len(t, disp.synthesisTurns(), 1, "the bound armed and dispatched the synthesis turn")

	// The synthesis reply, cast as a vote — an in-window duplicate for the
	// chair (W default 3; its live vote is 2 turns old).
	chairVote(t, router, ch, "iron-fox", openID, "Synthesis: ship the plan as scoped.")
	router.WaitForPendingFanout()

	assert.Equal(t, int64(1), closedCount(t, reader, structuralTrigger),
		"the reply closes as the bounded close — never spam-suppressed into the timeout net")
	assert.Equal(t, int64(1), synthesisTurnCount(t, reader, "closed_on_reply"))
	notifications := disp.closeNotifications()
	require.NotEmpty(t, notifications, "the close fans the artifact notification")
	for _, c := range notifications {
		assert.Contains(t, c.msg.Content, "Synthesis: ship the plan",
			"the notification carries the synthesis vote — the §D artifact, not the bounding stimulus")
	}
	_, _, stillTracked := router.openInteractionEscalationState(ch)
	assert.False(t, stillTracked, "the id is retired on the reply, not the timer")
}

// TestSynthesisClose_QuorumCompletingSynthesisVoteKeepsMeteredClose — the
// chair's synthesis vote would COMPLETE the end-vote quorum (K=2: one member
// vote is already live). processEndVote used to win and close as `end_votes`
// — the unmetered wire shape: no close trigger on the notification, so every
// RFC 0020 summary of a bound-crossed arc silently skipped its OQ #6 lease,
// decided by whether some other member happened to hold a live vote. The
// commit-path claim now keys the close to the ARMED trigger: same reply, same
// metered close, quorum or no quorum.
func TestSynthesisClose_QuorumCompletingSynthesisVoteKeepsMeteredClose(t *testing.T) {
	router, disp, ch, reader := synthesisCloseHarness(t, 3)
	router.synthesisTimeout = time.Hour

	tick(t, router, ch) // round 1
	openID, _, _ := router.openInteractionEscalationState(ch)
	memberVote(t, router, ch, "ember-owl", openID) // 1 of K=2, round 2
	tick(t, router, ch)                            // round 3 = bound → arm
	require.Len(t, disp.synthesisTurns(), 1)

	chairVote(t, router, ch, "iron-fox", openID, "Synthesis: consensus reached; adopt it.")
	router.WaitForPendingFanout()

	assert.Equal(t, int64(1), closedCount(t, reader, structuralTrigger),
		"the armed bounded close owns the reply — not the quorum relabel")
	assert.Zero(t, closedCount(t, reader, endVotesTrigger),
		"no end_votes close: the reply was claimed before the vote hook")
	assert.Equal(t, int64(1), synthesisTurnCount(t, reader, "closed_on_reply"))
	notifications := disp.closeNotifications()
	require.NotEmpty(t, notifications)
	for _, c := range notifications {
		assert.Equal(t, structuralTrigger, c.env.InteractionCloseTrigger,
			"the truthful bounded cause rides the wire — the OQ #6 metering key every summary leases off")
		assert.Contains(t, c.msg.Content, "Synthesis: consensus reached")
	}
}

// TestSynthesisClose_ClaimWindowWithholdsStragglers — the consumed arm: between
// the reply claim and markInteractionClosed's ledger write, the entry keeps its
// pendingSynthesis pointer, so a straggler stamped with the still-open id can
// neither advance the tally past the bound again (arming a duplicate synthesis
// directive) nor claim a second close. White-box, because the window is a
// sub-teardown interleaving.
func TestSynthesisClose_ClaimWindowWithholdsStragglers(t *testing.T) {
	router, _, ch, reader := synthesisCloseHarness(t, 2)
	router.synthesisTimeout = time.Hour

	tick(t, router, ch)
	openID, _, _ := router.openInteractionEscalationState(ch)
	tick(t, router, ch) // bound → armed

	reply := ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "iron-fox", Content: "Synthesis: done.",
		Metadata: map[string]any{"interaction_id": openID, synthesisReplyMetadataKey: true},
	}
	pending := router.claimSynthesisReply(reply, openID)
	require.NotNil(t, pending, "the marked chair reply claims the arm")

	// The claim→tombstone window, held open deliberately: every withhold and
	// re-arm gate must still see the terminating interaction.
	_, _, ok, closedStale := router.advanceBoundedCloseRound(ch, openID)
	assert.False(t, ok, "a straggler cannot advance the tally in the claim window")
	assert.True(t, closedStale, "…and is withheld as terminating traffic, not dispatched")
	assert.True(t, router.stimulusOutlivedClose(
		ChannelMessage{ID: uuid.NewString(), ChannelID: ch, SenderID: "ember-owl"},
		router.AutonomousFor(ch)), "the head withhold holds through the window too")
	assert.Nil(t, router.claimSynthesisReply(reply, openID),
		"a second marked reply claims nothing — one arm, one close")

	// Finish the teardown as the commit path would; everything reconciles.
	require.True(t, router.boundedClose(context.Background(), reply, ChannelTypeGroup,
		pending.interactionID, pending.trigger, closeNotify{}))
	router.WaitForPendingFanout()
	assert.Equal(t, int64(1), closedCount(t, reader, structuralTrigger))
	_, _, tracked := router.openInteractionEscalationState(ch)
	assert.False(t, tracked)
}

// TestSynthesisClose_StaleInboundClaimDoesNotClaimTheArm — the id conjunct
// must read the INBOUND wire claim, not the metadata bag (PR #718 follow-up
// review): publishCommit stamps the resolver's verdict over the bag before
// the claim runs, and while armed the resolver always resolves the armed id
// for a non-latched publish — so a bag re-read made the conjunct a tautology
// and a marked chair publish echoing a PREDECESSOR generation's id (or none
// at all) was consumed as the closing artifact. Both shapes must fall through
// to the armed-window withhold, and the genuinely-claimed reply still closes.
func TestSynthesisClose_StaleInboundClaimDoesNotClaimTheArm(t *testing.T) {
	router, disp, ch, reader := synthesisCloseHarness(t, 2)
	router.synthesisTimeout = time.Hour // a timeout fallback would hang, not pass

	tick(t, router, ch)
	openID, _, _ := router.openInteractionEscalationState(ch)
	tick(t, router, ch) // bound → armed
	require.Len(t, disp.synthesisTurns(), 1)

	// A marked chair publish whose INBOUND claim names an OLD generation —
	// a stale echo of a rotated/disarmed predecessor the 8-generation ledger
	// never held.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "iron-fox",
		Content:  "Synthesis: a stale echo.",
		Metadata: map[string]any{"interaction_id": "predecessor-generation", synthesisReplyMetadataKey: true},
	}, ""))
	// …and a marked chair publish carrying NO claim at all: a genuine
	// synthesis reply always has the claim stamped beside the marker.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "iron-fox",
		Content:  "Synthesis: an unclaimed echo.",
		Metadata: map[string]any{synthesisReplyMetadataKey: true},
	}, ""))
	router.WaitForPendingFanout()

	assert.Zero(t, closedCount(t, reader, structuralTrigger),
		"neither mis-claimed reply closes — the armed withhold owns both")
	require.Equal(t, "iron-fox", router.armedSynthesisChair(ch),
		"the arm survives, still waiting on the genuinely-claimed reply")

	chairReply(t, router, ch, "iron-fox", openID, "Synthesis: the real artifact.")
	router.WaitForPendingFanout()
	assert.Equal(t, int64(1), closedCount(t, reader, structuralTrigger))
	notifications := disp.closeNotifications()
	require.NotEmpty(t, notifications)
	for _, c := range notifications {
		assert.Contains(t, c.msg.Content, "Synthesis: the real artifact.",
			"the close carries the claimed reply, never a stale echo")
	}
}

// TestSynthesisTimeout_DisabledChannelAbandonsClose — the arm-after-disarm
// sliver: an arm created just after SetAutonomous(disable)'s disarm swept
// (nothing was armed yet) leaves a live timeout net on a channel the operator
// took manual control of. The net now re-checks `enabled` at fire time and
// abandons the close — leaving the interaction open under manual control —
// instead of force-closing a live human-steered discussion.
func TestSynthesisTimeout_DisabledChannelAbandonsClose(t *testing.T) {
	router, disp, ch, reader := synthesisCloseHarness(t, 5)

	tick(t, router, ch)
	openID, _, _ := router.openInteractionEscalationState(ch)

	// The window, reproduced deterministically: disable first (the disarm
	// no-ops — nothing armed), then the stale-snapshot arm lands.
	router.SetAutonomous(ch, AutonomousConfig{Enabled: false})
	pending := &pendingSynthesisClose{
		interactionID: openID, trigger: structuralTrigger, chairID: "iron-fox",
		ct:       ChannelTypeGroup,
		stimulus: ChannelMessage{ID: uuid.NewString(), ChannelID: ch, SenderID: "operator", Content: "continue"},
	}
	router.interactionMu.Lock()
	router.openInteractions[ch].pendingSynthesis = pending
	router.interactionMu.Unlock()

	router.synthesisWG.Add(1) // the fire path owns one count (its deferred Done)
	router.onSynthesisTimeout(pending)

	assert.Zero(t, closedCount(t, reader, structuralTrigger),
		"a disabled channel's timeout fire must not close the manual discussion")
	assert.Zero(t, synthesisTurnCount(t, reader, "closed_on_timeout"))
	assert.Empty(t, disp.closeNotifications(), "no close notification fans")
	_, _, tracked := router.openInteractionEscalationState(ch)
	assert.True(t, tracked, "the interaction stays open under the operator's manual control")
	assert.Empty(t, router.armedSynthesisChair(ch), "the abandoned arm is fully disarmed, not left withholding")
}

// gatedSynthDispatcher parks ordinary dispatches on a gate (signalling arrival
// once) while letting control dispatches through — it holds a floor-path
// fanout in flight, pre-arm, so the shutdown drain can be started against it.
type gatedSynthDispatcher struct {
	dispatchRecorder
	mu      sync.Mutex
	gate    chan struct{}
	arrived chan struct{}
	once    sync.Once
}

func (g *gatedSynthDispatcher) Dispatch(ctx context.Context, env DispatchEnvelope, msg ChannelMessage) error {
	g.mu.Lock()
	gate := g.gate
	g.mu.Unlock()
	if gate != nil && !env.SynthesisTurn && !env.InteractionCloseNotification {
		g.once.Do(func() { close(g.arrived) })
		<-gate
	}
	return g.dispatchRecorder.Dispatch(ctx, env, msg)
}

// TestDrainPendingFanout_ArmDuringDrainRefusedNotLeaked — the drain-vs-arm
// race: a fanout still in flight when DrainPendingFanout starts can cross the
// bound AFTER the disarm sweep, leaving the drain to block on (or return
// past) a fresh timer no sweep will ever stop. The draining gate closes it:
// the flag is set under interactionMu before the sweep, the arm CAS checks it
// under the same lock, so the late bound-crosser REFUSES to arm and degrades
// to the immediate close — the WaitGroups settle, nothing is left armed, and
// the interaction still terminated deterministically. (Waiting the in-flight
// fanouts before sweeping instead traded this race for a firing timer's
// fanoutWG.Add-from-zero against the drain's own first Wait — the second
// follow-up review.)
func TestDrainPendingFanout_ArmDuringDrainRefusedNotLeaked(t *testing.T) {
	disp := &gatedSynthDispatcher{arrived: make(chan struct{})}
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ch := mustCreateGroupWithPolicies(t, store, "brainstorm",
		map[string]RespondPolicy{
			"operator":  RespondNever,
			"ember-owl": RespondAlways,
			"iron-fox":  RespondAlways,
		}, "operator", "ember-owl", "iron-fox")
	router.SetFloorControl(ch, true, time.Millisecond)
	router.SetAutonomous(ch, AutonomousConfig{Enabled: true, MaxRounds: 1, Convener: "ember-owl", Goal: "a recommendation"})
	router.SetEscalationChair(ch, "iron-fox")
	router.synthesisTimeout = time.Hour // a leaked arm would be glaring (and stable)

	// The bounding publish, detached, parked in its round pre-arm.
	gate := make(chan struct{})
	disp.mu.Lock()
	disp.gate = gate
	disp.mu.Unlock()
	require.NoError(t, router.PublishAsync(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "operator", Content: "continue",
	}, ""))
	<-disp.arrived // the fanout is in flight, blocked on the gate

	drained := make(chan bool, 1)
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		drained <- router.DrainPendingFanout(ctx)
	}()
	// Let the drain reach its first wait, then release the fanout: the round
	// completes and the tail arms — DURING the drain.
	time.Sleep(20 * time.Millisecond)
	close(gate)

	select {
	case ok := <-drained:
		assert.True(t, ok, "the drain settles: the late arm was refused, not waited on")
	case <-time.After(15 * time.Second):
		t.Fatal("drain never settled — the in-drain arm leaked past the sweep")
	}
	assert.Empty(t, router.armedSynthesisChair(ch),
		"nothing is left armed behind a drain that reported success")
	assert.NotEmpty(t, disp.closeNotifications(),
		"the refused arm degraded to the immediate close — termination stayed deterministic through the drain")
}

// closeThenRemarkDispatcher reproduces the arm/markActivity ordering race
// deterministically: on the synthesis-turn dispatch it runs a racing
// deliberate close (whose releaseSynthesisArm clears a chair mark that is not
// there yet in the real interleaving) and then re-marks the chair — the
// post-close markActivity state the race strands. The arm-side re-lock must
// then clear the mark itself.
type closeThenRemarkDispatcher struct {
	dispatchRecorder
	router *ChannelRouter
	ch     string
}

func (d *closeThenRemarkDispatcher) Dispatch(ctx context.Context, env DispatchEnvelope, msg ChannelMessage) error {
	if env.SynthesisTurn {
		d.router.markInteractionClosed(d.ch, readInteractionID(msg.Metadata), endVotesTrigger)
		d.router.markActivity(d.ch, []string{env.Recipient.ParticipantID})
	}
	return d.dispatchRecorder.Dispatch(ctx, env, msg)
}

// TestSynthesisClose_RacingCloseMidDispatchClearsChairMark — a deliberate
// close landing between the arm and the timer registration disarms via
// markInteractionClosed, whose clearActivity can run BEFORE the arm side's
// markActivity sets the chair's "thinking" mark; with the reply then
// latch-suppressed nothing ever clears it and the chair strands as composing
// for the whole activity TTL. The arm side's re-lock now clears the mark when
// it finds the arm gone.
func TestSynthesisClose_RacingCloseMidDispatchClearsChairMark(t *testing.T) {
	disp := &closeThenRemarkDispatcher{}
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	disp.router = router
	ch := mustCreateGroupWithPolicies(t, store, "brainstorm",
		map[string]RespondPolicy{
			"operator":  RespondNever,
			"ember-owl": RespondAlways,
			"iron-fox":  RespondAlways,
		}, "operator", "ember-owl", "iron-fox")
	disp.ch = ch
	router.SetFloorControl(ch, true, time.Millisecond)
	router.SetAutonomous(ch, AutonomousConfig{Enabled: true, MaxRounds: 1, Convener: "ember-owl", Goal: "a recommendation"})
	router.SetEscalationChair(ch, "iron-fox")

	tick(t, router, ch) // bound at round 1 → arm → dispatch → racing close inside
	router.WaitForPendingFanout()

	assert.NotContains(t, router.ChannelActivity(ch), "iron-fox",
		"the chair's thinking mark is cleared when the racing close disarmed mid-dispatch")
	assert.Empty(t, router.armedSynthesisChair(ch), "the racing close disarmed the arm")
}

// TestResolverFreshMint_RoutesArmedDisarmThroughRelease — the fresh-mint reset
// used to discard disarmPendingSynthesisLocked's timerStopped return ("owes no
// WG Done"), so if a live arm ever reached that reset its stopped timer's
// synthesisWG count leaked and every later shutdown drain hung forever. The
// reset now routes through releaseSynthesisArm like every other disarm
// terminal. White-box: the state is contrived (rotation is arm-gated and
// deliberate closes disarm first), which is exactly why the accounting must be
// defensive rather than assumed.
func TestResolverFreshMint_RoutesArmedDisarmThroughRelease(t *testing.T) {
	router, _, ch, _ := synthesisCloseHarness(t, 50)

	tick(t, router, ch) // a committed entry

	// The "unreachable" shape: an emptied id with a LIVE armed timer.
	router.interactionMu.Lock()
	entry := router.openInteractions[ch]
	entry.id = ""
	entry.pendingSynthesis = &pendingSynthesisClose{
		interactionID: "dead-generation", chairID: "iron-fox", ct: ChannelTypeGroup,
		timer: time.AfterFunc(time.Hour, func() {}),
	}
	router.interactionMu.Unlock()
	router.synthesisWG.Add(1) // the live timer's drain registration

	tick(t, router, ch) // the next publish mints fresh → the reset disarms

	assert.Empty(t, router.armedSynthesisChair(ch), "the stale arm is gone")
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	assert.True(t, router.DrainPendingFanout(ctx),
		"the stopped timer's synthesisWG count was released — the drain settles instead of hanging forever")
}

// TestSynthesisClose_ReplyClaimHoldsArmCountThroughClose — PR #718 review
// follow-up: the commit-path claim released the arm's synthesisWG count the
// moment it Stop()ped the timer — BEFORE the caller's close ran — so the
// close's notifyInteractionClose fanoutWG.Add(1)s (on the publishing
// goroutine, which holds no fanoutWG count) could race DrainPendingFanout's
// fanoutWG.Wait from zero: the exact Add-vs-Wait misuse the drain's ordering
// proof rules out for the timeout path, whose Done is deferred past its close.
// The pin: the claim TRANSFERS the count to the caller (closeOnSynthesisReply
// releases it after the close), so synthesisWG stays held across the
// claim→close window.
func TestSynthesisClose_ReplyClaimHoldsArmCountThroughClose(t *testing.T) {
	router, _, ch, _ := synthesisCloseHarness(t, 2)

	tick(t, router, ch)
	openID, _, _ := router.openInteractionEscalationState(ch)
	tick(t, router, ch) // bound → armed

	// White-box claim (the publishCommit branch's first half): the arm's count
	// must ride the returned pending until the caller's close completes.
	msg := ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "iron-fox", Content: "synthesis",
		Metadata: map[string]any{
			interactionIDMetadataKey:  openID,
			synthesisReplyMetadataKey: true,
		},
	}
	pending := router.claimSynthesisReply(msg, openID)
	require.NotNil(t, pending, "precondition: the reply claims the arm")

	waited := make(chan struct{})
	go func() { router.synthesisWG.Wait(); close(waited) }()
	select {
	case <-waited:
		t.Fatal("synthesisWG released at claim time — the close's fanoutWG.Adds can race the drain's Wait from zero")
	case <-time.After(50 * time.Millisecond):
	}

	router.synthesisWG.Done() // the caller's post-close release
	select {
	case <-waited:
	case <-time.After(time.Second):
		t.Fatal("the transferred count was never releasable")
	}
}
