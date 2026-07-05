package channels

// chair_escalation_resynthesize_test.go — the ISSUE-0099 misfire trigger.
// chair_escalation_test.go / _guard_test.go pin the first forced turn (CE1–7)
// and its dispositions; this file pins the SECOND one: after the chair's
// forced-turn reply provably reaches no floor-capable member, the orchestrator
// re-forces exactly one synthesize-only turn — built from the STASHED original
// (non-chair) stimulus, so the gate admits it (re-sending the chair's own reply
// would self-suppress, which is why a plain refund is inert), marked
// resynthesize so the persona renders the synthesize-only framing, and bounded
// to once per interaction so a second misfire stands.

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.uber.org/zap"
)

// driveResynthesize composes the trigger's two halves exactly as the fanout
// seam does off a bounded close — claim at the head, dispatch iff a re-force is
// owed — so these unit pins exercise the same claim→dispatch arc a real publish
// runs (fanout.go; the split is the PR 4b-i review round 5 ordering fix).
func driveResynthesize(router *ChannelRouter, msg ChannelMessage, ct ChannelType, members []Member, channelSize int, misfired bool) {
	if pending := router.claimResynthesizeMisfire(msg, misfired); pending != nil {
		router.dispatchResynthesizeMisfire(context.Background(), msg, ct, members, channelSize, pending)
	}
}

// resynthesizeEnvelopes filters the recorder's calls down to the resynthesize
// re-dispatches (both flags set; the lift still rides ChairEscalation).
func resynthesizeEnvelopes(disp *envelopeRecorder) []DispatchEnvelope {
	var out []DispatchEnvelope
	for _, env := range disp.snapshot() {
		if env.ChairEscalationResynthesize {
			out = append(out, env)
		}
	}
	return out
}

// chairMisfireReply is the chair's forced-turn reply that named a hand-off
// target which is not floor-capable — here the `respond: never` operator
// `alex` — so the floor-mention subset is empty at the publish seam.
func chairMisfireReply(ch, interactionID string) ChannelMessage {
	reply := stalledMsg(ch, "nova-sparrow", interactionID)
	reply.Mentions = []string{"alex"}
	return reply
}

// TestChairResynthesize_MisfiredReply_ReForcesSynthesizeOnly — the happy arc:
// a stall escalates (forced turn 1, stashing the stimulus), then the chair's
// own reply names only a non-floor-capable target, and the publish seam
// re-forces one resynthesize turn to the chair. The envelope carries BOTH
// flags (the lift is unchanged; only the framing flips) and the metric labels
// it `resynthesized`.
func TestChairResynthesize_MisfiredReply_ReForcesSynthesizeOnly(t *testing.T) {
	router, disp, _, ch, reader := escalationHarness(t)
	open := commitOpenInteraction(t, router, ch)
	members := []Member{member("nova-sparrow", RespondAlways), member("ember-owl", RespondAlways)}

	// Forced turn 1: stall on alex's stimulus → escalate, stash alex's stimulus.
	router.maybeEscalateStall(context.Background(), stalledMsg(ch, "alex", open), ChannelTypeGroup, "",
		floorRoundOutcome{granted: 2}, members, 4, nil)
	require.Len(t, escalationEnvelopes(disp), 1)

	// The chair's reply misfires: it @-mentions the operator, who cannot take
	// the floor → the resynthesize re-dispatch fires (misfired = true, the
	// value the fanout seam computes from the empty floor-mention subset).
	driveResynthesize(router, chairMisfireReply(ch, open),
		ChannelTypeGroup, members, 4, true)

	re := resynthesizeEnvelopes(disp)
	require.Len(t, re, 1, "a provable misfire re-forces exactly one synthesize-only turn")
	assert.Equal(t, "nova-sparrow", re[0].Recipient.ParticipantID, "re-forced to the chair")
	assert.True(t, re[0].ChairEscalation, "the lift rides ChairEscalation, unchanged")
	assert.True(t, re[0].ChairEscalationResynthesize, "the framing flips to synthesize-only")

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), chairEscalationCount(t, rm, "group", "resynthesized"))
}

