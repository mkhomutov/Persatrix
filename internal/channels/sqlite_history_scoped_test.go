// RFC 0036 PR 5 (Phase 3) — the membership-scoped, epoch-filtered history query
// ([sqliteStore.GetHistoryScoped]) that backs the conversation-window/catch-up
// `?as_participant=` filter. It is the §G sibling of PR 2's §C recall query: the
// SAME [membershipEpochScope] fragment (membership `EXISTS` + `epoch_id`
// equality), so the live persona prompt obeys the same access rule recall does —
// a re-added persona's window excludes the removal gap, just as recall does.
//
// These tests pin the store-level contract with no endpoint or persona in the
// loop, reusing sqlite_search_test.go's fixtures (`seedInterval` / `seedMsg` /
// `withDB` / `mins` / `idSlice`, same package):
//
//   - Scope: against the join → leave → rejoin fixture, the scoped window returns
//     both stints' messages and excludes the pre-join prefix and the removal gap
//     — and agrees with the Go [InScope] predicate on every message.
//   - Newest-first ordering + limit, matching the unscoped [GetHistory] contract.
//   - `before` cursor composes with the scope (pagination still membership-bound).
//   - Epoch (§OQ-6 lock): a non-"live" epoch row is excluded — the window filters
//     the live world the persisted transcript carries, via the identical fragment.
//   - A current single-stint member's scoped window equals the tail of the
//     unscoped window (the no-op-for-current-member property §G promises).
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

// TestGetHistoryScoped_JoinLeaveRejoin pins the access-control core of the live
// window: against the two-stint fixture, the scoped history returns messages
// inside either stint and excludes the pre-join prefix and the removal gap. The
// half-open `[joined_at, left_at)` boundary is exercised at the exact join /
// leave / rejoin instants, and the returned set is asserted to equal the Go
// [InScope] verdict for every message — so the §G SQL `EXISTS` encoding and the
// Go predicate cannot drift (the same no-drift guard PR 2 holds for §C).
func TestGetHistoryScoped_JoinLeaveRejoin(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: ch, Name: "planning", Type: ChannelTypeGroup}))

	// alice: closed stint [60m,120m), open stint [240m, ∞). The half-open ledger.
	aliceIvs := []MembershipInterval{
		{ChannelID: ch, ParticipantID: "alice", JoinedAt: mins(60), LeftAt: mins(120)},
		{ChannelID: ch, ParticipantID: "alice", JoinedAt: mins(240)}, // open
	}

	seeds := []msgSeed{
		{id: "m-before", channelID: ch, sender: "bob", content: "before join", ts: mins(30)},
		{id: "m-atjoin1", channelID: ch, sender: "bob", content: "at join", ts: mins(60)},     // inclusive lower → in
		{id: "m-stint1", channelID: ch, sender: "bob", content: "stint one", ts: mins(90)},    // in
		{id: "m-atleave1", channelID: ch, sender: "bob", content: "at leave", ts: mins(120)},  // exclusive upper → out
		{id: "m-gap", channelID: ch, sender: "bob", content: "in gap", ts: mins(180)},         // out
		{id: "m-atrejoin", channelID: ch, sender: "bob", content: "at rejoin", ts: mins(240)}, // in
		{id: "m-stint2", channelID: ch, sender: "bob", content: "stint two", ts: mins(300)},   // in
	}

	withDB(t, path, func(db *sql.DB) {
		seedInterval(t, db, ch, "alice", mins(60), mins(120)) // stint 1 closed
		seedInterval(t, db, ch, "alice", mins(240), nil)      // stint 2 open
		for _, m := range seeds {
			seedMsg(t, db, m)
		}
	})

	got, err := store.GetHistoryScoped(ctx, ch, "alice", 50, time.Time{})
	require.NoError(t, err)
	recalled := idSet(got)

	// The scoped window equals exactly the in-scope set...
	assert.ElementsMatch(t,
		[]string{"m-atjoin1", "m-stint1", "m-atrejoin", "m-stint2"},
		idSlice(got),
		"both stints visible; pre-join prefix and removal gap excluded")

	// ...and equals the Go InScope verdict on every message (no-drift guard).
	for _, m := range seeds {
		want := InScope(aliceIvs, m.ts)
		assert.Equalf(t, want, recalled[m.id],
			"message %s at +%s: InScope(Go)=%v, scoped(SQL)=%v — encodings must agree",
			m.id, m.ts.Sub(recallFixtureBase), want, recalled[m.id])
	}
}

