package channels

// synthesis_close_demotion_test.go — the DEMOTED synthesis reply: a marked
// chair vote whose arm is already gone (abandoned by a mid-arm RFC 0050
// disable, or a max_rounds raise honored at the timeout fire) is refused by
// the commit-path claim and falls through to processEndVote as an ordinary
// quorum vote. Split out of synthesis_close_races_test.go at the 500-line
// review cap (the synthesis_close_guard_test.go precedent) — the harness,
// chairVote/memberVote publishers, and counters are shared from that family.

import (
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestSynthesisClose_DemotedQuorumVoteNotifiesTheChair — PR #718 review: a
// chair's synthesis-reply vote DEMOTED to an ordinary quorum vote (the arm
// abandoned by a mid-arm disable) completes the K=2 quorum in processEndVote —
// and the end-vote fan used to exclude its sender, while the chair's Python
// discharge had already deferred its local close to exactly that self-echo
// (vote_close.py's synthesis_reply carve-out, which keys on the wire echo, not
// on Go's acceptance): the one close whose fan skipped the one member whose
// record was still open, stranding the chair's record until a late, mislabeled
// idle bury. The fan now keys excludeSender off the vote's wire marker, so the
// demoted shape self-echoes the chair; unmarked votes keep the exclusion
// byte-for-byte.
func TestSynthesisClose_DemotedQuorumVoteNotifiesTheChair(t *testing.T) {
	router, disp, ch, reader := synthesisCloseHarness(t, 3)
	router.synthesisTimeout = time.Hour

	tick(t, router, ch) // round 1
	openID, _, _ := router.openInteractionEscalationState(ch)
	memberVote(t, router, ch, "ember-owl", openID) // 1 of K=2, in-window; round 2
	tick(t, router, ch)                            // round 3 = bound → arm
	require.Len(t, disp.synthesisTurns(), 1, "the bound armed and dispatched the synthesis turn")

	// The operator disables mid-arm: the arm is abandoned, the interaction
	// stays open, and the chair's in-flight synthesis vote demotes to an
	// ordinary quorum vote at the (now config-refusing) claim.
	router.SetAutonomous(ch, AutonomousConfig{Enabled: false})
	require.Empty(t, router.armedSynthesisChair(ch), "the disable disarmed the arm")

	chairVote(t, router, ch, "iron-fox", openID, "Synthesis: adopt the monorepo.")
	router.WaitForPendingFanout()

	assert.Equal(t, int64(1), closedCount(t, reader, endVotesTrigger),
		"the demoted vote completes the ordinary quorum — the end-vote close, not a bounded one")
	assert.Zero(t, synthesisTurnCount(t, reader, "closed_on_reply"))
	notifications := disp.closeNotifications()
	require.NotEmpty(t, notifications)
	recipients := make(map[string]bool, len(notifications))
	for _, c := range notifications {
		recipients[c.env.Recipient.ParticipantID] = true
		assert.Empty(t, c.env.InteractionCloseTrigger,
			"an end_votes close keeps the unmetered wire shape — no bounded trigger for anyone")
	}
	assert.True(t, recipients["iron-fox"],
		"the fan includes the voting chair — its discharge deferred the local close to this self-echo")
	assert.True(t, recipients["ember-owl"], "non-sender members are notified as before")
}