// TestChairResynthesize_EndToEnd_PublishWiring — the fanout seam itself: a
// real stall escalates, then the chair's forced-turn reply is PUBLISHED naming
// only the operator, and the publish path (not a direct call) detects the
// misfire and re-forces. Pins the wiring a refactor that dropped the seam call
// would otherwise pass straight through.
func TestChairResynthesize_EndToEnd_PublishWiring(t *testing.T) {
	router, disp, _, ch, reader := escalationHarness(t)

	stallStimulus(t, router, ch, "alex") // forced turn 1 → chair, stash alex's stimulus
	require.Len(t, escalationEnvelopes(disp), 1)

	// The chair replies, handing off to the operator (a `respond: never`
	// member) — a provable misfire at the publish seam.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "nova-sparrow",
		Content: "alex, your call?", Mentions: []string{"alex"},
	}, ""))

	re := resynthesizeEnvelopes(disp)
	require.Len(t, re, 1, "the fanout misfire seam re-forces one synthesize-only turn")
	assert.Equal(t, "nova-sparrow", re[0].Recipient.ParticipantID)
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), chairEscalationCount(t, rm, "group", "resynthesized"))
}

// TestChairResynthesize_EndToEnd_VoteReplyDisarmsNoReForce — the false positive
// ISSUE-0099's live MT surfaced: the chair's forced-turn reply is a synthesis-
// in-VOTE that @-mentions the still-outstanding voice — here the operator, a
// `respond: never` member — exactly what the framing invites ("@-mention the
// missing voice inside your vote's `content`"). Its floor-mention subset is
// empty, identical to a misfired hand-off at the seam, but a vote is outcome
// (a), not a hand-off: it must DISARM the trigger (consume the stash) WITHOUT
// re-forcing, or the chair gets a second synthesize-only turn after it already
// synthesized and voted. Exercised through the real publish seam, where the
// `end_interaction_vote` guard on `misfired` lives.
func TestChairResynthesize_EndToEnd_VoteReplyDisarmsNoReForce(t *testing.T) {
	router, disp, _, ch, reader := escalationHarness(t)

	stallStimulus(t, router, ch, "alex") // forced turn 1 → chair, stash alex's stimulus
	require.Len(t, escalationEnvelopes(disp), 1)

	// The chair's forced-turn reply: a synthesis cast as a vote that @-mentions
	// the operator (non-floor-capable). A vote, not a hand-off — no re-force.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "nova-sparrow",
		Content:  "Closing: three risks on the record. @alex owns the budget call.",
		Mentions: []string{"alex"},
		Metadata: map[string]any{endVoteMetadataKey: true},
	}, ""))

	assert.Empty(t, resynthesizeEnvelopes(disp),
		"a synthesis-in-vote is outcome (a), not a misfired hand-off — no re-force")
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Zero(t, chairEscalationCount(t, rm, "group", "resynthesized"))

	// The vote still consumed the arm: a later BARE misfire from the chair is a
	// stale message, not the forced-turn reply, so it does not re-inject either.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "nova-sparrow",
		Content: "@alex thanks all", Mentions: []string{"alex"},
	}, ""))
	assert.Empty(t, resynthesizeEnvelopes(disp),
		"the vote reply already disarmed the trigger — a later bare misfire re-forces nothing")
}

// TestChairResynthesize_CarriesOriginalNonChairSender — the headline of the
// design: the re-dispatch must carry the ORIGINAL stimulus's non-chair sender,
// not the chair's misfired reply. Re-sending the chair's own reply to the chair
// trips the gate's self_sender defence (the same reason option 1's refund is
// inert), so the synthesize turn would never run. Under a fresh event id, like
// every forced turn.
func TestChairResynthesize_CarriesOriginalNonChairSender(t *testing.T) {
	router, _, _, ch, _ := escalationHarness(t)
	rec := &messageRecordingDispatcher{}
	router.dispatcher = rec
	open := commitOpenInteraction(t, router, ch)
	members := []Member{member("nova-sparrow", RespondAlways), member("ember-owl", RespondAlways)}

	router.maybeEscalateStall(context.Background(), stalledMsg(ch, "alex", open), ChannelTypeGroup, "",
		floorRoundOutcome{granted: 2}, members, 4, nil)
	reply := chairMisfireReply(ch, open)
	driveResynthesize(router, reply, ChannelTypeGroup, members, 4, true)

	var forced []ChannelMessage
	for i, env := range rec.envelopes {
		if env.ChairEscalationResynthesize {
			forced = append(forced, rec.messages[i])
		}
	}
	require.Len(t, forced, 1)
	assert.Equal(t, "alex", forced[0].SenderID,
		"the re-dispatch carries the ORIGINAL non-chair sender, not the chair's reply — else the gate self-suppresses")
	assert.NotEqual(t, reply.ID, forced[0].ID, "a fresh event id, like every forced turn")
	assert.Equal(t, open, forced[0].Metadata[interactionIDMetadataKey],
		"the stamped interaction_id rides along for lease attribution")
}

