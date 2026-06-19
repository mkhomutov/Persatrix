// RFC 0036 PR 1 — the `messages_fts` FTS5 index migration
// (channelStoreSchemaVersion v9 → v10).
//
// The migration is forward-only and a pure addition: it creates the
// external-content FTS5 virtual table `messages_fts` over `messages.content`,
// the `messages_ai` / `messages_ad` / `messages_au` synchronisation triggers,
// and rebuilds the index from the existing `messages` rows. No existing table,
// row, or index is touched, so every `channels` / `messages` query reads back
// byte-identically post-v10.
//
// PR 1 ships the index dormant — no query reads `messages_fts` yet (RFC 0036
// PR 2 adds the scoped search that joins it). These tests therefore assert the
// storage contract only: the virtual table + triggers exist after migration,
// the backfill makes a pre-seeded message MATCH-findable, the insert/delete
// triggers keep the index in sync (the delete trigger is load-bearing for the
// RFC 0036 §H retention story — a cap-pruned message must leave the index),
// idempotent reopen, and the FTS5-unavailable degradation path.
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

// TestSQLiteStore_SchemaV10_FreshDB_HasMessagesFTS asserts the FTS index and
// its three synchronisation triggers exist after a fresh open.
func TestSQLiteStore_SchemaV10_FreshDB_HasMessagesFTS(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		// The FTS5 virtual table is registered as a `table` in sqlite_master.
		var n int
		require.NoError(t, db.QueryRow(
			`SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='messages_fts'`).Scan(&n))
		assert.Equal(t, 1, n, "messages_fts virtual table missing")

		// All three sync triggers present.
		triggers := tableTriggers(t, db, "messages")
		assert.Contains(t, triggers, "messages_ai", "insert trigger missing")
		assert.Contains(t, triggers, "messages_ad", "delete trigger missing")
		assert.Contains(t, triggers, "messages_au", "update trigger missing")
	})
}

// TestSQLiteStore_SchemaV10_Migration_Idempotent pins the schema version at the
// newest migration. Reopening the same file is a no-op (no duplicate-table /
// duplicate-trigger error, user_version stable at the latest). Per the
// convention the v5..v9 migration-test headers document, the literal-version
// pin lives in the newest migration's idempotent test — here.
func TestSQLiteStore_SchemaV10_Migration_Idempotent(t *testing.T) {
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
		assert.Equal(t, 10, channelStoreSchemaVersion,
			"RFC 0036 PR 1 bumps the channel store to v10")

		// Reopen must not duplicate the triggers (the user_version gate skips
		// the migration on the second boot).
		triggers := tableTriggers(t, db, "messages")
		assert.Len(t, triggers, 3, "exactly three sync triggers after idempotent reopen")
	})
}

// TestSQLiteStore_Migration_V9ToV10_BackfillsExistingMessages pins the
// `('rebuild')` backfill: a message that already exists in a v9 database is
// MATCH-findable in `messages_fts` after the v9→v10 migration runs.
func TestSQLiteStore_Migration_V9ToV10_BackfillsExistingMessages(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")

	// Hand-build a v9-shaped database, seed a channel + message, then close so
	// the next NewSQLiteStore open runs only the v9→v10 step.
	db := buildV9DB(t, path)
	_, err := db.Exec(
		`INSERT INTO channels (id, name, channel_type, description, created_at, session_id, epoch_id)
		   VALUES ('group:planning', 'planning', 'group', '', '2026-01-01T00:00:00Z', 'legacy', 'live')`)
	require.NoError(t, err)
	_, err = db.Exec(
		`INSERT INTO messages (id, channel_id, sender_id, content, timestamp)
		   VALUES ('msg-budget', 'group:planning', 'alice',
		           'we should increase the budget for Q2', '2026-01-01T01:00:00Z')`)
	require.NoError(t, err)
	require.NoError(t, db.Close())

	// Reopen — the v9→v10 migration runs and rebuilds the index from `messages`.
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		var version int
		require.NoError(t, db.QueryRow(`PRAGMA user_version`).Scan(&version))
		assert.GreaterOrEqual(t, version, 10, "the v9→v10 migration ran")

		var gotID string
		err := db.QueryRow(
			`SELECT m.id FROM messages m
			   WHERE m.rowid IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'budget')`).
			Scan(&gotID)
		require.NoError(t, err, "the backfilled message is MATCH-findable")
		assert.Equal(t, "msg-budget", gotID)
	})
}

