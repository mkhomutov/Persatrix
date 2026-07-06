package channels

// synthesis_close_guard_test.go — RFC 0052 §D armed-window guard regressions
// (PR #718 review). Split out of synthesis_close_test.go at the 500-line review
// cap. These pin the determinism guarantees around the armed close-on-reply
// window: an armed interaction must not idle-rotate out from under its pending
// close (finding 2), disabling the autonomous block must disarm it (finding 3),
// and an entry that moved on between the bound tally and the arm must fall
// through to the immediate close rather than withhold the message (finding 4).
// They share synthesis_close_test.go's harness (synthesisCloseHarness, tick,
// chairReply, closedCount) in the same package.

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestSynthesisClose_ArmedInteractionDoesNotIdleRotate — PR #718 review
// finding 2: an interaction with an armed synthesis close must NOT idle-rotate
// while the chair's reply (or the timeout net) is outstanding. A per-channel
// idle window shorter than the synthesis timeout used to fire first — the
// rotation disarmed the pending close and retired the id WITHOUT running the
// bounded close, silently cancelling the §D artifact and resuming the
// discussion under a fresh generation. The pin: after the idle window elapses
// inside the armed window, the interaction stays open and armed (no
// interaction_closed{idle}), and the chair's reply still lands the
// deterministic close.
func TestSynthesisClose_ArmedInteractionDoesNotIdleRotate(t *testing.T) {
	router, _, ch, reader := synthesisCloseHarness(t, 2)
	now := time.Date(2026, 7, 4, 12, 0, 0, 0, time.UTC)
	router.interactionNow = func() time.Time { return now }
	router.SetInteractionIdleTimeout(ch, 30) // << the 120s synthesis timeout

	tick(t, router, ch)
	openID, _, _ := router.openInteractionEscalationState(ch)
	tick(t, router, ch) // bound → armed

	// The idle window elapses while the close is armed; an unstamped operator
	// stimulus lands in that window. Pre-fix this idle-rotated and disarmed.
	now = now.Add(90 * time.Second) // past the 30s idle window, under 120s
	tick(t, router, ch)
	router.WaitForPendingFanout()

	assert.Zero(t, closedCount(t, reader, idleTrigger),
		"the armed interaction must not idle-rotate — that would cancel the pending close")
	gotID, _, tracked := router.openInteractionEscalationState(ch)
	require.True(t, tracked, "the armed interaction stays open, not rotated away")
	assert.Equal(t, openID, gotID, "still the same armed interaction, no fresh mint")

	// The arm survived: the chair's reply still lands the deterministic close.
	chairReply(t, router, ch, "iron-fox", openID, "Synthesis: converged past the idle window.")
	router.WaitForPendingFanout()
	assert.Equal(t, int64(1), closedCount(t, reader, structuralTrigger),
		"the chair's reply closes the interaction the idle window would otherwise have orphaned")
}

// TestSynthesisClose_DisableDisarmsPendingClose — PR #718 review finding 3:
// disabling the autonomous block mid-arm must drop the pending synthesis close.
// Every arm seam gates on autonomous.enabled, so once disabled the chair's
// reply re-fans as an ordinary stimulus — but pre-fix the orphaned timeout net
// still held the pending pointer and closed the now-live conversation ~2min
// later. SetAutonomous(disabled) now disarms; the timer is stopped and never
// closes.
func TestSynthesisClose_DisableDisarmsPendingClose(t *testing.T) {
	router, _, ch, reader := synthesisCloseHarness(t, 2)
	router.synthesisTimeout = 20 * time.Millisecond

	tick(t, router, ch)
	tick(t, router, ch) // bound → armed

	router.interactionMu.Lock()
	armed := router.openInteractions[ch].pendingSynthesis != nil
	router.interactionMu.Unlock()
	require.True(t, armed, "precondition: the close is armed")

	// The operator disables the block while the close is armed.
	router.SetAutonomous(ch, AutonomousConfig{Enabled: false, MaxRounds: 2})

	router.interactionMu.Lock()
	stillArmed := router.openInteractions[ch].pendingSynthesis != nil
	router.interactionMu.Unlock()
	assert.False(t, stillArmed, "disabling the block disarms the pending synthesis close")

	// The orphaned timeout net (20ms) must never fire a close on the disabled channel.
	require.Never(t, func() bool {
		return closedCount(t, reader, structuralTrigger) > 0 || closedCount(t, reader, costTrigger) > 0
	}, 100*time.Millisecond, 10*time.Millisecond,
		"the stopped timer must not close the re-livened interaction")
}