// TestChairResynthesize_BoundOncePerInteraction — the loop guard: the
// re-dispatch fires at most once. A second provable misfire on the same
// interaction re-forces nothing, so a hand-off that misfires twice stands.
func TestChairResynthesize_BoundOncePerInteraction(t *testing.T) {
	router, disp, _, ch, reader := escalationHarness(t)
	open := commitOpenInteraction(t, router, ch)
	members := []Member{member("nova-sparrow", RespondAlways), member("ember-owl", RespondAlways)}

	router.maybeEscalateStall(context.Background(), stalledMsg(ch, "alex", open), ChannelTypeGroup, "",
		floorRoundOutcome{granted: 2}, members, 4, nil)
	driveResynthesize(router, chairMisfireReply(ch, open), ChannelTypeGroup, members, 4, true)
	driveResynthesize(router, chairMisfireReply(ch, open), ChannelTypeGroup, members, 4, true)

	assert.Len(t, resynthesizeEnvelopes(disp), 1, "the resynthesize re-dispatch is bounded to once")
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), chairEscalationCount(t, rm, "group", "resynthesized"))
}

// TestChairResynthesize_OnlyTheChairsOwnReply — the misfire must be the
// chair's. An ordinary member's publish that happens to name only the operator
// is not an escalation failure (the chair never handed off), so it must not
// re-force even within an escalated interaction.
func TestChairResynthesize_OnlyTheChairsOwnReply(t *testing.T) {
	router, disp, _, ch, reader := escalationHarness(t)
	open := commitOpenInteraction(t, router, ch)
	members := []Member{member("nova-sparrow", RespondAlways), member("ember-owl", RespondAlways)}

	router.maybeEscalateStall(context.Background(), stalledMsg(ch, "alex", open), ChannelTypeGroup, "",
		floorRoundOutcome{granted: 2}, members, 4, nil)

	// ember-owl (not the chair) names the operator — not a hand-off misfire.
	notChair := stalledMsg(ch, "ember-owl", open)
	notChair.Mentions = []string{"alex"}
	driveResynthesize(router, notChair, ChannelTypeGroup, members, 4, true)

	assert.Empty(t, resynthesizeEnvelopes(disp), "only the chair's own reply misfires a hand-off")
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Zero(t, chairEscalationCount(t, rm, "group", "resynthesized"))
}

// TestChairResynthesize_NotEscalated_NoReForce — without a first escalation
// there is no provable failure to recover and no stash to re-force from: a
// chair publish naming only the operator is just an ordinary directed-nowhere
// message, not a misfire.
func TestChairResynthesize_NotEscalated_NoReForce(t *testing.T) {
	router, disp, _, ch, reader := escalationHarness(t)
	open := commitOpenInteraction(t, router, ch)
	members := []Member{member("nova-sparrow", RespondAlways), member("ember-owl", RespondAlways)}

	driveResynthesize(router, chairMisfireReply(ch, open), ChannelTypeGroup, members, 4, true)

	assert.Empty(t, resynthesizeEnvelopes(disp))
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Zero(t, chairEscalationCount(t, rm, "group", "resynthesized"))
}

// TestChairResynthesize_RoundOutlivedInteraction_NotReForced — the divergence
// guard, mirroring the stall tail: a misfired reply whose stamped id no longer
// matches the open id (a concurrent rotation moved it on) must not spend the
// successor's resynthesize on the predecessor's hand-off.
func TestChairResynthesize_RoundOutlivedInteraction_NotReForced(t *testing.T) {
	router, disp, _, ch, reader := escalationHarness(t)
	open := commitOpenInteraction(t, router, ch)
	members := []Member{member("nova-sparrow", RespondAlways), member("ember-owl", RespondAlways)}

	router.maybeEscalateStall(context.Background(), stalledMsg(ch, "alex", open), ChannelTypeGroup, "",
		floorRoundOutcome{granted: 2}, members, 4, nil)

	stale := chairMisfireReply(ch, uuid.NewString()) // a retired generation's id
	driveResynthesize(router, stale, ChannelTypeGroup, members, 4, true)

	assert.Empty(t, resynthesizeEnvelopes(disp), "a reply that outlived its interaction does not re-force")
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Zero(t, chairEscalationCount(t, rm, "group", "resynthesized"))
}

