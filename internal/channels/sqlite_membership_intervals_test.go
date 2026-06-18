// RFC 0035 PR 3 — the transactional write hooks that keep the
// `membership_intervals` ledger live and exact. PR 1 landed the table dormant,
// PR 2 the read surface ([GetMembershipIntervals] / [InScope]); these tests pin
// the four `memberships`-mutating call sites that must each maintain the ledger
// in the same transaction:
//
//   - AddMember               — opens one interval on a genuine insert, none on
//     a redundant re-add (RowsAffected==0).
//   - RemoveMember            — closes the open interval; a zero-row close is a
//     loud invariant breach (errMembershipLedgerDivergence).
//   - GetOrCreateDM           — opens one interval per DM participant.
//   - CreateChannelWithMembers — opens one interval per initial member.
//
// The RFC §C narrative enumerates only the first three ("three call sites
// mutate memberships today"), but CreateChannelWithMembers (sqlite.go) is a
// fourth — the atomic create path the REST handler and config reconcile use to
// seed every config-declared channel. Omitting it would leave those members
// with no interval (RFC 0036 recall silently broken for them) and make a later
// RemoveMember trip the divergence guard — a reachable REST 500. The
// TestCreateChannelWithMembers_* cases below pin that fourth hook.
//
// Goal 6 (RFC 0035) is the correctness core: at most one OPEN interval per
// (channel_id, participant_id) at any time, and every present member has
// exactly one — so RemoveMember's close finds exactly one row on the success
// path. The atomicity tests prove each interval write rides its memberships
// mutation's transaction: a forced failure rolls back both tables.
package channels

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"

	_ "modernc.org/sqlite"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestAddMember_OpensOneOpenInterval pins Goal 2: a genuine AddMember opens
// exactly one OPEN interval whose joined_at equals the memberships row's
// joined_at (the same `now` is written to both inside one tx).
func TestAddMember_OpensOneOpenInterval(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	}))

	require.NoError(t, store.AddMember(ctx, "group:planning", "alice", RespondWhenMentioned))

	ivs, err := store.GetMembershipIntervals(ctx, "group:planning", "alice")
	require.NoError(t, err)
	require.Len(t, ivs, 1, "a genuine add opens exactly one interval")
	assert.True(t, ivs[0].LeftAt.IsZero(), "the opened interval is open (NULL left_at)")

	m, err := store.GetMember(ctx, "group:planning", "alice")
	require.NoError(t, err)
	assert.True(t, ivs[0].JoinedAt.Equal(m.JoinedAt),
		"interval joined_at == memberships.joined_at (same now, same tx)")
}

// TestAddMember_RedundantAdd_NoSecondInterval pins the RowsAffected==0 path: a
// redundant add on a present member no-ops on both tables — no second interval,
// the existing open one stays correct.
func TestAddMember_RedundantAdd_NoSecondInterval(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	}))

	require.NoError(t, store.AddMember(ctx, "group:planning", "alice", RespondWhenMentioned))
	require.NoError(t, store.AddMember(ctx, "group:planning", "alice", RespondAlways)) // redundant

	ivs, err := store.GetMembershipIntervals(ctx, "group:planning", "alice")
	require.NoError(t, err)
	require.Len(t, ivs, 1, "a redundant add opens no second interval (RowsAffected==0 path)")
	assert.True(t, ivs[0].LeftAt.IsZero())
}

// TestRemoveMember_ClosesOpenInterval pins Goal 2's close half: RemoveMember
// stamps left_at on the open interval, leaves joined_at unchanged, and the
// append-only row persists (closed, not deleted).
func TestRemoveMember_ClosesOpenInterval(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice")

	before, err := store.GetMembershipIntervals(ctx, id, "alice")
	require.NoError(t, err)
	require.Len(t, before, 1)
	require.True(t, before[0].LeftAt.IsZero())

	require.NoError(t, store.RemoveMember(ctx, id, "alice"))

	after, err := store.GetMembershipIntervals(ctx, id, "alice")
	require.NoError(t, err)
	require.Len(t, after, 1, "the interval persists (append-only) but is now closed")
	assert.False(t, after[0].LeftAt.IsZero(), "left_at stamped on removal")
	assert.True(t, after[0].JoinedAt.Equal(before[0].JoinedAt), "joined_at unchanged")
	assert.False(t, after[0].LeftAt.Before(after[0].JoinedAt), "left_at >= joined_at")
}