// TestSynthesisClose_ChannelDeleteDisarmsAndForgetsPendingClose — PR #718
// review (findings audit): PurgeChannelInteraction is the exact call the
// channel-DELETE HTTP handler (internal/server/channel_delete_handlers.go)
// makes to stop an orphaned timeout net and forget the resolver entry when a
// channel is deleted mid-arm — one call, one interactionMu critical section
// (the follow-up review folded the disarm and the delete together so a full
// arm sequence cannot land a live timer in a gap between them). No test
// previously exercised it while a synthesis close was actually armed — the
// sibling disable-path (TestSynthesisClose_DisableDisarmsPendingClose) only
// covers SetAutonomous's disarm — so a regression in PurgeChannelInteraction
// itself (a reorder, a swallowed disarm, a dropped delete) would have gone
// uncaught by every existing test. Pins: after PurgeChannelInteraction, the
// pending synthesis is gone, the resolver entry itself is gone (the map-leak
// half of the fix), and the stopped timer never fires a close.
func TestSynthesisClose_ChannelDeleteDisarmsAndForgetsPendingClose(t *testing.T) {
	router, _, ch, reader := synthesisCloseHarness(t, 2)
	router.synthesisTimeout = 20 * time.Millisecond

	tick(t, router, ch)
	tick(t, router, ch) // bound → armed

	router.interactionMu.Lock()
	armed := router.openInteractions[ch].pendingSynthesis != nil
	router.interactionMu.Unlock()
	require.True(t, armed, "precondition: the close is armed")

	// The channel-delete handler's exact call.
	router.PurgeChannelInteraction(ch)

	router.interactionMu.Lock()
	entry, tracked := router.openInteractions[ch]
	router.interactionMu.Unlock()
	assert.Nil(t, entry, "the resolver entry itself is forgotten, not just disarmed")
	assert.False(t, tracked, "the map entry is gone, closing the per-deleted-channel leak")

	// The orphaned timeout net (20ms) must never fire a close on the deleted channel.
	require.Never(t, func() bool {
		return closedCount(t, reader, structuralTrigger) > 0 || closedCount(t, reader, costTrigger) > 0
	}, 100*time.Millisecond, 10*time.Millisecond,
		"the stopped timer must not close an interaction whose channel is gone")
}

// TestSynthesisClose_PurgeDischargesGovernanceState — PR #718 review
// follow-up: PurgeChannelInteraction dropped the openInteractions entry but
// never fired the per-interaction discard trio, and those maps are pruned ONLY
// by the one-generation-deferred seams (markInteractionClosed, the resolver's
// idle rotation) that key off the very entry the purge deletes — so a channel
// deleted mid-discussion stranded its open id's (and its pending retiree's)
// reply counters, end-vote tally, tombstone, and budget snapshot for the
// process lifetime: unbounded router growth across create-discuss-delete
// cycles. The pin: after the purge, every governance map has dropped BOTH
// generations.
func TestSynthesisClose_PurgeDischargesGovernanceState(t *testing.T) {
	router, _, ch, _ := synthesisCloseHarness(t, 2)

	tick(t, router, ch)
	openID, _, tracked := router.openInteractionEscalationState(ch)
	require.True(t, tracked, "precondition: an open committed interaction")

	// A retired predecessor whose deferred discharge is still pending, plus
	// live governance rows for both generations — the state a mid-discussion
	// delete strands.
	retiredID := uuid.NewString()
	router.interactionMu.Lock()
	router.openInteractions[ch].retired = retiredID
	router.interactionMu.Unlock()
	router.replyBudgetMu.Lock()
	router.replyCounts[openID] = map[string]int{"ember-owl": 1}
	router.replyCounts[retiredID] = map[string]int{"ember-owl": 2}
	router.replyBudgetMu.Unlock()
	router.endVoteMu.Lock()
	router.endVotes[openID] = &interactionEndVotes{}
	router.closedInteractions[retiredID] = struct{}{}
	router.endVoteMu.Unlock()
	router.budgetMu.Lock()
	router.interactionBudgetSnapshots[openID] = 1000
	router.interactionBudgetSnapshots[retiredID] = 1000
	router.budgetMu.Unlock()

	router.PurgeChannelInteraction(ch)

	router.replyBudgetMu.Lock()
	_, openCounts := router.replyCounts[openID]
	_, retiredCounts := router.replyCounts[retiredID]
	router.replyBudgetMu.Unlock()
	assert.False(t, openCounts, "the open id's reply counters are discharged with the entry")
	assert.False(t, retiredCounts, "the retiree's deferred reply-counter discharge fires now — no later close can")
	router.endVoteMu.Lock()
	_, votes := router.endVotes[openID]
	_, tomb := router.closedInteractions[retiredID]
	router.endVoteMu.Unlock()
	assert.False(t, votes, "the open id's end-vote tally is discharged")
	assert.False(t, tomb, "the retiree's tombstone is discharged — no commit can race a deleted channel's close")
	router.budgetMu.Lock()
	_, openSnap := router.interactionBudgetSnapshots[openID]
	_, retiredSnap := router.interactionBudgetSnapshots[retiredID]
	router.budgetMu.Unlock()
	assert.False(t, openSnap, "the open id's budget snapshot is discharged")
	assert.False(t, retiredSnap, "the retiree's budget snapshot is discharged")
}

