// RFC 0031 Phase 1 PR 2 — sessions table + session_id columns migration
// (channelStoreSchemaVersion v2 → v3).
//
// The migration is forward-only and idempotent. It introduces the `sessions`
// table, adds `session_id TEXT NOT NULL DEFAULT 'legacy'` to `channels` and
// `messages`, swaps the chronological-scan index for a covering
// `(channel_id, session_id, timestamp DESC)` shape, and adds a per-table
// session lookup index. Phase 1 ships no recall changes — these tests assert
// the storage contract only.
package channels

import (
	"context"
	"database/sql"
	"path/filepath"
	"sort"
	"testing"

	_ "modernc.org/sqlite"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestSQLiteStore_SchemaV3_FreshDB_HasSessionsTable asserts a freshly-opened
// store materialises the `sessions` table with the columns RFC 0031 §D
// specifies. The plan defers seeding of the `legacy` row to Phase 3 CLI, so
// the table is empty after migration.
func TestSQLiteStore_SchemaV3_FreshDB_HasSessionsTable(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	db, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	defer func() { _ = db.Close() }()

	cols := tableColumns(t, db, "sessions")
	assert.ElementsMatch(t,
		[]string{"id", "label", "created_at", "archived_at", "metadata_json"},
		cols,
		"sessions table columns")

	var count int
	require.NoError(t, db.QueryRow(`SELECT COUNT(1) FROM sessions`).Scan(&count))
	assert.Equal(t, 0, count, "sessions table is created empty (Phase 1 — no seed row)")
}

// TestSQLiteStore_SchemaV3_FreshDB_ChannelsHasSessionIDColumn asserts the
// channels table grows a `session_id` column defaulted to `legacy`. A row
// inserted without an explicit session_id picks up the default.
func TestSQLiteStore_SchemaV3_FreshDB_ChannelsHasSessionIDColumn(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		assert.Contains(t, tableColumns(t, db, "channels"), "session_id",
			"channels.session_id missing")
	})

	ctx := context.Background()
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	}))

	withDB(t, path, func(db *sql.DB) {
		var sid string
		require.NoError(t, db.QueryRow(
			`SELECT session_id FROM channels WHERE id = ?`, "group:planning").Scan(&sid))
		assert.Equal(t, "legacy", sid, "empty SessionID defaults to legacy")
	})
}

// TestSQLiteStore_SchemaV3_FreshDB_MessagesHasSessionIDColumn mirrors the
// channels assertion for the messages table.
func TestSQLiteStore_SchemaV3_FreshDB_MessagesHasSessionIDColumn(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		assert.Contains(t, tableColumns(t, db, "messages"), "session_id",
			"messages.session_id missing")
	})

	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice")
	mid := uuid.NewString()
	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID: mid, ChannelID: id, SenderID: "alice", Content: "hi",
	}))

	withDB(t, path, func(db *sql.DB) {
		var sid string
		require.NoError(t, db.QueryRow(
			`SELECT session_id FROM messages WHERE id = ?`, mid).Scan(&sid))
		assert.Equal(t, "legacy", sid, "empty SessionID defaults to legacy on publish")
	})
}

// TestSQLiteStore_SchemaV3_FreshDB_HasIndexes asserts the v3 index set is
// present after migration. The chronological-scan index is replaced by a
// covering shape that lets a future per-session filter (Phase 2) stay
// efficient without losing the channel-prefix scan today.
func TestSQLiteStore_SchemaV3_FreshDB_HasIndexes(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		idx := tableIndexes(t, db, "messages")
		assert.Contains(t, idx, "idx_messages_channel_session",
			"covering chronological index missing")
		assert.NotContains(t, idx, "idx_messages_channel_ts",
			"v2 chronological index must be dropped during v2→v3")
		assert.Contains(t, idx, "idx_messages_thread",
			"thread index preserved across migration")

		channelIdx := tableIndexes(t, db, "channels")
		assert.Contains(t, channelIdx, "idx_channels_session",
			"per-session channel lookup index missing")
	})
}

// TestSQLiteStore_SchemaV3_Migration_Idempotent asserts that opening the
// store twice over the same file is a no-op: schema version stays at v3 and
// the migration does not re-run (no duplicate index errors, no data drift).
func TestSQLiteStore_SchemaV3_Migration_Idempotent(t *testing.T) {
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
		assert.Equal(t, 3, version, "user_version stamped to v3")
	})
}

// TestSQLiteStore_SchemaV3_LegacyCarveoutShape asserts the WHERE-clause
// shape that Phase 2 will run against existing v3 databases: a row with
// `session_id = 'legacy'` is returned by a query that ORs the active session
// against the legacy carve-out. Phase 1 has no recall code yet, so we
// exercise the predicate directly to lock in the storage invariant.
func TestSQLiteStore_SchemaV3_LegacyCarveoutShape(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()

	// Channel A is implicitly legacy (no SessionID supplied).
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:planning-a", Name: "planning-a", Type: ChannelTypeGroup,
	}))
	require.NoError(t, store.AddMember(ctx, "group:planning-a", "alice", RespondAlways))
	// Channel B is explicitly tagged run-a.
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:planning-b", Name: "planning-b", Type: ChannelTypeGroup,
		SessionID: "run-a",
	}))
	require.NoError(t, store.AddMember(ctx, "group:planning-b", "alice", RespondAlways))

	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: "group:planning-a", SenderID: "alice",
		Content: "legacy-msg",
	}))
	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: "group:planning-b", SenderID: "alice",
		Content: "run-a-msg", SessionID: "run-a",
	}))

	withDB(t, path, func(db *sql.DB) {
		// Phase-2 recall shape ORs the active session with the legacy carve-out.
		rows, err := db.Query(
			`SELECT content FROM messages
			   WHERE session_id IN ('run-a') OR session_id = 'legacy'
			   ORDER BY content ASC`)
		require.NoError(t, err)
		defer func() { _ = rows.Close() }()
		var got []string
		for rows.Next() {
			var c string
			require.NoError(t, rows.Scan(&c))
			got = append(got, c)
		}
		require.NoError(t, rows.Err())
		assert.Equal(t, []string{"legacy-msg", "run-a-msg"}, got)
	})
}

// tableColumns returns the column names of `table` as reported by
// PRAGMA table_info. Sorting is alphabetical to keep ElementsMatch
// assertions order-independent.
func tableColumns(t *testing.T, db *sql.DB, table string) []string {
	t.Helper()
	rows, err := db.Query(`SELECT name FROM pragma_table_info(?)`, table)
	require.NoError(t, err)
	defer func() { _ = rows.Close() }()
	var cols []string
	for rows.Next() {
		var n string
		require.NoError(t, rows.Scan(&n))
		cols = append(cols, n)
	}
	require.NoError(t, rows.Err())
	sort.Strings(cols)
	return cols
}

// tableIndexes returns the index names defined on `table` (excluding
// SQLite's auto-indexes for PK / UNIQUE column constraints).
func tableIndexes(t *testing.T, db *sql.DB, table string) []string {
	t.Helper()
	rows, err := db.Query(
		`SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? AND name NOT LIKE 'sqlite_autoindex_%'`,
		table)
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

// withDB opens a fresh *sql.DB against `path` via the same DSN the store
// uses, hands it to `fn`, and closes it before returning. Centralises the
// open/close pair so test bodies cannot leak handles — modernc.org/sqlite
// holds an exclusive Windows file lock that blocks t.TempDir() cleanup
// when a connection survives the test.
func withDB(t *testing.T, path string, fn func(*sql.DB)) {
	t.Helper()
	db, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	defer func() { _ = db.Close() }()
	fn(db)
}