// TestMembershipLedger_JoinLeaveRejoin_TwoStints pins Goal 3: a join → leave →
// rejoin cycle yields two distinct, non-overlapping intervals — one closed, one
// open — the history the current-state `memberships` projection destroys.
func TestMembershipLedger_JoinLeaveRejoin_TwoStints(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning") // empty group

	require.NoError(t, store.AddMember(ctx, id, "alice", RespondWhenMentioned)) // stint 1 open
	require.NoError(t, store.RemoveMember(ctx, id, "alice"))                    // stint 1 closed
	require.NoError(t, store.AddMember(ctx, id, "alice", RespondWhenMentioned)) // stint 2 open

	ivs, err := store.GetMembershipIntervals(ctx, id, "alice")
	require.NoError(t, err)
	require.Len(t, ivs, 2, "join → leave → rejoin yields two stints")

	// Ordered by joined_at ASC: [0] is the earlier, closed stint; [1] the later,
	// open one.
	assert.False(t, ivs[0].LeftAt.IsZero(), "stint 1 is closed")
	assert.True(t, ivs[1].LeftAt.IsZero(), "stint 2 is open")
	assert.False(t, ivs[1].JoinedAt.Before(ivs[0].LeftAt),
		"stints do not overlap: stint-2 join is at/after stint-1 leave")
}

// TestGetOrCreateDM_OpensIntervalPerParticipant pins the DM hook: creating a DM
// opens one OPEN interval per participant, each joined_at at the DM's creation
// time. DM membership is never removed in normal operation, so these stay open.
func TestGetOrCreateDM_OpensIntervalPerParticipant(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()

	ch, err := store.GetOrCreateDM(ctx, "agent-a", "agent-b")
	require.NoError(t, err)

	for _, p := range []string{"agent-a", "agent-b"} {
		ivs, err := store.GetMembershipIntervals(ctx, ch.ID, p)
		require.NoError(t, err)
		require.Lenf(t, ivs, 1, "DM participant %s has exactly one interval", p)
		assert.Truef(t, ivs[0].LeftAt.IsZero(), "DM interval for %s is open", p)
		assert.Truef(t, ivs[0].JoinedAt.Equal(ch.CreatedAt),
			"DM interval joined_at for %s == DM creation time", p)
	}
}

// TestCreateChannelWithMembers_OpensIntervalPerMember pins the FOURTH hook the
// RFC §C narrative omits: a channel created with initial members (the
// config-reconcile / REST atomic-create path) seeds one OPEN interval per
// member, exactly as AddMember would. Without this hook the ledger is silently
// incomplete for every config-declared channel — RFC 0036 recall would find no
// interval to join and return nothing for those personas.
func TestCreateChannelWithMembers_OpensIntervalPerMember(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()

	require.NoError(t, store.CreateChannelWithMembers(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	}, []Member{
		{ParticipantID: "alice", RespondPolicy: RespondAlways},
		{ParticipantID: "bob", RespondPolicy: RespondWhenMentioned},
	}))

	for _, p := range []string{"alice", "bob"} {
		ivs, err := store.GetMembershipIntervals(ctx, "group:planning", p)
		require.NoError(t, err)
		require.Lenf(t, ivs, 1, "initial member %s gets exactly one open interval", p)
		assert.Truef(t, ivs[0].LeftAt.IsZero(), "initial member %s interval is open", p)

		m, err := store.GetMember(ctx, "group:planning", p)
		require.NoError(t, err)
		assert.Truef(t, ivs[0].JoinedAt.Equal(m.JoinedAt),
			"initial member %s interval joined_at == memberships.joined_at", p)
	}
}

