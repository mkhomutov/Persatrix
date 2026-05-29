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
	"strings"
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

// TestSQLiteStore_SchemaV3_GetHistory_UsesCoveringIndex pins the v3 query
// plan for the channel-history hot path. PR #335 review M1 deferred the
// EXPLAIN regression as a follow-up: the v2→v3 migration drops
// `idx_messages_channel_ts(channel_id, timestamp DESC)` and replaces it
// with the covering `idx_messages_channel_session(channel_id, session_id,
// timestamp DESC)`. The chronological-scan shape (`WHERE channel_id = ?
// ORDER BY timestamp DESC LIMIT ?`) used by `GetHistory` / `pruneExcess`
// still has its equality prefix in the new index, so SQLite walks it.
// This test captures that as a tested invariant so a future schema change
// surfaces the cost early.
//
// Scope note: the test does not assert anything about SORT steps. In a
// single-session-per-channel deployment (Phase 1 reality — every row is
// `session_id='legacy'` unless `PERSATRIX_SESSION_ID` is set) SQLite's
// skip-scan optimisation lets it satisfy the ORDER BY from the index
// directly. Once a channel accumulates rows under multiple `session_id`
// values, the planner introduces a sort step; that trade-off is
// documented in [docs/rfcs/0031-pr-plan.md §PR 2 key implementation
// details]. Pinning the sort-vs-no-sort transition requires Phase 2 read
// shapes that this PR does not ship — locked here only as a forward-
// looking comment, not an assertion.
func TestSQLiteStore_SchemaV3_GetHistory_UsesCoveringIndex(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		// EXPLAIN QUERY PLAN against the exact GetHistory query shape
		// (no-`before` branch — the dominant call path; the `before`
		// branch uses the same prefix, so a regression on one would
		// surface in either).
		rows, err := db.Query(
			`EXPLAIN QUERY PLAN
			 SELECT id, channel_id, sender_id, content, timestamp, thread_id, mentions, metadata, session_id
			   FROM messages
			  WHERE channel_id = ?
			  ORDER BY timestamp DESC
			  LIMIT ?`, "group:planning", 50)
		require.NoError(t, err)
		defer func() { _ = rows.Close() }()

		var plan []string
		for rows.Next() {
			// EXPLAIN QUERY PLAN columns: (id INT, parent INT,
			// notused INT, detail TEXT). The textual `detail` is
			// the part we assert against; the integer columns are
			// SQLite planner bookkeeping and are unstable across
			// versions, so we drop them.
			var id, parent, notused int
			var detail string
			require.NoError(t, rows.Scan(&id, &parent, &notused, &detail))
			plan = append(plan, detail)
		}
		require.NoError(t, rows.Err())

		joined := strings.Join(plan, " | ")
		assert.Contains(t, joined, "idx_messages_channel_session",
			"GetHistory must walk the v3 covering index (channel_id, session_id, timestamp DESC); plan=%s", joined)
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

// --- ISSUE-0082 PR 1: session_bindings table (channelStoreSchemaVersion v3 → v4) ---

// TestSQLiteStore_SchemaV4_FreshDB_HasSessionBindingsTable asserts a freshly
// opened store materialises the `session_bindings` table with the columns
// and composite primary key ISSUE-0082 specifies. The table is the
// (agent, channel, user) → session_id map the orchestrator's per-request
// SessionResolver writes; it is empty after migration (no seed row).
func TestSQLiteStore_SchemaV4_FreshDB_HasSessionBindingsTable(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		cols := tableColumns(t, db, "session_bindings")
		assert.ElementsMatch(t,
			[]string{"agent_id", "channel_id", "user_id", "session_id", "created_at"},
			cols, "session_bindings table columns")

		assert.Equal(t,
			[]string{"agent_id", "channel_id", "user_id"},
			tablePKColumns(t, db, "session_bindings"),
			"session_bindings composite PK = (agent_id, channel_id, user_id) in order")

		var count int
		require.NoError(t, db.QueryRow(`SELECT COUNT(1) FROM session_bindings`).Scan(&count))
		assert.Equal(t, 0, count, "session_bindings is created empty (no seed row)")
	})
}

// TestSQLiteStore_SchemaV4_Migration_Idempotent asserts that opening the
// store twice over the same file leaves user_version at v4 and does not
// re-run the migration (no duplicate-table error, no data drift).
func TestSQLiteStore_SchemaV4_Migration_Idempotent(t *testing.T) {
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
		assert.Equal(t, 4, version, "user_version stamped to v4")
	})
}

// TestSQLiteStore_Migration_V3ToV4_ExistingRowsUntouched simulates a v3
// database opened by a v4 binary: the new `session_bindings` table is
// added and pre-existing channel / message rows are left exactly as they
// were (no backfill, no session_id rewrite). v3 is the last schema before
// this PR, so this is the realistic upgrade path.
func TestSQLiteStore_Migration_V3ToV4_ExistingRowsUntouched(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")

	// Hand-build a v3-shaped database by running the baseline + the v1→v2
	// and v2→v3 steps directly, then stamp user_version=3 so the v4
	// binary's loop runs only the v3→v4 step.
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

	// Reopen — the v3→v4 migration runs.
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	got, err := store.GetChannel(context.Background(), "group:planning")
	require.NoError(t, err)
	assert.Equal(t, "run-a", got.SessionID, "existing channel row session_id untouched by v3→v4")
	msg, err := store.GetMessage(context.Background(), "m-1")
	require.NoError(t, err)
	assert.Equal(t, "run-a", msg.SessionID, "existing message row session_id untouched by v3→v4")

	withDB(t, path, func(db *sql.DB) {
		var version int
		require.NoError(t, db.QueryRow(`PRAGMA user_version`).Scan(&version))
		assert.Equal(t, 4, version)

		assert.Contains(t, tableColumns(t, db, "session_bindings"), "session_id",
			"session_bindings table created by v3→v4")
		var n int
		require.NoError(t, db.QueryRow(`SELECT COUNT(1) FROM session_bindings`).Scan(&n))
		assert.Equal(t, 0, n, "v3→v4 adds an empty binding table — no backfill")
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