// TestGetHistoryScoped_NewestFirstAndLimit pins the ordering + limit contract:
// the scoped history is newest-first (matching [GetHistory]) and honours the
// row cap, returning the most recent `limit` in-scope messages.
func TestGetHistoryScoped_NewestFirstAndLimit(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: ch, Name: "planning", Type: ChannelTypeGroup}))

	withDB(t, path, func(db *sql.DB) {
		seedInterval(t, db, ch, "alice", mins(0), nil) // open from the start
		seedMsg(t, db, msgSeed{id: "m1", channelID: ch, sender: "bob", content: "one", ts: mins(10)})
		seedMsg(t, db, msgSeed{id: "m2", channelID: ch, sender: "bob", content: "two", ts: mins(20)})
		seedMsg(t, db, msgSeed{id: "m3", channelID: ch, sender: "bob", content: "three", ts: mins(30)})
	})

	got, err := store.GetHistoryScoped(ctx, ch, "alice", 2, time.Time{})
	require.NoError(t, err)
	assert.Equal(t, []string{"m3", "m2"}, idSlice(got),
		"newest-first, capped at the requested limit")
}

// TestGetHistoryScoped_BeforeCursorComposesWithScope pins that the `before`
// pagination cursor (exclusive upper bound, matching [GetHistory]) composes with
// the membership scope — paging back never reaches a gap or pre-join message.
func TestGetHistoryScoped_BeforeCursorComposesWithScope(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: ch, Name: "planning", Type: ChannelTypeGroup}))

	withDB(t, path, func(db *sql.DB) {
		// alice present only in [100m, 200m); the gap rows on either side must stay
		// invisible however the cursor is positioned.
		seedInterval(t, db, ch, "alice", mins(100), mins(200))
		seedMsg(t, db, msgSeed{id: "m-pre", channelID: ch, sender: "bob", content: "pre", ts: mins(50)})
		seedMsg(t, db, msgSeed{id: "m-in1", channelID: ch, sender: "bob", content: "in one", ts: mins(110)})
		seedMsg(t, db, msgSeed{id: "m-in2", channelID: ch, sender: "bob", content: "in two", ts: mins(150)})
		seedMsg(t, db, msgSeed{id: "m-post", channelID: ch, sender: "bob", content: "post", ts: mins(250)})
	})

	// A cursor after the stint must not surface the post-leave row.
	got, err := store.GetHistoryScoped(ctx, ch, "alice", 50, mins(300))
	require.NoError(t, err)
	assert.Equal(t, []string{"m-in2", "m-in1"}, idSlice(got),
		"before-cursor window stays inside the stint; pre/post rows excluded")

	// A cursor inside the stint trims to the older in-scope row only.
	got, err = store.GetHistoryScoped(ctx, ch, "alice", 50, mins(150))
	require.NoError(t, err)
	assert.Equal(t, []string{"m-in1"}, idSlice(got),
		"before is an exclusive upper bound, composed with scope")
}

// TestGetHistoryScoped_EpochHardFilter pins the §OQ-6 lock for the window: the
// scoped history filters the persisted "live" epoch (the world every real
// message carries), via the identical [membershipEpochScope] fragment recall
// uses. A synthetically-seeded non-"live" row inside the membership window is
// never returned — so the §C predicate and the §G clause filter epoch together.
func TestGetHistoryScoped_EpochHardFilter(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: ch, Name: "planning", Type: ChannelTypeGroup}))

	withDB(t, path, func(db *sql.DB) {
		seedInterval(t, db, ch, "alice", mins(0), nil) // open, so only epoch trims
		seedMsg(t, db, msgSeed{id: "m-live", channelID: ch, sender: "bob", content: "live world", ts: mins(10), epoch: "live"})
		seedMsg(t, db, msgSeed{id: "m-ci", channelID: ch, sender: "bob", content: "other world", ts: mins(20), epoch: "ci-run-7"})
	})

	got, err := store.GetHistoryScoped(ctx, ch, "alice", 50, time.Time{})
	require.NoError(t, err)
	assert.Equal(t, []string{"m-live"}, idSlice(got),
		"the scoped window is strict-equality on the live epoch; a cross-epoch row is excluded")
}