// TestCreateChannelWithMembers_ThenRemoveMember_ClosesInterval is the
// consequence test for the fourth hook: removing an initial member must close
// its interval cleanly, NOT trip the divergence guard. Without the
// CreateChannelWithMembers hook the member has no open interval, so
// RemoveMember's zero-row close fires errMembershipLedgerDivergence — a
// reachable REST 500 (POST a channel with members, DELETE a member).
func TestCreateChannelWithMembers_ThenRemoveMember_ClosesInterval(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()

	require.NoError(t, store.CreateChannelWithMembers(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	}, []Member{{ParticipantID: "alice", RespondPolicy: RespondAlways}}))

	require.NoError(t, store.RemoveMember(ctx, "group:planning", "alice"),
		"removing an initial member must not trip the ledger-divergence guard")

	ivs, err := store.GetMembershipIntervals(ctx, "group:planning", "alice")
	require.NoError(t, err)
	require.Len(t, ivs, 1)
	assert.False(t, ivs[0].LeftAt.IsZero(), "the initial member's interval is closed on removal")
}

// TestAddMember_OpensInterval_Atomically proves the interval open rides the
// memberships insert's transaction. We pre-seed a dangling OPEN interval (no
// memberships row) so the AddMember interval-open collides with
// ux_membership_intervals_open and fails; the whole tx — including the
// memberships insert — must roll back, leaving neither table moved.
func TestAddMember_OpensInterval_Atomically(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	}))

	// Dangling OPEN interval with no membership row — the forced-failure seed.
	withDB(t, path, func(db *sql.DB) {
		_, err := db.Exec(
			`INSERT INTO membership_intervals (channel_id, participant_id, joined_at, left_at)
			   VALUES ('group:planning', 'alice', '2026-01-01T00:00:00Z', NULL)`)
		require.NoError(t, err)
	})

	err = store.AddMember(ctx, "group:planning", "alice", RespondWhenMentioned)
	require.Error(t, err, "the second open interval must fail the unique index and the add")

	isMember, err := store.IsMember(ctx, "group:planning", "alice")
	require.NoError(t, err)
	assert.False(t, isMember,
		"the memberships insert rolled back with the failed interval open (atomic)")

	ivs, err := store.GetMembershipIntervals(ctx, "group:planning", "alice")
	require.NoError(t, err)
	assert.Len(t, ivs, 1, "no second interval was committed — only the pre-seeded one remains")
}

// TestRemoveMember_LedgerDivergence_RollsBack pins the loud-failure posture of
// §C: a memberships row with no matching open interval (Goal 6 breach) makes
// RemoveMember's close affect zero rows. It MUST return
// errMembershipLedgerDivergence and roll the transaction back rather than
// silently delete the row — a never-closing interval would be a data-exposure
// bug for RFC 0036.
func TestRemoveMember_LedgerDivergence_RollsBack(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	}))

	// Synthesize divergence: a memberships row with NO open interval.
	withDB(t, path, func(db *sql.DB) {
		_, err := db.Exec(
			`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at)
			   VALUES ('group:planning', 'ghost', 'always', '2026-01-01T00:00:00Z')`)
		require.NoError(t, err)
	})

	err = store.RemoveMember(ctx, "group:planning", "ghost")
	require.Error(t, err)
	assert.ErrorIs(t, err, errMembershipLedgerDivergence,
		"a zero-row close is a loud invariant breach, not a silent commit")

	isMember, err := store.IsMember(ctx, "group:planning", "ghost")
	require.NoError(t, err)
	assert.True(t, isMember,
		"RemoveMember rolled back; the memberships row survives the divergence guard")
}

// TestRemoveMember_NotPresent_IsNotDivergence guards the boundary: removing a
// never-present participant is the expected ErrNotMember no-op (the DELETE
// affects zero memberships rows), NOT the ledger-divergence breach (which is a
// memberships row that DID delete but had no interval to close).
func TestRemoveMember_NotPresent_IsNotDivergence(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice")

	err := store.RemoveMember(ctx, id, "ghost")
	assert.ErrorIs(t, err, ErrNotMember, "absent member is ErrNotMember")
	assert.NotErrorIs(t, err, errMembershipLedgerDivergence,
		"member-not-present is the expected no-op, not the invariant breach")
}
