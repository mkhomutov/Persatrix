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
)

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
	router.maybeResynthesizeMisfire(context.Background(), chairMisfireReply(ch, open),
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
	router.maybeResynthesizeMisfire(context.Background(), reply, ChannelTypeGroup, members, 4, true)

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
	router.maybeResynthesizeMisfire(context.Background(), chairMisfireReply(ch, open), ChannelTypeGroup, members, 4, true)
	router.maybeResynthesizeMisfire(context.Background(), chairMisfireReply(ch, open), ChannelTypeGroup, members, 4, true)

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
	router.maybeResynthesizeMisfire(context.Background(), notChair, ChannelTypeGroup, members, 4, true)

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

	router.maybeResynthesizeMisfire(context.Background(), chairMisfireReply(ch, open), ChannelTypeGroup, members, 4, true)

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
	router.maybeResynthesizeMisfire(context.Background(), stale, ChannelTypeGroup, members, 4, true)

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
	router.maybeResynthesizeMisfire(context.Background(), chairMisfireReply(ch, open), ChannelTypeGroup, withoutChair, 4, true)
	// The arm is consumed on the drift error — a second misfire re-forces nothing.
	router.maybeResynthesizeMisfire(context.Background(), chairMisfireReply(ch, open), ChannelTypeGroup, full, 4, true)

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
	router.maybeResynthesizeMisfire(context.Background(), chairMisfireReply(ch, first), ChannelTypeGroup, members, 4, true)
	require.Len(t, resynthesizeEnvelopes(disp), 1)

	// Idle past the window: the next commit rotates to a fresh interaction. The
	// resolver entry is reused across generations, so the stash and bound must
	// clear in lockstep with the ration.
	now = now.Add(601 * time.Second)
	second := commitOpenInteraction(t, router, ch)
	require.NotEqual(t, first, second)
	router.maybeEscalateStall(context.Background(), stalledMsg(ch, "alex", second), ChannelTypeGroup, "",
		floorRoundOutcome{granted: 2}, members, 4, nil)
	router.maybeResynthesizeMisfire(context.Background(), chairMisfireReply(ch, second), ChannelTypeGroup, members, 4, true)

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
	router.maybeResynthesizeMisfire(context.Background(), cleanReply, ChannelTypeGroup, members, 4, false)
	require.Empty(t, resynthesizeEnvelopes(disp), "a clean forced-turn reply does not re-force")

	// A later chair message names only the operator — a provable empty floor
	// mention. The arm is already consumed, so this stands; pre-fix it would
	// have spuriously re-forced from the stale stimulus.
	router.maybeResynthesizeMisfire(context.Background(), chairMisfireReply(ch, open), ChannelTypeGroup, members, 4, true)

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

	router.maybeResynthesizeMisfire(context.Background(), chairMisfireReply(ch, open),
		ChannelTypeGroup, members, 4, true)

	re := resynthesizeEnvelopes(disp)
	require.Len(t, re, 1)
	assert.Equal(t, "thread-root-author", re[0].ThreadParentSenderID,
		"the re-dispatch reproduces the stimulus's thread parent, stashed at escalation — not the reply's")
}