// TestGetHistoryScoped_CurrentMemberMatchesUnscopedTail pins the narrow case in
// which §G is a no-op: a member present from before the first retained message
// (a from-the-start single stint) has no pre-join prefix to trim, so its scoped
// window equals the unscoped [GetHistory] window. This is the ONLY no-op case —
// see TestGetHistoryScoped_CurrentMemberJoinedMidStream_TrimsPreJoin for the
// common mid-join case, where a current member's window IS trimmed.
func TestGetHistoryScoped_CurrentMemberMatchesUnscopedTail(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: ch, Name: "planning", Type: ChannelTypeGroup}))

	withDB(t, path, func(db *sql.DB) {
		seedInterval(t, db, ch, "alice", mins(0), nil) // present for the whole transcript
		for i := 0; i < 5; i++ {
			seedMsg(t, db, msgSeed{
				id: "m-" + itoa(int64(i)), channelID: ch, sender: "bob",
				content: "msg", ts: mins(i + 1),
			})
		}
	})

	unscoped, err := store.GetHistory(ctx, ch, 50, time.Time{})
	require.NoError(t, err)
	scoped, err := store.GetHistoryScoped(ctx, ch, "alice", 50, time.Time{})
	require.NoError(t, err)
	assert.Equal(t, idSlice(unscoped), idSlice(scoped),
		"a current from-the-start member's scoped window equals the unscoped window")
}

// TestGetHistoryScoped_CurrentMemberJoinedMidStream_TrimsPreJoin is the
// necessary counter-case to the no-op test above: the "no-op for current
// member" property is NARROW — it holds only for a member present from before
// the first retained message. A CURRENT member (open stint) that joined
// mid-conversation still has its pre-join prefix trimmed, so the scoped window
// is strictly smaller than the unscoped one. This pins that §G is a real filter
// for the common mid-join case, not the cosmetic no-op the headline suggests.
func TestGetHistoryScoped_CurrentMemberJoinedMidStream_TrimsPreJoin(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: ch, Name: "planning", Type: ChannelTypeGroup}))

	withDB(t, path, func(db *sql.DB) {
		// alice is a CURRENT member (open stint) but joined at +30m — after the
		// channel already had traffic. The half-open lower bound is inclusive.
		seedInterval(t, db, ch, "alice", mins(30), nil)
		seedMsg(t, db, msgSeed{id: "m-pre1", channelID: ch, sender: "bob", content: "pre one", ts: mins(10)})
		seedMsg(t, db, msgSeed{id: "m-pre2", channelID: ch, sender: "bob", content: "pre two", ts: mins(20)})
		seedMsg(t, db, msgSeed{id: "m-in1", channelID: ch, sender: "bob", content: "in one", ts: mins(40)})
		seedMsg(t, db, msgSeed{id: "m-in2", channelID: ch, sender: "bob", content: "in two", ts: mins(50)})
	})

	unscoped, err := store.GetHistory(ctx, ch, 50, time.Time{})
	require.NoError(t, err)
	scoped, err := store.GetHistoryScoped(ctx, ch, "alice", 50, time.Time{})
	require.NoError(t, err)

	assert.Equal(t, []string{"m-in2", "m-in1"}, idSlice(scoped),
		"only post-join messages are in scope for a mid-join member")
	assert.NotEqual(t, idSlice(unscoped), idSlice(scoped),
		"a mid-join current member's scoped window is NOT a no-op — the pre-join prefix is trimmed")
}

// TestGetHistoryScoped_EmptyParticipantID_Errors pins the input-validation half
// of the "one access rule" property §G shares with §C recall: an empty
// participant id is rejected outright — exactly as [sqliteStore.RecallMessages]
// rejects it — rather than silently run as a query for the empty-string
// participant. The latter would return an empty set that reads as "this member
// has no in-scope history" when the real fault is "no scope subject was
// supplied", and would mask a caller bug behind a plausible-looking empty
// window. The handler treats a blank `?as_participant=` as absent (routing to
// the unscoped query) before reaching here; this guard is the store-level
// belt-and-suspenders that backs that contract.
func TestGetHistoryScoped_EmptyParticipantID_Errors(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: ch, Name: "planning", Type: ChannelTypeGroup}))

	_, err = store.GetHistoryScoped(ctx, ch, "", 50, time.Time{})
	require.Error(t, err,
		"an empty participant id must be rejected, not run as an empty-result query")
}