// TestChairResynthesize_ChairGone_ResynthesizeError — runtime drift: the chair
// left after the first escalation. claimChairReply consumes the arm (like the
// stall tail's drift branch spends the ration — no refund), so the failure is
// the publish-seam `resynthesize_error` (NOT the round-tail `dispatch_error`),
// and a later misfire does not retry.
func TestChairResynthesize_ChairGone_ResynthesizeError(t *testing.T) {
	router, disp, _, ch, reader := escalationHarness(t)
	open := commitOpenInteraction(t, router, ch)
	full := []Member{member("nova-sparrow", RespondAlways), member("ember-owl", RespondAlways)}

	router.maybeEscalateStall(context.Background(), stalledMsg(ch, "alex", open), ChannelTypeGroup, "",
		floorRoundOutcome{granted: 2}, full, 4, nil)

	withoutChair := []Member{member("ember-owl", RespondAlways)}
	driveResynthesize(router, chairMisfireReply(ch, open), ChannelTypeGroup, withoutChair, 4, true)
	// The arm is consumed on the drift error — a second misfire re-forces nothing.
	driveResynthesize(router, chairMisfireReply(ch, open), ChannelTypeGroup, full, 4, true)

	assert.Empty(t, resynthesizeEnvelopes(disp))
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), chairEscalationCount(t, rm, "group", "resynthesize_error"),
		"a drift failure consumes the arm and labels resynthesize_error — no retry")
	assert.Zero(t, chairEscalationCount(t, rm, "group", "dispatch_error"),
		"the publish-seam failure must NOT pollute the round-tail dispatch_error stall count")
	assert.Zero(t, chairEscalationCount(t, rm, "group", "resynthesized"))
}

// TestChairResynthesize_FreshInteractionClearsState — the stash and the bound
// ride the resolver entry and die with the interaction: after an idle rotation,
// a new escalation re-stashes and a new misfire re-forces again.
func TestChairResynthesize_FreshInteractionClearsState(t *testing.T) {
	router, disp, _, ch, _ := escalationHarness(t)
	members := []Member{member("nova-sparrow", RespondAlways), member("ember-owl", RespondAlways)}
	now := time.Date(2026, 6, 13, 12, 0, 0, 0, time.UTC)
	router.interactionNow = func() time.Time { return now }

	first := commitOpenInteraction(t, router, ch)
	router.maybeEscalateStall(context.Background(), stalledMsg(ch, "alex", first), ChannelTypeGroup, "",
		floorRoundOutcome{granted: 2}, members, 4, nil)
	driveResynthesize(router, chairMisfireReply(ch, first), ChannelTypeGroup, members, 4, true)
	require.Len(t, resynthesizeEnvelopes(disp), 1)

	// Idle past the window: the next commit rotates to a fresh interaction. The
	// resolver entry is reused across generations, so the stash and bound must
	// clear in lockstep with the ration.
	now = now.Add(601 * time.Second)
	second := commitOpenInteraction(t, router, ch)
	require.NotEqual(t, first, second)
	router.maybeEscalateStall(context.Background(), stalledMsg(ch, "alex", second), ChannelTypeGroup, "",
		floorRoundOutcome{granted: 2}, members, 4, nil)
	driveResynthesize(router, chairMisfireReply(ch, second), ChannelTypeGroup, members, 4, true)

	assert.Len(t, resynthesizeEnvelopes(disp), 2, "a fresh interaction re-forces again")
}