// TestSQLiteStore_SchemaV10_Triggers_KeepIndexInSync asserts the insert trigger
// indexes a newly published message and the delete trigger removes a pruned one
// — the latter is what keeps recall honest about the retention horizon
// (RFC 0036 §H): a cap-pruned message must disappear from `messages_fts`.
func TestSQLiteStore_SchemaV10_Triggers_KeepIndexInSync(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	require.NoError(t, store.CreateChannel(context.Background(), Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	}))

	withDB(t, path, func(db *sql.DB) {
		// Insert — the messages_ai trigger indexes the new row.
		_, err := db.Exec(
			`INSERT INTO messages (id, channel_id, sender_id, content, timestamp)
			   VALUES ('msg-1', 'group:planning', 'alice', 'quarterly roadmap review', '2026-01-01T01:00:00Z')`)
		require.NoError(t, err)

		var n int
		require.NoError(t, db.QueryRow(
			`SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'roadmap'`).Scan(&n))
		assert.Equal(t, 1, n, "messages_ai trigger indexes the inserted message")

		// Delete — the messages_ad trigger removes it from the index.
		_, err = db.Exec(`DELETE FROM messages WHERE id = 'msg-1'`)
		require.NoError(t, err)

		require.NoError(t, db.QueryRow(
			`SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'roadmap'`).Scan(&n))
		assert.Equal(t, 0, n, "messages_ad trigger removes the deleted message from the index")
	})
}

// TestSQLiteStore_SchemaV10_FTS5Unavailable_SkipsVirtualTable exercises the
// degradation path deterministically (modernc.org/sqlite ships FTS5, so the
// probe is forced via the parameterised helper). The migration still completes
// and stamps v10, but creates no virtual table and no triggers — RFC 0036 PR 2
// detects the absent `messages_fts` and takes its LIKE fallback.
func TestSQLiteStore_SchemaV10_FTS5Unavailable_SkipsVirtualTable(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	db := buildV9DB(t, path)
	t.Cleanup(func() { _ = db.Close() })

	require.NoError(t, applyMessagesFTSMigration(db, false),
		"the migration completes even when FTS5 is unavailable")

	assert.Equal(t, 10, readUserVersion(t, db),
		"v10 is stamped regardless of FTS5 availability (version line stays monotonic)")

	var tables int
	require.NoError(t, db.QueryRow(
		`SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='messages_fts'`).Scan(&tables))
	assert.Equal(t, 0, tables, "no virtual table when FTS5 is unavailable")

	var triggers int
	require.NoError(t, db.QueryRow(
		`SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name IN
		   ('messages_ai','messages_ad','messages_au')`).Scan(&triggers))
	assert.Equal(t, 0, triggers, "no sync triggers when FTS5 is unavailable")
}

// tableTriggers returns the trigger names defined on `table`.
func tableTriggers(t *testing.T, db *sql.DB, table string) []string {
	t.Helper()
	rows, err := db.Query(
		`SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?`, table)
	require.NoError(t, err)
	defer func() { _ = rows.Close() }()
	var names []string
	for rows.Next() {
		var n string
		require.NoError(t, rows.Scan(&n))
		names = append(names, n)
	}
	require.NoError(t, rows.Err())
	return names
}

// buildV9DB hand-builds a v9-shaped database at `path` and returns the open
// handle with user_version stamped to 9 (by migrateV8ToV9), so the next
// NewSQLiteStore open runs only the v9→v10 step. The caller owns closing the
// handle.
func buildV9DB(t *testing.T, path string) *sql.DB {
	t.Helper()
	db := buildV8DB(t, path) // open handle, user_version = 8
	require.NoError(t, migrateV8ToV9(db))
	return db
}
