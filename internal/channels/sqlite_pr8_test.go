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

// This file gathers the tests landed by RFC 0011 PR 8 (the internal-scope
// close PR) to dispatch the deferred PR #231 review NTH items without
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
//
// Note: the two manual Close calls below are followed by a third call from
// `newTestStore`'s `t.Cleanup` registration (sqlite_test.go: `defer-style
// cleanup that closes the store`). Its return is intentionally discarded
// there — pinning ≥ 3-call idempotency for free, beyond the two-call
// minimum this test asserts directly.
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

// TestSQLiteStore_PublishMessage_FKDisambiguation_InvalidThreadID pins
// sub-case 2 of the round-2 FK-disambiguation branch
// (sqlite_messages.go: the `chCount > 0 && msg.ThreadID != ""` arm). When
// the membership probe succeeds, the channel row is still present, and the
// INSERT fires `SQLITE_CONSTRAINT_FOREIGNKEY` because of a non-existent
// `thread_id`, the error must surface as `"channels: invalid thread_id ..."`
// — not as `ErrChannelNotFound` (the channel is present) and not as the
// raw SQL error (the round-1-only path).
//
// The schema declares `thread_id TEXT REFERENCES messages(id) ON DELETE
// CASCADE`, so publishing with a `ThreadID` that does not match any
// existing message id triggers the violation directly — no test seam
// required (this contrasts with sub-case 1, which needs the
// `foreign_keys = OFF` second-handle trick because the cascade would
// otherwise prevent the orphan state).
//
// Sub-case 3 of the round-2 branch (`chCount > 0`, empty `ThreadID` →
// raw error, marked "unexpected" in the production code) is intentionally
// not tested: with `thread_id` the only other FK column on `messages`,
// the only way to fire an FK violation with `ThreadID == ""` and the
// channel still present would be a contrived driver-level seam, which
// would falsely imply the branch is reachable in production. A genuine
// regression in that arm would surface through the raw-error fallback.
//
// Pairs with TestSQLiteStore_PublishMessage_FKDisambiguation_ChannelDeletedConcurrently
// (sub-case 1) to round out the disambiguation coverage at PR 8 — the
// canonical "deferred-NTH disposition" PR.
func TestSQLiteStore_PublishMessage_FKDisambiguation_InvalidThreadID(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob")

	err := store.PublishMessage(ctx, ChannelMessage{
		ID:        uuid.NewString(),
		ChannelID: id,
		SenderID:  "alice",
		Content:   "reply",
		ThreadID:  "msg-does-not-exist",
	})
	require.Error(t, err, "publish with non-existent thread_id must fail")
	assert.Contains(t, err.Error(), "invalid thread_id",
		"FK violation on thread_id with extant channel must surface as "+
			"'invalid thread_id', not as ErrChannelNotFound or a raw SQL error")
	assert.Contains(t, err.Error(), "msg-does-not-exist",
		"the rejected thread_id value must appear in the error message so "+
			"the caller can identify which value was unknown")
	assert.NotErrorIs(t, err, ErrChannelNotFound,
		"sub-case 2 must not surface as the 'deleted concurrently' branch — "+
			"the channel row is still present")
}
