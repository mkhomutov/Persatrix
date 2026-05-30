// session_bindings migration tests across the binding-table schema steps:
// v3→v4 (ISSUE-0082 PR 1 — table introduced on the `(agent, channel, user)`
// triple) and v4→v5 (ISSUE-0083 — the sender axis dropped, collapsing the
// table onto the `(agent, channel)` pair / room-continuity unit).
//
// Split out of sqlite_session_migration_test.go (which covers the v2→v3
// sessions-table step and owns the shared PRAGMA helpers) to keep each file
// under the 500-line code cap. The helpers — withDB / tableColumns /
// tablePKColumns — live in the sibling file at package scope.
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

// --- ISSUE-0083: session_bindings sender-axis drop (channelStoreSchemaVersion v4 → v5) ---

// TestSQLiteStore_SchemaV5_FreshDB_HasSessionBindingsTable asserts a freshly
// opened store materialises the post-ISSUE-0083 `session_bindings` table:
// the `user_id` column is gone and the primary key is the (agent, channel)
// pair (room continuity, RFC 0031 §A scope-axes amendment). The table is the
// (agent, channel) → session_id map the orchestrator's per-request
// SessionResolver writes; it is empty after migration (no seed row).
func TestSQLiteStore_SchemaV5_FreshDB_HasSessionBindingsTable(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		cols := tableColumns(t, db, "session_bindings")
		assert.ElementsMatch(t,
			[]string{"agent_id", "channel_id", "session_id", "created_at"},
			cols, "session_bindings table columns (user_id dropped in v5)")

		assert.Equal(t,
			[]string{"agent_id", "channel_id"},
			tablePKColumns(t, db, "session_bindings"),
			"session_bindings composite PK = (agent_id, channel_id) in order — sender axis dropped")

		var count int
		require.NoError(t, db.QueryRow(`SELECT COUNT(1) FROM session_bindings`).Scan(&count))
		assert.Equal(t, 0, count, "session_bindings is created empty (no seed row)")
	})
}

// TestSQLiteStore_SchemaV5_Migration_Idempotent asserts that opening the
// store twice over the same file leaves user_version at the latest schema
// version and does not re-run the migration (no duplicate-table error, no
// data drift).
func TestSQLiteStore_SchemaV5_Migration_Idempotent(t *testing.T) {
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
		assert.Equal(t, channelStoreSchemaVersion, version, "user_version stamped to the latest schema version")
		assert.Equal(t, 5, channelStoreSchemaVersion, "ISSUE-0083 bumps the channel store to v5")
	})
}

// TestSQLiteStore_Migration_V4ToV5_CollapsesSenderAxis pins the ISSUE-0083
// data migration: an existing v4 database whose `session_bindings` carry the
// `(agent, channel, user)` triple is rebuilt onto the `(agent, channel)` pair.
// Two senders in one room collapse to ONE binding — the earliest-created one
// wins so the room keeps its oldest continuity — while a single-sender DM
// binding survives unchanged. The losing session rows in the `sessions`
// registry are left in place (archive/list still resolve them); only the
// binding map collapses.
func TestSQLiteStore_Migration_V4ToV5_CollapsesSenderAxis(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")

	// Hand-build a v4-shaped database: baseline + v1→v2 + v2→v3 + v3→v4, then
	// stamp user_version=4 so the v5 binary's loop runs only the v4→v5 step.
	db, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	_, err = db.Exec(schemaV1SQL)
	require.NoError(t, err)
	require.NoError(t, migrateV1ToV2(db))
	require.NoError(t, migrateV2ToV3(db))
	require.NoError(t, migrateV3ToV4(db))
	_, err = db.Exec(`PRAGMA user_version = 4`)
	require.NoError(t, err)

	// Group room with two senders → two triple-keyed bindings. The earliest
	// (alice, created_at=100.0) must win the collapse.
	_, err = db.Exec(
		`INSERT INTO session_bindings (agent_id, channel_id, user_id, session_id, created_at) VALUES
		   ('agent-b', 'group:planning', 'alice', 'sess-alice', 100.0),
		   ('agent-b', 'group:planning', 'bob',   'sess-bob',   200.0),
		   ('agent-b', 'dm:c:b',         'carol', 'sess-carol', 150.0)`)
	require.NoError(t, err)
	require.NoError(t, db.Close())

	// Reopen — the v4→v5 migration runs the collapse.
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		var version int
		require.NoError(t, db.QueryRow(`PRAGMA user_version`).Scan(&version))
		assert.Equal(t, 5, version, "v4→v5 ran")

		assert.ElementsMatch(t,
			[]string{"agent_id", "channel_id", "session_id", "created_at"},
			tableColumns(t, db, "session_bindings"),
			"user_id column dropped after collapse")

		// The group room collapsed to one binding — the earliest (alice) wins.
		var groupSession string
		require.NoError(t, db.QueryRow(
			`SELECT session_id FROM session_bindings WHERE agent_id='agent-b' AND channel_id='group:planning'`).
			Scan(&groupSession))
		assert.Equal(t, "sess-alice", groupSession,
			"earliest-created binding wins the (agent, channel) collapse")

		var groupRows int
		require.NoError(t, db.QueryRow(
			`SELECT COUNT(1) FROM session_bindings WHERE agent_id='agent-b' AND channel_id='group:planning'`).
			Scan(&groupRows))
		assert.Equal(t, 1, groupRows, "two senders in one room collapse to a single binding")

		// The single-sender DM binding survives unchanged.
		var dmSession string
		require.NoError(t, db.QueryRow(
			`SELECT session_id FROM session_bindings WHERE agent_id='agent-b' AND channel_id='dm:c:b'`).
			Scan(&dmSession))
		assert.Equal(t, "sess-carol", dmSession, "single-sender DM binding preserved")

		var total int
		require.NoError(t, db.QueryRow(`SELECT COUNT(1) FROM session_bindings`).Scan(&total))
		assert.Equal(t, 2, total, "three triple bindings collapse to two pair bindings")
	})

	// The resolver now reads the collapsed binding: resolving the group room
	// returns the surviving room session, regardless of which sender speaks.
	got, err := mustResolver(t, store).Resolve(context.Background(), "agent-b", "group:planning")
	require.NoError(t, err)
	assert.Equal(t, "sess-alice", got,
		"post-collapse resolve returns the surviving room session for the (agent, channel) pair")
}