// TestChairResynthesize_CleanReplyDisarms_NoLaterMisfire — the deep-review
// finding the trigger MUST close: the re-synthesis is the chair's FORCED-TURN
// reply's misfire, not "the first chair publish in the interaction that names
// no floor-capable member". When the forced-turn reply takes the floor cleanly
// (`misfired == false`), consuming the stash disarms the trigger, so a LATER
// innocuous chair message ("@alex, thanks") can no longer be mistaken for the
// reply's misfire and re-inject the now-stale stimulus.
func TestChairResynthesize_CleanReplyDisarms_NoLaterMisfire(t *testing.T) {
	router, disp, _, ch, reader := escalationHarness(t)
	open := commitOpenInteraction(t, router, ch)
	members := []Member{member("nova-sparrow", RespondAlways), member("ember-owl", RespondAlways)}

	router.maybeEscalateStall(context.Background(), stalledMsg(ch, "alex", open), ChannelTypeGroup, "",
		floorRoundOutcome{granted: 2}, members, 4, nil)

	// The chair's forced-turn reply hands the floor cleanly (misfired = false):
	// it consumes the arm without re-forcing.
	cleanReply := stalledMsg(ch, "nova-sparrow", open)
	driveResynthesize(router, cleanReply, ChannelTypeGroup, members, 4, false)
	require.Empty(t, resynthesizeEnvelopes(disp), "a clean forced-turn reply does not re-force")

	// A later chair message names only the operator — a provable empty floor
	// mention. The arm is already consumed, so this stands; pre-fix it would
	// have spuriously re-forced from the stale stimulus.
	driveResynthesize(router, chairMisfireReply(ch, open), ChannelTypeGroup, members, 4, true)

	assert.Empty(t, resynthesizeEnvelopes(disp),
		"a clean hand-off disarms the trigger; a later innocuous chair message is not a misfire")
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Zero(t, chairEscalationCount(t, rm, "group", "resynthesized"))
}

// TestChairResynthesize_EndToEnd_CleanReplyDisarms — the same defence through
// the real fanout seam: the seam must run the trigger for the chair's clean
// hand-off too (not only empty-floor-mention publishes), or the arm never
// clears and the false-positive returns. A refactor that re-gates the seam call
// behind the misfire condition would fail here.
func TestChairResynthesize_EndToEnd_CleanReplyDisarms(t *testing.T) {
	router, disp, _, ch, _ := escalationHarness(t)

	stallStimulus(t, router, ch, "alex") // forced turn 1 → chair, arm the stash
	require.Len(t, escalationEnvelopes(disp), 1)

	// The chair hands off to a floor-capable member — a clean reply, not a
	// misfire. The seam must still consume the arm.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "nova-sparrow",
		Content: "ember-owl, your take?", Mentions: []string{"ember-owl"},
	}, ""))
	require.Empty(t, resynthesizeEnvelopes(disp), "a clean hand-off does not re-force")

	// A later chair message names only the operator — disarmed, so it stands.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "nova-sparrow",
		Content: "alex, thanks", Mentions: []string{"alex"},
	}, ""))
	assert.Empty(t, resynthesizeEnvelopes(disp),
		"the seam disarmed on the clean reply; the later empty-floor-mention message is not a misfire")
}

// TestChairResynthesize_CarriesOriginalThreadParent — the re-dispatch
// reproduces the ORIGINAL stimulus's thread parent (stashed at escalation), not
// the misfired reply's: the reply is a different node in the thread tree, and
// the re-forced turn re-delivers the stimulus, so it must carry the stimulus's
// thread context.
func TestChairResynthesize_CarriesOriginalThreadParent(t *testing.T) {
	router, disp, _, ch, _ := escalationHarness(t)
	open := commitOpenInteraction(t, router, ch)
	members := []Member{member("nova-sparrow", RespondAlways), member("ember-owl", RespondAlways)}

	// Forced turn 1 carries the original stimulus's thread parent.
	router.maybeEscalateStall(context.Background(), stalledMsg(ch, "alex", open), ChannelTypeGroup,
		"thread-root-author", floorRoundOutcome{granted: 2}, members, 4, nil)

	driveResynthesize(router, chairMisfireReply(ch, open),
		ChannelTypeGroup, members, 4, true)

	re := resynthesizeEnvelopes(disp)
	require.Len(t, re, 1)
	assert.Equal(t, "thread-root-author", re[0].ThreadParentSenderID,
		"the re-dispatch reproduces the stimulus's thread parent, stashed at escalation — not the reply's")
}