// TestSynthesisClose_TimeoutAbandonsCloseAfterMaxRoundsRaise — PR #718 review
// follow-up: maybeBoundedClose's fresh-config contract says the bound is only
// ever ACTED on against the CURRENT config — Enabled AND MaxRounds — and its
// tail re-check covers raises up to the arm. The timeout net is the OTHER
// action point, and it re-checked only Enabled: a `max_rounds` raise landing
// inside the armed window (which the raise does NOT disarm — only a disable
// does) was silently ignored, and a lost chair reply force-closed the
// discussion against the old bound up to the full reply timeout after the
// operator extended it. The pin: the fire abandons (the disable branch's
// posture), the interaction stays open with the withhold lifted, and the
// frozen tally survives to resume under the raised bound.
func TestSynthesisClose_TimeoutAbandonsCloseAfterMaxRoundsRaise(t *testing.T) {
	router, _, ch, reader := synthesisCloseHarness(t, 2)
	router.synthesisTimeout = 20 * time.Millisecond

	tick(t, router, ch)
	openID, _, _ := router.openInteractionEscalationState(ch)
	tick(t, router, ch) // bound → armed

	router.interactionMu.Lock()
	armed := router.openInteractions[ch].pendingSynthesis != nil
	router.interactionMu.Unlock()
	require.True(t, armed, "precondition: the close is armed")

	// The operator raises the bound mid-arm; enabled stays true, so nothing
	// disarms — only the fire-time re-check can honour the raise.
	router.SetAutonomous(ch, AutonomousConfig{
		Enabled: true, MaxRounds: 50, Convener: "ember-owl",
		Topic: "Adopt a monorepo?", Goal: "A synthesized recommendation.",
	})

	require.Never(t, func() bool {
		return closedCount(t, reader, structuralTrigger) > 0 || closedCount(t, reader, costTrigger) > 0
	}, 150*time.Millisecond, 10*time.Millisecond,
		"the raise extended the discussion — the net must not close it against the old bound")

	router.interactionMu.Lock()
	entry := router.openInteractions[ch]
	stillArmed := entry != nil && entry.pendingSynthesis != nil
	stillOpen := entry != nil && entry.id == openID
	router.interactionMu.Unlock()
	assert.False(t, stillArmed, "the fire abandons the arm — the withhold must not outlive it")
	assert.True(t, stillOpen, "the interaction stays open and resumes under the raised bound")
}

