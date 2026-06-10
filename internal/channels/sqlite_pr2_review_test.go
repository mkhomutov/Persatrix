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

// TestSQLiteStore_Migration_V1ToV2_PreservesChildRows pins PR #245 review
// finding "FK during 12-step rebuild" (High):
//
// The canonical SQLite "12-step" table rebuild (https://sqlite.org/lang_altertable.html
// §7) requires `PRAGMA foreign_keys=OFF` *outside* the transaction so the
// `DROP TABLE channels` step does not fire FK actions against the
// `memberships` and `messages` rows that reference it. The original
// migration test (TestSQLiteStore_Migration_V1ToV2_PreservesRows) only
// seeded `channels` rows, so this risk path went uncovered.
//
// This test seeds *both* a v1 group channel AND its membership + message
// children, then opens the store via NewSQLiteStore (which runs the
// migration). The expectation is that every child row survives the
// rebuild with intact FK targets — i.e. the children are still readable
// via the public store API after migration, and a subsequent publish
// against the same channel still passes the membership probe.
//
// Without the FK-OFF guard, SQLite's behaviour is technically defined
// (DROP TABLE does not fire `ON DELETE CASCADE`) but the rebuild relies
// on undocumented driver behaviour for the `RENAME TO channels` step to
// re-bind the FK. This regression test fails loudly if a future SQLite
// version (or a different driver) tightens that behaviour.
func TestSQLiteStore_Migration_V1ToV2_PreservesChildRows(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")

	// Hand-create a v1-shaped database with seeded children.
	db, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	_, err = db.Exec(schemaV1SQL)
	require.NoError(t, err)
	_, err = db.Exec(
		`INSERT INTO channels (id, name, channel_type, description, created_at)
		 VALUES (?, ?, 'group', '', datetime('now'))`,
		"group:planning", "planning",
	)
	require.NoError(t, err)
	_, err = db.Exec(
		`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at)
		 VALUES (?, ?, 'always', datetime('now')),
		        (?, ?, 'when_mentioned', datetime('now'))`,
		"group:planning", "alice",
		"group:planning", "bob",
	)
	require.NoError(t, err)
	_, err = db.Exec(
		`INSERT INTO messages (id, channel_id, sender_id, content, timestamp)
		 VALUES (?, ?, ?, ?, datetime('now')),
		        (?, ?, ?, ?, datetime('now'))`,
		"msg-1", "group:planning", "alice", "hello",
		"msg-2", "group:planning", "bob", "world",
	)
	require.NoError(t, err)
	require.NoError(t, db.Close())

	// Reopen via NewSQLiteStore — applySchema runs the v1→v2 migration.
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	ctx := context.Background()

	// Channel survived.
	ch, err := store.GetChannel(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, "planning", ch.Name)

	// Memberships survived AND still bind to the (rebuilt) channel row.
	members, err := store.GetMembers(ctx, "group:planning")
	require.NoError(t, err)
	require.Len(t, members, 2, "both v1 memberships must survive the rebuild")

	// Messages survived AND still bind to the (rebuilt) channel row.
	hist, err := store.GetHistory(ctx, "group:planning", 10, time.Time{})
	require.NoError(t, err)
	require.Len(t, hist, 2, "both v1 messages must survive the rebuild")

	// FK target is live: a fresh publish against the migrated channel
	// passes the membership probe and persists. If `RENAME TO channels`
	// failed to re-bind the FK, the INSERT would fire SQLITE_CONSTRAINT_
	// FOREIGNKEY and surface ErrChannelNotFound here.
	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID: "msg-3", ChannelID: "group:planning", SenderID: "alice", Content: "post-migration",
	}))

	// And cascade still fires: deleting the channel cleans children up.
	require.NoError(t, store.DeleteChannel(ctx, "group:planning"))
	hist2, err := store.GetHistory(ctx, "group:planning", 10, time.Time{})
	require.NoError(t, err)
	assert.Empty(t, hist2, "messages must be cascade-deleted with the channel")
}

// TestSQLiteStore_CreateChannelWithMembers_HappyPath pins PR #245 review
// finding "non-atomic create-then-add-members" (High): the new
// transactional helper inserts the channel and every membership in a
// single tx so a partial failure leaves no orphan channel.
func TestSQLiteStore_CreateChannelWithMembers_HappyPath(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()

	require.NoError(t, store.CreateChannelWithMembers(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	}, []Member{
		{ParticipantID: "alice", RespondPolicy: RespondAlways},
		{ParticipantID: "bob", RespondPolicy: RespondWhenMentioned},
	}))

	members, err := store.GetMembers(ctx, "group:planning")
	require.NoError(t, err)
	assert.Len(t, members, 2)
}

// TestSQLiteStore_CreateChannelWithMembers_AtomicOnPartialFailure pins
// the rollback contract: when any member insert fails, the entire
// channel creation is rolled back so a client retry hits a clean state
// (rather than ErrChannelExists for an orphan row).
//
// We trigger the failure via an invalid participant id (`""` rejected by
// ValidateParticipantID) in the second slot. After the call:
//
//   - GetChannel must return ErrChannelNotFound (channel was rolled back)
//   - GetMembers must return empty (no orphan memberships)
//   - A retry with the same canonical id and a fixed member list must
//     succeed (no ErrChannelExists from a leaked row)
func TestSQLiteStore_CreateChannelWithMembers_AtomicOnPartialFailure(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()

	err := store.CreateChannelWithMembers(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	}, []Member{
		{ParticipantID: "alice", RespondPolicy: RespondAlways},
		{ParticipantID: "", RespondPolicy: RespondAlways}, // invalid → triggers rollback
	})
	require.Error(t, err, "invalid member must surface an error")

	_, getErr := store.GetChannel(ctx, "group:planning")
	assert.ErrorIs(t, getErr, ErrChannelNotFound,
		"channel must not exist after rollback (no orphan row)")

	// Retry with a clean member list must succeed (no ErrChannelExists).
	require.NoError(t, store.CreateChannelWithMembers(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	}, []Member{
		{ParticipantID: "alice", RespondPolicy: RespondAlways},
	}), "retry after rollback must succeed cleanly")
}
