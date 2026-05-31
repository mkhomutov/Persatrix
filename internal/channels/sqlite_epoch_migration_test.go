// ISSUE-0085 PR 2 — epoch_id columns migration (channelStoreSchemaVersion
// v5 → v6).
//
// The migration is forward-only and idempotent. It adds
// `epoch_id TEXT NOT NULL DEFAULT 'live'` to `channels` and `messages` —
// the run/test-isolation sibling of the `session_id` (v3) operator
// namespace — plus a per-table `idx_<table>_epoch` lookup index. Where
// `session_id` is the room-continuity axis (with a `legacy` carve-out),
// `epoch_id` is the strict-equality isolation axis (default `live`, no
// carve-out) — the Go-store half of the ISSUE-0085 axis whose persona-memory
// half ships as migration v12. PR 2 ships no recall changes and no writer
// sets a non-default epoch — these tests assert the storage contract only;
// the column default backfills every existing and new row to `live`.
package channels

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"

	_ "modernc.org/sqlite"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestSQLiteStore_SchemaV6_FreshDB_ChannelsHasEpochIDColumn asserts the
// channels table grows an `epoch_id` column defaulted to `live`. A row
// inserted without an explicit epoch (no writer sets one in PR 2) picks up
// the column default.
func TestSQLiteStore_SchemaV6_FreshDB_ChannelsHasEpochIDColumn(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		assert.Contains(t, tableColumns(t, db, "channels"), "epoch_id",
			"channels.epoch_id missing")
	})

	ctx := context.Background()
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	}))

	withDB(t, path, func(db *sql.DB) {
		var eid string
		require.NoError(t, db.QueryRow(
			`SELECT epoch_id FROM channels WHERE id = ?`, "group:planning").Scan(&eid))
		assert.Equal(t, DefaultEpochID, eid,
			"a channel written without an explicit epoch defaults to live")
	})
}

// TestSQLiteStore_SchemaV6_FreshDB_MessagesHasEpochIDColumn mirrors the
// channels assertion for the messages table.
func TestSQLiteStore_SchemaV6_FreshDB_MessagesHasEpochIDColumn(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		assert.Contains(t, tableColumns(t, db, "messages"), "epoch_id",
			"messages.epoch_id missing")
	})

	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice")
	mid := uuid.NewString()
	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID: mid, ChannelID: id, SenderID: "alice", Content: "hi",
	}))

	withDB(t, path, func(db *sql.DB) {
		var eid string
		require.NoError(t, db.QueryRow(
			`SELECT epoch_id FROM messages WHERE id = ?`, mid).Scan(&eid))
		assert.Equal(t, DefaultEpochID, eid,
			"a message published without an explicit epoch defaults to live")
	})
}

// TestSQLiteStore_SchemaV6_FreshDB_HasIndexes asserts the v6 per-table epoch
// lookup indexes are present after migration. Mirrors the persona-memory v12
// `idx_<tier>_epoch` shape — a uniform standalone index for a future
// epoch-scoped maintenance op, not the index recall seeks (epoch is a
// residual equality filter, like principal).
func TestSQLiteStore_SchemaV6_FreshDB_HasIndexes(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		assert.Contains(t, tableIndexes(t, db, "channels"), "idx_channels_epoch",
			"per-epoch channel lookup index missing")
		assert.Contains(t, tableIndexes(t, db, "messages"), "idx_messages_epoch",
			"per-epoch message lookup index missing")
		// The v3 covering index is untouched by v6 — epoch is a residual
		// filter, so the migration adds a standalone index rather than
		// rebuilding the (channel_id, session_id, timestamp DESC) shape.
		assert.Contains(t, tableIndexes(t, db, "messages"), "idx_messages_channel_session",
			"v3 covering index must survive the v6 additive migration")
	})
}

// TestSQLiteStore_SchemaV6_Migration_Idempotent asserts that opening the
// store twice over the same file leaves user_version at the latest schema
// version and does not re-run the migration (no duplicate-index error, no
// data drift). The literal-version pin lives here (the newest migration's
// test), matching how the v5 test held `channelStoreSchemaVersion == 5`.
func TestSQLiteStore_SchemaV6_Migration_Idempotent(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")

	store1, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	require.NoError(t, store1.Close())

	store2, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store2.Close() })

	withDB(t, path, func(db *sql.DB) {
		var version int
		require.NoError(t, db.QueryRow(`PRAGMA user_version`).Scan(&version))
		assert.Equal(t, channelStoreSchemaVersion, version,
			"user_version stamped to the latest schema version; reopen is a no-op")
		assert.Equal(t, 6, channelStoreSchemaVersion, "ISSUE-0085 bumps the channel store to v6")
	})
}

// TestSQLiteStore_Migration_V5ToV6_BackfillsLive pins the data migration: an
// existing v5 database whose `channels` / `messages` rows pre-date the epoch
// axis are backfilled to `epoch_id = 'live'` by the column default — no
// data loss, single-world deployments unchanged.
func TestSQLiteStore_Migration_V5ToV6_BackfillsLive(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")

	// Hand-build a v5-shaped database: baseline + v1→v2 … v4→v5, then stamp
	// user_version=5 so the v6 binary's loop runs only the v5→v6 step.
	db, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	_, err = db.Exec(schemaV1SQL)
	require.NoError(t, err)
	require.NoError(t, migrateV1ToV2(db))
	require.NoError(t, migrateV2ToV3(db))
	require.NoError(t, migrateV3ToV4(db))
	require.NoError(t, migrateV4ToV5(db))
	_, err = db.Exec(`PRAGMA user_version = 5`)
	require.NoError(t, err)

	// A pre-v6 group channel + a message in it (both at session_id='legacy',
	// no epoch_id column yet).
	_, err = db.Exec(
		`INSERT INTO channels (id, name, channel_type, description, created_at, session_id)
		   VALUES ('group:planning', 'planning', 'group', '', '2026-01-01T00:00:00Z', 'legacy')`)
	require.NoError(t, err)
	_, err = db.Exec(
		`INSERT INTO messages (id, channel_id, sender_id, content, timestamp, mentions, metadata, session_id)
		   VALUES ('m1', 'group:planning', 'alice', 'hi', '2026-01-01T00:00:01Z', '[]', '{}', 'legacy')`)
	require.NoError(t, err)
	require.NoError(t, db.Close())

	// Reopen — the v5→v6 migration runs and backfills the default epoch.
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		var version int
		require.NoError(t, db.QueryRow(`PRAGMA user_version`).Scan(&version))
		assert.Equal(t, 6, version, "v5→v6 ran")

		var chEpoch, msgEpoch string
		require.NoError(t, db.QueryRow(
			`SELECT epoch_id FROM channels WHERE id = 'group:planning'`).Scan(&chEpoch))
		require.NoError(t, db.QueryRow(
			`SELECT epoch_id FROM messages WHERE id = 'm1'`).Scan(&msgEpoch))
		assert.Equal(t, DefaultEpochID, chEpoch, "pre-v6 channel row backfilled to live")
		assert.Equal(t, DefaultEpochID, msgEpoch, "pre-v6 message row backfilled to live")
	})
}
