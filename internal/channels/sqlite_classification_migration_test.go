// RFC 0037 PR 1 (v0.3.12) — the `channels.classification` column migration
// (channelStoreSchemaVersion v10 → v11) plus the DM-creation stamping it
// enables. The column is dark substrate: these tests read it with raw SQL
// (withDB) precisely because no store API reads it yet — the first reader is
// the RFC 0037 PR 2 wire lift.
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

// channelClassification reads the raw column for one channel row.
func channelClassification(t *testing.T, db *sql.DB, channelID string) string {
	t.Helper()
	var got string
	require.NoError(t, db.QueryRow(
		`SELECT classification FROM channels WHERE id = ?`, channelID).Scan(&got))
	return got
}

// TestSQLiteStore_SchemaV11_FreshDB_ChannelsHasClassificationColumn asserts a
// fresh database lands with the column present.
func TestSQLiteStore_SchemaV11_FreshDB_ChannelsHasClassificationColumn(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		assert.Contains(t, tableColumns(t, db, "channels"), "classification",
			"channels.classification missing")
	})
}

// TestSQLiteStore_SchemaV11_Migration_Idempotent pins the literal latest
// version (the newest migration's test owns the literal pin, per the v5/v6
// test-header convention) and that a reopen is a no-op.
func TestSQLiteStore_SchemaV11_Migration_Idempotent(t *testing.T) {
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
		assert.Equal(t, 11, version,
			"channelStoreSchemaVersion is v11 as of RFC 0037 PR 1; reopen is a no-op")
		assert.Equal(t, channelStoreSchemaVersion, version,
			"the literal pin and the const must agree")
	})
}

// TestSQLiteStore_Migration_V10ToV11_BackfillsInternal pins the data
// migration on a POPULATED v10 store: existing group and DM rows are carried
// forward unchanged and every one of them backfills to `internal` — §A rule
// (a): a channel that predates classification is confidential-by-default,
// never `public`.
func TestSQLiteStore_Migration_V10ToV11_BackfillsInternal(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")

	// Hand-build a v10-shaped database: baseline + every migration through
	// v9→v10 (each stamps its own user_version, so the file reads back as a
	// genuine v10 store the v11 binary's loop advances exactly one step).
	db, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	_, err = db.Exec(schemaV1SQL)
	require.NoError(t, err)
	require.NoError(t, migrateV1ToV2(db))
	require.NoError(t, migrateV2ToV3(db))
	require.NoError(t, migrateV3ToV4(db))
	require.NoError(t, migrateV4ToV5(db))
	require.NoError(t, migrateV5ToV6(db))
	require.NoError(t, migrateV6ToV7(db))
	require.NoError(t, migrateV7ToV8(db))
	require.NoError(t, migrateV8ToV9(db))
	require.NoError(t, migrateV9ToV10(db))

	// Pre-v11 rows: one declared group channel, one on-demand DM.
	_, err = db.Exec(
		`INSERT INTO channels (id, name, channel_type, description, created_at, session_id, epoch_id)
		   VALUES ('group:planning', 'planning', 'group', '', '2026-01-01T00:00:00Z', 'legacy', 'live')`)
	require.NoError(t, err)
	_, err = db.Exec(
		`INSERT INTO channels (id, name, channel_type, description, created_at, session_id, epoch_id)
		   VALUES ('dm:alice:bob', NULL, 'dm', '', '2026-01-01T00:00:00Z', 'legacy', 'live')`)
	require.NoError(t, err)
	require.NoError(t, db.Close())

	// Reopen — the v10→v11 migration runs.
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		var version int
		require.NoError(t, db.QueryRow(`PRAGMA user_version`).Scan(&version))
		assert.Equal(t, channelStoreSchemaVersion, version, "the v10→v11 migration ran")

		assert.Equal(t, "internal", channelClassification(t, db, "group:planning"),
			"pre-v11 group row backfills to internal (§A rule (a))")
		assert.Equal(t, "internal", channelClassification(t, db, "dm:alice:bob"),
			"pre-v11 DM row backfills to internal (§A rule (a))")
	})

	// No data loss: the rows still resolve through the store API.
	ch, err := store.GetChannel(context.Background(), "group:planning")
	require.NoError(t, err)
	assert.Equal(t, "planning", ch.Name)
}

// TestMigrateV10ToV11_StampsUserVersionInTransaction drives the single
// migration step directly (the PR #335 review L3 property, pinned per-step
// like the v1→v2 / v2→v3 originals): after the step, user_version reads 11 —
// the stamp committed atomically with the ALTER.
func TestMigrateV10ToV11_StampsUserVersionInTransaction(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	db, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	t.Cleanup(func() { _ = db.Close() })

	_, err = db.Exec(schemaV1SQL)
	require.NoError(t, err)
	require.NoError(t, migrateV1ToV2(db))
	require.NoError(t, migrateV2ToV3(db))
	require.NoError(t, migrateV3ToV4(db))
	require.NoError(t, migrateV4ToV5(db))
	require.NoError(t, migrateV5ToV6(db))
	require.NoError(t, migrateV6ToV7(db))
	require.NoError(t, migrateV7ToV8(db))
	require.NoError(t, migrateV8ToV9(db))
	require.NoError(t, migrateV9ToV10(db))

	require.NoError(t, migrateV10ToV11(db))

	var version int
	require.NoError(t, db.QueryRow(`PRAGMA user_version`).Scan(&version))
	assert.Equal(t, 11, version, "v10→v11 stamps user_version inside its own tx")
}