// TestSynthesisClose_EntryMovedOnFallsThroughNotWithheld — PR #718 review
// finding 4: when the resolver entry rotates or closes between the bound's
// tally advance and the arm, maybeArmSynthesisClose must report
// synthesisEntryMovedOn (so the caller falls through to the immediate close and
// the tombstone CAS decides), NOT synthesisAlreadyArmed (the deliberate-close
// withhold, which silently swallows a live committed message on a benign
// rotation). White-box, because the interleaving is a sub-lock race: it drives
// the branch → outcome mapping directly. The end-to-end CAS behaviour (a benign
// rotation delivers, a racing close reports stale) is boundedClose's own.
func TestSynthesisClose_EntryMovedOnFallsThroughNotWithheld(t *testing.T) {
	router, disp, ch, _ := synthesisCloseHarness(t, 2)
	members, err := router.store.GetMembers(context.Background(), ch)
	require.NoError(t, err)
	a := router.AutonomousFor(ch)
	msg := ChannelMessage{ID: uuid.NewString(), ChannelID: ch, SenderID: "operator", Content: "bound"}

	// No entry at all (the interaction closed and its entry was dropped).
	got := router.maybeArmSynthesisClose(context.Background(), msg, ChannelTypeGroup,
		members, len(members), "gone", structuralTrigger, false, nil, a)
	assert.Equal(t, synthesisEntryMovedOn, got, "a missing entry falls through to the immediate close")

	// An entry that moved on to a DIFFERENT id (rotation / fresh mint under the arm).
	router.interactionMu.Lock()
	router.openInteractions[ch] = &openInteraction{id: "successor", idCommitted: true}
	router.interactionMu.Unlock()
	got = router.maybeArmSynthesisClose(context.Background(), msg, ChannelTypeGroup,
		members, len(members), "retired", structuralTrigger, false, nil, a)
	assert.Equal(t, synthesisEntryMovedOn, got, "a moved-on id falls through, never the deliberate-close withhold")

	assert.Empty(t, disp.synthesisTurns(), "a moved-on arm dispatches no synthesis turn")
	router.interactionMu.Lock()
	stillArmed := router.openInteractions[ch].pendingSynthesis != nil
	router.interactionMu.Unlock()
	assert.False(t, stillArmed, "a moved-on arm leaves nothing armed on the successor")
}

// TestSynthesisClose_RacingEndVoteClearsChairMark — this review: a racing
// end-vote quorum keeps its close supremacy (CE4) and disarms the pending
// synthesis through the shared close seam (finalizeInteractionClose →
// markInteractionClosed), which stops the timeout net's timer AND latches the
// interaction closed. The arm marked the chair composing its synthesis turn
// (maybeArmSynthesisClose → markActivity), and — unlike the timeout-net and
// disable disarm terminals — this racing-close disarm used to omit the paired
// clearActivity. With the timer dead and the chair's reply now latch-suppressed
// (it never re-enters publishCommit), nothing else cleared the mark, stranding
// the chair as "thinking" for the whole activity TTL on a channel whose
// interaction had already closed. The pin: the racing-close disarm clears the
// chair's mark, matching its three sibling disarm sites.
func TestSynthesisClose_RacingEndVoteClearsChairMark(t *testing.T) {
	router, _, ch, _ := synthesisCloseHarness(t, 2)

	tick(t, router, ch)
	openID, _, _ := router.openInteractionEscalationState(ch)
	tick(t, router, ch) // bound → synthesis turn dispatched, chair marked "thinking"

	router.interactionMu.Lock()
	armed := router.openInteractions[ch].pendingSynthesis != nil
	router.interactionMu.Unlock()
	require.True(t, armed, "precondition: the close is armed")
	require.Contains(t, router.ChannelActivity(ch), "iron-fox",
		"precondition: the arm marked the chair composing the synthesis turn")

	// An end-vote quorum races the arm and closes the SAME interaction through
	// the seam both deterministic close causes route through.
	router.markInteractionClosed(ch, openID, endVotesTrigger)

	router.interactionMu.Lock()
	stillArmed := router.openInteractions[ch].pendingSynthesis != nil
	router.interactionMu.Unlock()
	assert.False(t, stillArmed, "the racing close disarms the pending synthesis (CE4 supremacy)")
	assert.NotContains(t, router.ChannelActivity(ch), "iron-fox",
		"the racing-close disarm clears the chair's mark — no thinking indicator stranded for the TTL")
}
