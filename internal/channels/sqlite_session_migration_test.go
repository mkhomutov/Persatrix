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
	"time"

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
// efficient. It did lose the time-ordered channel scan, which v14 restores
// with an index of its own (ISSUE-0188).
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
		assert.Equal(t, channelStoreSchemaVersion, version,
			"user_version stamped to the latest schema version; reopen is a no-op")
	})
}

// TestSQLiteStore_SchemaV3_Reopen_DoesNotResurrectDroppedV2Index pins the
// PR #335 review M1 regression. Before the fix, `applySchema` ran
// `schemaV1SQL` unconditionally on every open. That string contains
// `CREATE INDEX IF NOT EXISTS idx_messages_channel_ts`; `IF NOT EXISTS`
// checks by *name*, and `migrateV2ToV3` drops that index by name. So on
// every reopen of a v3 database, the index name was free, the create
// statement ran, and the dropped index came back. Because user_version
// was already 3 the migration loop was a no-op and the drop never re-ran
// — the database silently ended up carrying both the v3 covering index
// AND the v2 chronological index after every restart, defeating the
// migration's stated design intent (single covering index — see
// `migrateV2ToV3` header) and adding per-INSERT / per-DELETE write
// amplification in perpetuity. The sibling test
// `TestSQLiteStore_SchemaV3_Migration_Idempotent` above pinned
// `user_version` stability across reopen but not the index set — the gap
// that let this slip past two prior review passes. This test closes that
// gap with explicit positive and negative assertions so any future
// regression of the same shape (a later migration drops something
// `schemaV1SQL` creates) surfaces as a labelled failure rather than a
// generic set-equality mismatch buried in a diff.
func TestSQLiteStore_SchemaV3_Reopen_DoesNotResurrectDroppedV2Index(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")

	store1, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	require.NoError(t, store1.Close())

	var firstOpenIdx []string
	withDB(t, path, func(db *sql.DB) {
		firstOpenIdx = tableIndexes(t, db, "messages")
	})
	require.NotContains(t, firstOpenIdx, "idx_messages_channel_ts",
		"precondition: v2 chronological index already dropped after first open")

	store2, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store2.Close() })

	var secondOpenIdx []string
	withDB(t, path, func(db *sql.DB) {
		secondOpenIdx = tableIndexes(t, db, "messages")
	})

	assert.ElementsMatch(t, firstOpenIdx, secondOpenIdx,
		"messages index set must not drift across reopens — first=%v second=%v",
		firstOpenIdx, secondOpenIdx)
	assert.NotContains(t, secondOpenIdx, "idx_messages_channel_ts",
		"v2 chronological index (dropped by v2→v3) must not be resurrected by schemaV1SQL on reopen")
	assert.Contains(t, secondOpenIdx, "idx_messages_channel_session",
		"v3 covering index must survive reopen")
	assert.Contains(t, secondOpenIdx, "idx_messages_thread",
		"thread index must survive reopen")
}

// TestSQLiteStore_SchemaV3_GetHistory_NeverWalksTheDroppedV2Index pins the
// query plan for the channel-history hot path. PR #335 review M1 deferred the
// EXPLAIN regression as a follow-up: the v2→v3 migration drops
// `idx_messages_channel_ts(channel_id, timestamp DESC)` and replaces it
// with the covering `idx_messages_channel_session(channel_id, session_id,
// timestamp DESC)`.
//
// Until v14 this test pinned that covering index as the one the planner
// takes, on the reasoning that its `channel_id` prefix kept the
// chronological scan cheap and that skip-scan would satisfy the ORDER BY from
// it. Neither held: with `session_id` in the middle, the planner sorted the
// whole channel (`USE TEMP B-TREE FOR ORDER BY`; ISSUE-0188). It also
// explained a hand-copied query, which had drifted from GetHistory's.
//
// It now explains GetHistory's own query ([historyQuery]) and checks two
// things: the read walks `idx_messages_channel_time`, the index channels-store
// v14 restored under a new name, and never the v2 index. The second holds
// while the v2 index stays out of the schema, as
// TestSQLiteStore_SchemaV3_FreshDB_HasIndexes pins; it is kept for a later
// migration that brings the name back. The query-plan tests beside v14's
// migration also pin that the read does not sort.
func TestSQLiteStore_SchemaV3_GetHistory_NeverWalksTheDroppedV2Index(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		// The no-`before` branch, the dominant call path; the `before`
		// branch has the same prefix, so a regression on one would surface
		// in either.
		q, args := historyQuery("group:planning", 50, time.Time{})
		joined := queryPlan(t, db, q, args...)
		assert.Contains(t, joined, "idx_messages_channel_time",
			"GetHistory must walk v14's channel-time index (channel_id, timestamp DESC); plan=%s", joined)
		assert.NotContains(t, joined, "idx_messages_channel_ts",
			"v2 chronological index must be dropped during v2→v3; plan=%s", joined)
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

// tablePKColumns returns the primary-key column names of `table` in key
// order, as reported by PRAGMA table_info (`pk` is the 1-based position
// within the PK, 0 for non-key columns).
func tablePKColumns(t *testing.T, db *sql.DB, table string) []string {
	t.Helper()
	rows, err := db.Query(
		`SELECT name FROM pragma_table_info(?) WHERE pk > 0 ORDER BY pk`, table)
	require.NoError(t, err)
	defer func() { _ = rows.Close() }()
	var cols []string
	for rows.Next() {
		var n string
		require.NoError(t, rows.Scan(&n))
		cols = append(cols, n)
	}
	require.NoError(t, rows.Err())
	return cols
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
