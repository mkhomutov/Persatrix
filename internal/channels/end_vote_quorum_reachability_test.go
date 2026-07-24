package channels

// end_vote_quorum_reachability_test.go — ISSUE-0109 follow-up. Pins the K/W
// relationship the full-roster `end_vote_threshold` calibration depends on.
//
// W is a recency window over *tracked publishes*, not over votes:
// [ChannelRouter.processEndVote] advances `state.turn` on EVERY tracked publish
// and counts a vote only while `state.turn - voteTurn < w`. At most W votes are
// therefore live simultaneously, which fixes the reachability of any quorum:
//
//	K >  W  → can NEVER close (Layer 4 silently disabled for the channel)
//	K == W  → closes only on K strictly back-to-back votes
//	K <  W  → closes with room for ordinary turns between votes
//
// The shipped autonomous templates raise K to the full roster, so they must
// raise W with it (config/channels.yaml + both blueprints use W = 2K). These
// tests are the regression net: they would have caught the K=4/W=3 combination
// that shipped in the first cut of the calibration.

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
)

// closedByVotes collects the `interaction_closed{trigger=end_votes}` count.
func closedByVotes(t *testing.T, reader interface {
	Collect(context.Context, *metricdata.ResourceMetrics) error
}) int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	return interactionClosedCount(t, rm, "group", endVotesTrigger)
}

// TestEndVoteQuorum_KGreaterThanWindowNeverCloses — the bug the calibration
// shipped with. K=4 against the default W=3 cannot close even when all four
// seats vote on consecutive publishes: the first vote ages out of the window as
// the fourth lands, so the live count peaks at W=3 and never reaches K.
func TestEndVoteQuorum_KGreaterThanWindowNeverCloses(t *testing.T) {
	router, store, reader := routerWithInteractionClosedMetric(t)
	ch := mustCreateGroup(t, store, "planning", "alice", "bob", "carol", "dave")
	router.SetEndVoteParams(ch, 4, 3) // K > W — unreachable

	for _, voter := range []string{"alice", "bob", "carol", "dave"} {
		require.NoError(t, endVote(t, router, ch, voter, "int-1"))
	}

	assert.Equal(t, int64(0), closedByVotes(t, reader),
		"K=4 with W=3 is unreachable: at most W votes are ever live at once")
}

// TestEndVoteQuorum_KEqualToWindowIsKnifeEdge — K == W closes on strictly
// back-to-back votes, but a single ordinary turn between them ages the first
// vote out and the quorum is missed. This is why the full-roster templates do
// not simply keep the default W.
func TestEndVoteQuorum_KEqualToWindowIsKnifeEdge(t *testing.T) {
	t.Run("back to back closes", func(t *testing.T) {
		router, store, reader := routerWithInteractionClosedMetric(t)
		ch := mustCreateGroup(t, store, "planning", "alice", "bob", "carol")
		router.SetEndVoteParams(ch, 3, 3)

		for _, voter := range []string{"alice", "bob", "carol"} {
			require.NoError(t, endVote(t, router, ch, voter, "int-1"))
		}

		assert.Equal(t, int64(1), closedByVotes(t, reader),
			"three strictly consecutive votes exactly fill the W=3 window")
	})

	t.Run("one ordinary turn between votes misses the quorum", func(t *testing.T) {
		router, store, reader := routerWithInteractionClosedMetric(t)
		ch := mustCreateGroup(t, store, "planning", "alice", "bob", "carol")
		router.SetEndVoteParams(ch, 3, 3)

		require.NoError(t, endVote(t, router, ch, "alice", "int-1"))
		require.NoError(t, plainTurn(t, router, ch, "bob", "int-1")) // ages alice out
		require.NoError(t, endVote(t, router, ch, "bob", "int-1"))
		require.NoError(t, endVote(t, router, ch, "carol", "int-1"))

		assert.Equal(t, int64(0), closedByVotes(t, reader),
			"a single interleaved publish drops the live count below K")
	})
}

// TestEndVoteQuorum_FullRosterClosesWhenWindowScales — the shipped fix. With
// W = 2K a unanimous roster gets two full rounds of publishes to register its
// votes, so the full-roster bar is a high bar rather than an impossible one.
func TestEndVoteQuorum_FullRosterClosesWhenWindowScales(t *testing.T) {
	t.Run("roundtable K=3 W=6", func(t *testing.T) {
		router, store, reader := routerWithInteractionClosedMetric(t)
		ch := mustCreateGroup(t, store, "planning", "alice", "bob", "carol")
		router.SetEndVoteParams(ch, 3, 6) // the shipped template values

		require.NoError(t, endVote(t, router, ch, "alice", "int-1"))
		require.NoError(t, plainTurn(t, router, ch, "bob", "int-1"))
		require.NoError(t, endVote(t, router, ch, "bob", "int-1"))
		require.NoError(t, plainTurn(t, router, ch, "carol", "int-1"))
		require.NoError(t, endVote(t, router, ch, "carol", "int-1"))

		assert.Equal(t, int64(1), closedByVotes(t, reader),
			"W=2K tolerates ordinary turns interleaved with a unanimous vote")
	})

	t.Run("multivendor K=4 W=8", func(t *testing.T) {
		router, store, reader := routerWithInteractionClosedMetric(t)
		ch := mustCreateGroup(t, store, "planning", "alice", "bob", "carol", "dave")
		router.SetEndVoteParams(ch, 4, 8) // the shipped blueprint values

		for _, voter := range []string{"alice", "bob", "carol"} {
			require.NoError(t, endVote(t, router, ch, voter, "int-1"))
			require.NoError(t, plainTurn(t, router, ch, voter, "int-1"))
		}
		require.NoError(t, endVote(t, router, ch, "dave", "int-1"))

		assert.Equal(t, int64(1), closedByVotes(t, reader),
			"the four-seat roster can actually reach its full-roster quorum")
	})
}
