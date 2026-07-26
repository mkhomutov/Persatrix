// RFC 0036 PR 2 — the FTS5-UNAVAILABLE `LIKE` fallback contract for
// [sqliteStore.RecallMessages]. Split from sqlite_search_test.go (which holds
// the FTS-present path and the shared fixtures) to keep each file under the
// repo's 500-line cap — the same split the production code uses (recall.go vs
// sqlite_search.go). Fixtures (mins/seedMsg/seedInterval/idSlice/withDB) live in
// the sibling files and are shared across the package's test binary.
//
// Both tests drop the `messages_fts` index from a populated db and reopen, so a
// fresh store probes the table absent and routes recall through the LIKE branch.
// They pin the two halves of the fallback's contract:
//
//   - Scope is byte-identical to the FTS path (an out-of-scope row is
//     unreachable on both), while the per-token TEXT match diverges — `budget`
//     hits the `budgets` substring under LIKE but not the `budget` token under
//     FTS5.
//   - A multi-term query is an order-independent per-token AND (FTS-like), not a
//     contiguous-substring match of the raw phrase.
package channels

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// dropMessagesFTS removes the FTS5 index and its sync triggers from the db at
// `path`, so a store opened against it afterwards probes `messages_fts` absent
// and takes the LIKE fallback. Shared by the fallback tests below.
func dropMessagesFTS(t *testing.T, path string) {
	t.Helper()
	withDB(t, path, func(db *sql.DB) {
		for _, ddl := range []string{
			`DROP TRIGGER IF EXISTS messages_ai`, `DROP TRIGGER IF EXISTS messages_ad`,
			`DROP TRIGGER IF EXISTS messages_au`, `DROP TABLE IF EXISTS messages_fts`,
		} {
			_, err := db.Exec(ddl)
			require.NoError(t, err)
		}
	})
}

// TestRecallMessages_LikeFallback_SameScopeDifferentTextMatch pins the true
// FTS5-unavailable contract: the LIKE fallback keeps the byte-identical scope (an
// out-of-scope row is unreachable on BOTH paths), but its substring text match is
// NOT FTS5's token row set — `budget` excludes the `budgets` token under FTS5 yet
// includes it under LIKE. (The prior revision asserted "same row set", passing
// only on a fixture whose tokens were also exact substrings.)
func TestRecallMessages_LikeFallback_SameScopeDifferentTextMatch(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	ctx := context.Background()
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: "group:planning", Name: "planning", Type: ChannelTypeGroup}))
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: "group:secret", Name: "secret", Type: ChannelTypeGroup}))

	// m-exact: token "budget" (FTS+LIKE). m-plural: token "budgets" (LIKE substring
	// only). m-out: token "budget" but in a channel alice was never in (out of scope).
	withDB(t, path, func(db *sql.DB) {
		seedInterval(t, db, "group:planning", "alice", mins(0), nil)
		seedMsg(t, db, msgSeed{id: "m-exact", channelID: "group:planning", sender: "bob", content: "the budget review", ts: mins(10)})
		seedMsg(t, db, msgSeed{id: "m-plural", channelID: "group:planning", sender: "bob", content: "two budgets approved", ts: mins(20)})
		seedMsg(t, db, msgSeed{id: "m-out", channelID: "group:secret", sender: "carol", content: "the budget review", ts: mins(10)})
	})

	ftsGot, err := store.RecallMessages(ctx, RecallParams{ParticipantID: "alice", ActingClassification: ClassificationInternal, Query: "budget"})
	require.NoError(t, err)
	require.NoError(t, store.Close())

	dropMessagesFTS(t, path) // reopen must take the LIKE path
	store2, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store2.Close() })

	likeGot, err := store2.RecallMessages(ctx, RecallParams{ParticipantID: "alice", ActingClassification: ClassificationInternal, Query: "budget"})
	require.NoError(t, err, "recall still works with FTS5 unavailable (LIKE fallback)")

	// Text match diverges (FTS5 token vs LIKE substring, which adds m-plural)...
	assert.Equal(t, []string{"m-exact"}, idSlice(ftsGot), "FTS5 matches the whole token `budget` only")
	assert.ElementsMatch(t, []string{"m-exact", "m-plural"}, idSlice(likeGot), "LIKE adds the `budgets` substring — paths diverge")
	// ...but scope is byte-identical: the out-of-scope row is excluded on BOTH paths.
	assert.NotContains(t, idSlice(ftsGot), "m-out", "scope excludes the out-of-channel row (FTS path)")
	assert.NotContains(t, idSlice(likeGot), "m-out", "scope excludes the out-of-channel row (LIKE path)")
}

// TestRecallMessages_LikeFallback_MultiTermTokenAND pins the LIKE fallback's
// multi-term contract: it ANDs one `%token%` substring per sanitized token, so a
// query whose terms appear NON-adjacently still matches — mirroring FTS5's
// order-independent token AND, not a contiguous-substring match of the raw
// phrase. The earlier revision LIKE-matched the whole query as one blob
// (`%budget report%`), so it demanded the terms be adjacent and missed
// `budget … report`; this is the regression guard for that fix. Scope and the
// single-term superstring divergence (covered above) are unchanged — only the
// multi-term, non-FTS-like adjacency requirement is removed.
func TestRecallMessages_LikeFallback_MultiTermTokenAND(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	ctx := context.Background()
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: ch, Name: "planning", Type: ChannelTypeGroup}))

	withDB(t, path, func(db *sql.DB) {
		seedInterval(t, db, ch, "alice", mins(0), nil)
		// Both terms present but NOT adjacent — a contiguous `%budget report%`
		// substring of the raw query would miss this; a per-token AND matches it.
		seedMsg(t, db, msgSeed{id: "m-split", channelID: ch, sender: "bob", content: "the budget for the quarterly report", ts: mins(10)})
		// Only one of the two terms — must NOT match the two-term AND query.
		seedMsg(t, db, msgSeed{id: "m-one", channelID: ch, sender: "bob", content: "the budget was approved", ts: mins(20)})
		// Terms adjacent — matches under either interpretation (control row).
		seedMsg(t, db, msgSeed{id: "m-adj", channelID: ch, sender: "bob", content: "see the budget report attached", ts: mins(30)})
	})
	require.NoError(t, store.Close())

	dropMessagesFTS(t, path) // reopen must take the LIKE path
	store2, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store2.Close() })

	got, err := store2.RecallMessages(ctx, RecallParams{ParticipantID: "alice", ActingClassification: ClassificationInternal, Query: "budget report"})
	require.NoError(t, err)
	// Token AND: both terms present (any order, non-adjacent) → match; one term → no.
	assert.ElementsMatch(t, []string{"m-split", "m-adj"}, idSlice(got),
		"LIKE fallback ANDs per-token substrings (not a contiguous match of the raw phrase); m-one lacks `report`")
}
