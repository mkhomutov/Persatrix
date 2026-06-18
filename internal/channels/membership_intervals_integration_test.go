// RFC 0035 PR 3 — integration coverage for the membership-interval ledger
// through the public store API (no direct SQL). Exercises the full add → remove
// → re-add lifecycle end-to-end and confirms the live-produced intervals
// compose with the [InScope] predicate the way RFC 0036 recall will rely on.
package channels

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestMembershipIntervals_Integration_AddRemoveReadd drives the lifecycle purely
// through the store API and asserts GetMembershipIntervals returns the two-stint
// history in joined_at order, then that InScope classifies live timestamps
// against it — a pre-join instant is out of scope, a join instant is in, and a
// post-re-add instant is in. (The half-open boundary and removal-gap semantics
// are pinned exhaustively against controlled fixtures in
// membership_intervals_test.go; here we confirm the live hooks produce intervals
// that feed the same predicate.)
func TestMembershipIntervals_Integration_AddRemoveReadd(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning") // empty group

	require.NoError(t, store.AddMember(ctx, id, "alice", RespondWhenMentioned))
	require.NoError(t, store.RemoveMember(ctx, id, "alice"))
	require.NoError(t, store.AddMember(ctx, id, "alice", RespondWhenMentioned))

	ivs, err := store.GetMembershipIntervals(ctx, id, "alice")
	require.NoError(t, err)
	require.Len(t, ivs, 2, "the store API surfaces both stints")
	require.False(t, ivs[0].LeftAt.IsZero(), "first stint is closed")
	require.True(t, ivs[1].LeftAt.IsZero(), "second stint is open")

	// The live-produced intervals compose with the recall predicate.
	preJoin := ivs[0].JoinedAt.Add(-time.Hour)
	assert.False(t, InScope(ivs, preJoin), "before the first join is out of scope")
	assert.True(t, InScope(ivs, ivs[0].JoinedAt), "the first join instant is in scope")
	assert.True(t, InScope(ivs, ivs[1].JoinedAt.Add(time.Hour)), "after the re-add is in scope")
}