// TestSQLiteStore_Migration_V3ToV4_ExistingRowsUntouched simulates a v3
// database opened by this binary: the new `session_bindings` table is added
// (v3→v4) and pre-existing channel / message rows are left exactly as they
// were (no backfill, no session_id rewrite). The binary then runs v4→v5 on
// the freshly-created (empty) binding table, so it lands at the latest
// version with the rows still untouched.
func TestSQLiteStore_Migration_V3ToV4_ExistingRowsUntouched(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")

	// Hand-build a v3-shaped database by running the baseline + the v1→v2
	// and v2→v3 steps directly, then stamp user_version=3 so the binary's
	// loop runs from the v3→v4 step.
	db, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	_, err = db.Exec(schemaV1SQL)
	require.NoError(t, err)
	require.NoError(t, migrateV1ToV2(db))
	require.NoError(t, migrateV2ToV3(db))
	_, err = db.Exec(`PRAGMA user_version = 3`)
	require.NoError(t, err)

	// Seed a v3 channel + message carrying a real (non-legacy) session_id
	// so a stray backfill UPDATE would be observable.
	_, err = db.Exec(
		`INSERT INTO channels (id, name, channel_type, description, created_at, session_id)
		 VALUES ('group:planning', 'planning', 'group', '', datetime('now'), 'run-a')`)
	require.NoError(t, err)
	_, err = db.Exec(
		`INSERT INTO messages (id, channel_id, sender_id, content, timestamp, session_id)
		 VALUES ('m-1', 'group:planning', 'alice', 'pre-upgrade', datetime('now'), 'run-a')`)
	require.NoError(t, err)
	require.NoError(t, db.Close())

	// Reopen — the v3→v4 then v4→v5 migrations run.
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	got, err := store.GetChannel(context.Background(), "group:planning")
	require.NoError(t, err)
	assert.Equal(t, "run-a", got.SessionID, "existing channel row session_id untouched by the migration chain")
	msg, err := store.GetMessage(context.Background(), "m-1")
	require.NoError(t, err)
	assert.Equal(t, "run-a", msg.SessionID, "existing message row session_id untouched by the migration chain")

	withDB(t, path, func(db *sql.DB) {
		var version int
		require.NoError(t, db.QueryRow(`PRAGMA user_version`).Scan(&version))
		// A v3 DB opened by this binary runs v3→v4 then v4→v5; it lands at the
		// latest version, not v4.
		assert.Equal(t, channelStoreSchemaVersion, version)

		assert.Contains(t, tableColumns(t, db, "session_bindings"), "session_id",
			"session_bindings table created by v3→v4 (and rebuilt by v4→v5)")
		var n int
		require.NoError(t, db.QueryRow(`SELECT COUNT(1) FROM session_bindings`).Scan(&n))
		assert.Equal(t, 0, n, "empty binding table — no backfill (v4→v5 collapse of zero rows is zero)")
	})
}
