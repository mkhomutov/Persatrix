// RFC 0035 PR 2 — the read surface over the `membership_intervals` ledger:
// the [MembershipInterval] type, the [GetMembershipIntervals] read method, and
// the [InScope] predicate helper. PR 1 landed the table dormant; PR 3 wires the
// transactional write hooks. Because the write hooks do not exist yet, these
// tests source interval rows two ways, both already established by the PR 1
// migration tests: the §D backfill (a v8 DB with `memberships` rows reopened as
// v9) for the snapshot read, and direct `membership_intervals` INSERTs for the
// hand-built join → leave → rejoin history a live ledger would later produce.
//
// The half-open `[joined_at, left_at)` predicate (§F) is the correctness core —
// a message at the exact join instant is in scope, one at the exact leave
// instant is not — so a back-to-back leave-then-rejoin is unambiguous. The
// [InScope] table tests pin every boundary; RFC 0036's SQL `EXISTS` clause is
// the same predicate expressed as a join and is tested against the same shapes.
package channels

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"
	"time"

	_ "modernc.org/sqlite"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// mustRFC3339 parses an RFC3339 instant or fails the test — the interval
// fixtures are authored as readable UTC literals.
func mustRFC3339(t *testing.T, s string) time.Time {
	t.Helper()
	ts, err := time.Parse(time.RFC3339, s)
	require.NoError(t, err)
	return ts
}

// openInterval / closedInterval build fixtures for the pure [InScope] tests.
func openInterval(joined time.Time) MembershipInterval {
	return MembershipInterval{ChannelID: "group:planning", ParticipantID: "alice", JoinedAt: joined}
}
func closedInterval(joined, left time.Time) MembershipInterval {
	iv := openInterval(joined)
	iv.LeftAt = left
	return iv
}

// TestInScope_HalfOpenPredicate pins §F: in scope iff some interval satisfies
// `joined_at <= T AND (left_at IS NULL OR T < left_at)`. The boundary rows are
// the point of the test — `t == joined_at` is IN, `t == left_at` is OUT.
func TestInScope_HalfOpenPredicate(t *testing.T) {
	t0 := mustRFC3339(t, "2026-01-01T00:00:00Z") // stint-1 join
	t1 := mustRFC3339(t, "2026-01-10T00:00:00Z") // stint-1 leave
	t2 := mustRFC3339(t, "2026-01-20T00:00:00Z") // stint-2 (re-add) join

	// A join → leave → rejoin history: one closed [t0,t1), one open [t2,NULL).
	rejoin := []MembershipInterval{closedInterval(t0, t1), openInterval(t2)}

	tests := []struct {
		name      string
		intervals []MembershipInterval
		at        time.Time
		want      bool
	}{
		// Empty ledger — never in scope.
		{"empty slice is never in scope", nil, t0, false},

		// Single open interval [t0, NULL): closed below, open above.
		{"before an open interval's join", []MembershipInterval{openInterval(t1)}, t0, false},
		{"at an open interval's join instant (inclusive)", []MembershipInterval{openInterval(t1)}, t1, true},
		{"after an open interval's join", []MembershipInterval{openInterval(t1)}, t2, true},

		// Single closed interval [t0,t1): half-open both ends.
		{"before a closed interval", []MembershipInterval{closedInterval(t0, t1)}, mustRFC3339(t, "2025-12-31T23:59:59Z"), false},
		{"at a closed interval's join instant (inclusive)", []MembershipInterval{closedInterval(t0, t1)}, t0, true},
		{"inside a closed interval", []MembershipInterval{closedInterval(t0, t1)}, mustRFC3339(t, "2026-01-05T00:00:00Z"), true},
		{"at a closed interval's leave instant (exclusive)", []MembershipInterval{closedInterval(t0, t1)}, t1, false},
		{"after a closed interval", []MembershipInterval{closedInterval(t0, t1)}, t2, false},

		// Join → leave → rejoin: pre-join out, stint-1 in, gap out, rejoin in.
		{"pre-join is out", rejoin, mustRFC3339(t, "2025-12-01T00:00:00Z"), false},
		{"during stint 1 is in", rejoin, mustRFC3339(t, "2026-01-05T00:00:00Z"), true},
		{"in the removal gap is out", rejoin, mustRFC3339(t, "2026-01-15T00:00:00Z"), false},
		{"at the re-add join instant is in", rejoin, t2, true},
		{"after the re-add is in", rejoin, mustRFC3339(t, "2026-02-01T00:00:00Z"), true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			assert.Equal(t, tc.want, InScope(tc.intervals, tc.at))
		})
	}
}

