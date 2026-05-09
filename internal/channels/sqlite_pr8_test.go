package channels

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	_ "modernc.org/sqlite"
)

// This file gathers the three tests landed by RFC 0011 PR 8 (the internal-
// scope close PR) to dispatch the deferred PR #231 review NTH items without
// pushing `sqlite_test.go` over the 500-line file-size cap. Co-locating them
// here also makes the "tests added when RFC 0011 closed" set easy to find
// for a future reviewer auditing the partial-implementation handover into
// v0.5.0 (external bridges).

// TestSQLiteStore_Close_Idempotent pins the genuine idempotency contract that
// the previous version of this test under-asserted: `database/sql.DB.Close`
// returns nil on every invocation after the first (the pool tracks the
// closed flag), and `sqliteStore.Close` is a thin pass-through. A caller's
// `defer store.Close()` after an explicit shutdown must therefore stay
// silent, not just "not panic". Closes PR #231 review NTH "rename or
// tighten assertion".
func TestSQLiteStore_Close_Idempotent(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	require.NoError(t, store.Close(), "first Close must be clean")
	require.NoError(t, store.Close(),
		"second Close must also return nil — database/sql.DB is documented "+
			"as idempotent and `sqliteStore.Close` adds no error path")
}

// TestSQLiteStore_MaxOpenConnsPinnedToOne pins the v0.3.0 connection-pool
// invariant from `NewSQLiteStore`'s rationale comment: every channel-store
// transaction depends on serial writer access (cap-check TOCTOU windows in
// CreateChannel, dmMu serialisation in GetOrCreateDM, the membership-then-
// INSERT shape in PublishMessage). A future refactor that lifts the cap
// without first relaxing those transactions would silently widen the race
// surface — this test red-flags the cap drift before the next contributor
// has to re-derive the rationale from the comment alone. Closes PR #231
// review NTH "db.Stats().MaxOpenConnections == 1 invariant test".
func TestSQLiteStore_MaxOpenConnsPinnedToOne(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	concrete, ok := store.(*sqliteStore)
	require.True(t, ok, "newTestStore must return the concrete *sqliteStore")
	assert.Equal(t, 1, concrete.db.Stats().MaxOpenConnections,
		"MaxOpenConns must stay pinned to 1 until the txn shapes named in "+
			"NewSQLiteStore's rationale comment are relaxed")
}

// TestSQLiteStore_PublishMessage_FKDisambiguation_ChannelDeletedConcurrently
// pins the round-2 FK-disambiguation branch in `PublishMessage`
// (sqlite_messages.go: the `chCount == 0` arm of the `isForeignKeyViolation`
// re-probe). When the membership probe succeeds but the INSERT fires a FK
// violation because the channel row is no longer present, the error must
// surface as `ErrChannelNotFound: <id> (deleted concurrently)` — not as
// "invalid thread_id" (the round-1-only path) or as a raw SQL error.
//
// The test forces the otherwise-impossible "membership row exists, channel
// row does not" state by opening a second `sql.DB` to the same file with
// `foreign_keys = OFF` and dropping the channel row out from under the
// memberships. Inside `NewSQLiteStore` the cascade prevents this state; the
// test seam exercises the defence-in-depth that round 2 added for the case
// where a future refactor (e.g. lifting `MaxOpenConns` past 1) admits a
// concurrent DELETE during a publish transaction. Closes PR #231 review
// NTH "FK-disambiguation 'channel deleted concurrently' test (needs a
// test-only mutation seam)".
func TestSQLiteStore_PublishMessage_FKDisambiguation_ChannelDeletedConcurrently(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob")

	// Open a second handle with foreign_keys=OFF so we can delete the
	// channel row without firing the cascade that would normally clear the
	// memberships table. The orphaned membership row is exactly the state
	// the round-2 disambiguation branch was written to recognise.
	rawDB, err := sql.Open("sqlite", path+"?_pragma=foreign_keys(0)&_pragma=journal_mode(WAL)")
	require.NoError(t, err)
	rawDB.SetMaxOpenConns(1)
	t.Cleanup(func() { _ = rawDB.Close() })
	_, err = rawDB.ExecContext(ctx, `DELETE FROM channels WHERE id = ?`, id)
	require.NoError(t, err, "raw delete must succeed with FK off")

	// Sanity-check the seam: the membership row is still there, the
	// channel row is not. Without this assertion a future SQLite upgrade
	// that changed cascade semantics under foreign_keys=OFF could
	// silently invalidate the test premise.
	var memberCount, channelCount int
	require.NoError(t, rawDB.QueryRowContext(ctx,
		`SELECT COUNT(1) FROM memberships WHERE channel_id = ?`, id).Scan(&memberCount))
	require.NoError(t, rawDB.QueryRowContext(ctx,
		`SELECT COUNT(1) FROM channels WHERE id = ?`, id).Scan(&channelCount))
	require.Equal(t, 2, memberCount, "test seam must leave memberships orphaned")
	require.Equal(t, 0, channelCount, "test seam must remove the channel row")

	err = store.PublishMessage(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "ping",
	})
	require.Error(t, err, "publish against an orphaned membership must fail")
	assert.ErrorIs(t, err, ErrChannelNotFound,
		"FK violation with absent channel row must surface as ErrChannelNotFound")
	assert.Contains(t, err.Error(), "deleted concurrently",
		"the round-2 branch must mark the cause as a concurrent deletion, "+
			"not as 'invalid thread_id' (round-1-only path)")
}