// TestChairResynthesize_ClaimAtHead_LaterFastPublishCannotStealArm pins the
// PR 4b-i review round 5 ordering fix: the once-bound stash is claimed at the
// fanout HEAD (publishes reach their heads in commit order), never the tail.
// Tail-claiming let a later chair publish STEAL the arm: the real forced-turn
// reply — a clean hand-off naming two floor-capable members — parks in a
// multi-turn floor round before its tail runs, while a later innocuous
// "@alex, thanks" (misfired at the seam, but NOT the forced-turn reply)
// reclassifies to open floor, finds a single always responder, takes the fast
// concurrent path, reaches its tail first, and claims the stash with ITS
// misfire flag — re-forcing a stale stimulus, the exact false positive the
// claim-on-first-reply contract exists to prevent.
//
// Discriminating deterministically: the pin is that the arm is ALREADY
// consumed while the clean reply's floor round is still RUNNING (its speakers
// registered, the round parked on its first turn). The head claim precedes
// floorRound in program order on the fanout goroutine, so post-fix that holds
// by construction; the tail claim cannot run until the round ends, so pre-fix
// the mid-round check fails outright — no timing luck involved. The fast
// publish then lands mid-round to exercise the full steal shape the tail
// ordering allowed (pre-fix its tail would claim the still-armed stash and
// re-force a stale stimulus).
func TestChairResynthesize_ClaimAtHead_LaterFastPublishCannotStealArm(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &envelopeRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ch := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"alex":         RespondNever,         // the operator — the misfire hand-off target
			"nova-sparrow": RespondAlways,        // the escalation chair
			"ember-owl":    RespondAlways,        // the sole open-floor responder
			"iron-fox":     RespondWhenMentioned, // floor-capable only when named
		}, "alex", "nova-sparrow", "ember-owl", "iron-fox")
	// Long enough that the clean reply's 2-speaker floor round is still running
	// when the fast publish lands (the pre-fix discriminator window), short
	// enough to keep the test quick.
	router.SetFloorControl(ch, true, 250*time.Millisecond)
	router.SetEscalationChair(ch, "nova-sparrow")

	// Round 1: alex's stimulus names both floor-capable members → a 2-speaker
	// floor round that stalls (the recorder swallows every dispatch) → forced
	// turn to the chair, arming the stash.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "alex",
		Content: "thoughts?", Mentions: []string{"ember-owl", "iron-fox"},
	}, ""))
	router.WaitForPendingFanout()
	require.Len(t, escalationEnvelopes(disp), 1, "round 1 stalls and escalates, arming the stash")

	// The chair's forced-turn reply: a CLEAN hand-off naming two floor-capable
	// members (misfired=false). Async — its 2-speaker floor round parks the
	// fanout for ~2×turnTimeout; a tail claim would park with it.
	require.NoError(t, router.PublishAsync(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "nova-sparrow",
		Content: "ember-owl, iron-fox — your take?", Mentions: []string{"ember-owl", "iron-fox"},
	}, ""))
	// Wait only for the round to have STARTED (its floor speakers registered) —
	// the round itself is still parked on its first 250ms turn.
	require.Eventually(t, func() bool {
		return router.isFloorSpeakerReply(ch, "ember-owl") || router.isFloorSpeakerReply(ch, "iron-fox")
	}, time.Second, 2*time.Millisecond, "the clean reply's floor round starts")
	// THE discriminating pin: mid-round, the arm is already consumed. The head
	// claim ran before floorRound on the same goroutine; a tail claim cannot
	// have run yet — pre-fix the stash is provably still armed here.
	armed := func() bool {
		router.interactionMu.Lock()
		defer router.interactionMu.Unlock()
		entry := router.openInteractions[ch]
		return entry != nil && entry.escalatedStimulus != nil
	}
	require.False(t, armed(),
		"the fanout HEAD consumes the arm before the round runs — a tail claim would still be parked behind it")

	// A later innocuous chair message lands mid-round: "@alex" is misfired at
	// the seam (alex is not floor-capable), reclassifies to open floor with a
	// single always responder, and takes the FAST concurrent path — the shape
	// that stole the parked tail claim pre-fix.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "nova-sparrow",
		Content: "alex, thanks", Mentions: []string{"alex"},
	}, ""))
	router.WaitForPendingFanout()

	assert.Empty(t, resynthesizeEnvelopes(disp),
		"the forced-turn reply's head claim already disarmed the trigger — a later fast publish cannot steal the arm and re-force a stale stimulus")
}