// TestInScope_BackToBackLeaveRejoin pins the "unambiguous" claim of §F: when a
// leave and the next join share an instant, a message at that instant falls in
// exactly one interval (the new open one), never in both or neither.
func TestInScope_BackToBackLeaveRejoin(t *testing.T) {
	t0 := mustRFC3339(t, "2026-01-01T00:00:00Z")
	seam := mustRFC3339(t, "2026-01-10T00:00:00Z") // left_at of stint 1 == joined_at of stint 2
	intervals := []MembershipInterval{closedInterval(t0, seam), openInterval(seam)}

	// At the seam: excluded by the closed interval (T == left_at), included by
	// the open one (joined_at <= T, still open) ⇒ in scope, unambiguously.
	assert.True(t, InScope(intervals, seam), "the shared instant belongs to the new open stint")
	// One nanosecond before the seam is the closed stint only; still in scope.
	assert.True(t, InScope(intervals, seam.Add(-time.Nanosecond)))
}

// TestGetMembershipIntervals_BackfilledSnapshot reads the §D backfill through
// the PR 2 method: a v8 store with two members reopened as v9 exposes exactly
// one OPEN interval per current member, each carrying its source `joined_at`.
func TestGetMembershipIntervals_BackfilledSnapshot(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")

	db := buildV8DB(t, path)
	_, err := db.Exec(
		`INSERT INTO channels (id, name, channel_type, description, created_at, session_id, epoch_id)
		   VALUES ('group:planning', 'planning', 'group', '', '2026-01-01T00:00:00Z', 'legacy', 'live')`)
	require.NoError(t, err)
	_, err = db.Exec(
		`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at)
		   VALUES ('group:planning', 'alice', 'always', '2026-01-01T00:00:00Z')`)
	require.NoError(t, err)
	require.NoError(t, db.Close())

	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	got, err := store.GetMembershipIntervals(context.Background(), "group:planning", "alice")
	require.NoError(t, err)
	require.Len(t, got, 1, "the backfill seeds exactly one interval per current member")
	assert.Equal(t, "group:planning", got[0].ChannelID)
	assert.Equal(t, "alice", got[0].ParticipantID)
	assert.True(t, got[0].JoinedAt.Equal(mustRFC3339(t, "2026-01-01T00:00:00Z")),
		"backfilled joined_at equals the source membership row")
	assert.True(t, got[0].LeftAt.IsZero(), "a backfilled interval is open (NULL left_at ⇒ zero Time)")
}

// TestGetMembershipIntervals_UnknownPair returns an empty slice (not an error)
// for a `(channel, participant)` with no intervals — the read is a clean lookup,
// not a membership assertion.
func TestGetMembershipIntervals_UnknownPair(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	require.NoError(t, store.CreateChannel(context.Background(), Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	}))

	got, err := store.GetMembershipIntervals(context.Background(), "group:planning", "nobody")
	require.NoError(t, err)
	assert.Empty(t, got, "an unknown pair reads back as an empty interval list, no error")
}

// TestGetMembershipIntervals_TwoStintHistory reads a hand-built join → leave →
// rejoin ledger (the shape PR 3's write hooks will later produce live): two
// non-overlapping intervals returned in `joined_at` ascending order — one closed
// `[t0,t1)`, one open `[t2,NULL)` — with `left_at` mapped to a zero Time when
// open and the closed stint's `left_at` preserved.
func TestGetMembershipIntervals_TwoStintHistory(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	ctx := context.Background()
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	}))

	// Insert the open (later) stint first to prove the ORDER BY joined_at ASC.
	withDB(t, path, func(db *sql.DB) {
		_, err := db.Exec(
			`INSERT INTO membership_intervals (channel_id, participant_id, joined_at, left_at)
			   VALUES ('group:planning', 'alice', '2026-01-20T00:00:00Z', NULL),
			          ('group:planning', 'alice', '2026-01-01T00:00:00Z', '2026-01-10T00:00:00Z')`)
		require.NoError(t, err)
	})

	got, err := store.GetMembershipIntervals(ctx, "group:planning", "alice")
	require.NoError(t, err)
	require.Len(t, got, 2, "both stints are returned")

	// [0] is the earlier, closed stint [t0,t1).
	assert.True(t, got[0].JoinedAt.Equal(mustRFC3339(t, "2026-01-01T00:00:00Z")), "earliest join first")
	require.False(t, got[0].LeftAt.IsZero(), "stint 1 is closed")
	assert.True(t, got[0].LeftAt.Equal(mustRFC3339(t, "2026-01-10T00:00:00Z")), "closed left_at preserved")

	// [1] is the later, open stint [t2,NULL).
	assert.True(t, got[1].JoinedAt.Equal(mustRFC3339(t, "2026-01-20T00:00:00Z")), "later join second")
	assert.True(t, got[1].LeftAt.IsZero(), "stint 2 is open (NULL left_at ⇒ zero Time)")

	// The result composes with the predicate helper: a gap timestamp is out.
	assert.True(t, InScope(got, mustRFC3339(t, "2026-01-05T00:00:00Z")), "stint-1 timestamp is in scope")
	assert.False(t, InScope(got, mustRFC3339(t, "2026-01-15T00:00:00Z")), "removal-gap timestamp is out of scope")
}
