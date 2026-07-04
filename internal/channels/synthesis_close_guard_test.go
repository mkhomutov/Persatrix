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
		members, len(members), "gone", structuralTrigger, false, a)
	assert.Equal(t, synthesisEntryMovedOn, got, "a missing entry falls through to the immediate close")

	// An entry that moved on to a DIFFERENT id (rotation / fresh mint under the arm).
	router.interactionMu.Lock()
	router.openInteractions[ch] = &openInteraction{id: "successor", idCommitted: true}
	router.interactionMu.Unlock()
	got = router.maybeArmSynthesisClose(context.Background(), msg, ChannelTypeGroup,
		members, len(members), "retired", structuralTrigger, false, a)
	assert.Equal(t, synthesisEntryMovedOn, got, "a moved-on id falls through, never the deliberate-close withhold")

	assert.Empty(t, disp.synthesisTurns(), "a moved-on arm dispatches no synthesis turn")
	router.interactionMu.Lock()
	stillArmed := router.openInteractions[ch].pendingSynthesis != nil
	router.interactionMu.Unlock()
	assert.False(t, stillArmed, "a moved-on arm leaves nothing armed on the successor")
}
