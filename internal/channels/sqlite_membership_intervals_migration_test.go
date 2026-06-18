// RFC 0035 PR 1 — the `membership_intervals` ledger migration
// (channelStoreSchemaVersion v8 → v9).
//
// The migration is forward-only and a pure addition: it creates the
// append-only `membership_intervals` table, its `(channel_id,
// participant_id, joined_at)` lookup index, and the partial unique index
// `ux_membership_intervals_open` that guards the open-interval invariant,
// then backfills one OPEN interval per current `memberships` row (§D). No
// existing table, row, or index is touched, so every `memberships` /
// `messages` query reads back byte-identically post-v9.
//
// PR 1 ships the schema dormant — there is no Go read or write surface yet
// (PR 2 adds the reader, PR 3 the transactional write hooks). These tests
// therefore assert the storage contract only: the table/index shape, the
// backfill exactness, idempotent reopen, the invariant index, and the
// ON DELETE CASCADE that ties an interval row to its channel.
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

// TestSQLiteStore_SchemaV9_FreshDB_HasMembershipIntervalsTable asserts the
// ledger table and its two indexes exist after a fresh open.
func TestSQLiteStore_SchemaV9_FreshDB_HasMembershipIntervalsTable(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		// Table present with the §B columns.
		cols := tableColumns(t, db, "membership_intervals")
		require.NotEmpty(t, cols, "membership_intervals table missing")
		assert.Contains(t, cols, "id")
		assert.Contains(t, cols, "channel_id")
		assert.Contains(t, cols, "participant_id")
		assert.Contains(t, cols, "joined_at")
		assert.Contains(t, cols, "left_at")

		// Both indexes present.
		idx := tableIndexes(t, db, "membership_intervals")
		assert.Contains(t, idx, "idx_membership_intervals_lookup",
			"lookup index missing")
		assert.Contains(t, idx, "ux_membership_intervals_open",
			"partial unique open-interval index missing")
	})
}

// TestSQLiteStore_SchemaV9_Migration_Idempotent pins the schema version at the
// newest migration. Reopening the same file is a no-op (no duplicate-table /
// duplicate-index error, user_version stable at the latest). Per the
// convention the v5..v8 migration-test headers document, the literal-version
// pin lives in the newest migration's idempotent test — here.
func TestSQLiteStore_SchemaV9_Migration_Idempotent(t *testing.T) {
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
		assert.Equal(t, 9, channelStoreSchemaVersion,
			"RFC 0035 PR 1 bumps the channel store to v9")
	})
}

// TestSQLiteStore_Migration_V8ToV9_BackfillsOpenIntervals pins the §D backfill:
// a v8 database with N `memberships` rows produces exactly N OPEN intervals,
// each carrying its source row's `joined_at` — one stint per currently-present
// member, none closed.
func TestSQLiteStore_Migration_V8ToV9_BackfillsOpenIntervals(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")

	// Hand-build a v8-shaped database: baseline + v1→v2 … v7→v8, then stamp
	// user_version=8 so the v9 binary's loop runs only the v8→v9 step.
	db := buildV8DB(t, path)

	// A pre-v9 group channel with two members at distinct join times.
	_, err := db.Exec(
		`INSERT INTO channels (id, name, channel_type, description, created_at, session_id, epoch_id)
		   VALUES ('group:planning', 'planning', 'group', '', '2026-01-01T00:00:00Z', 'legacy', 'live')`)
	require.NoError(t, err)
	_, err = db.Exec(
		`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at)
		   VALUES ('group:planning', 'alice', 'always', '2026-01-01T00:00:00Z'),
		          ('group:planning', 'bob',   'always', '2026-01-02T09:30:00Z')`)
	require.NoError(t, err)
	require.NoError(t, db.Close())

	// Reopen — the v8→v9 migration runs and backfills the ledger.
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		var version int
		require.NoError(t, db.QueryRow(`PRAGMA user_version`).Scan(&version))
		assert.GreaterOrEqual(t, version, 9, "the v8→v9 migration ran")

		// Exactly one interval per membership row, all open, joined_at copied.
		type row struct {
			participant string
			joinedAt    string
			leftAt      sql.NullString
		}
		rows, err := db.Query(
			`SELECT participant_id, joined_at, left_at FROM membership_intervals
			   WHERE channel_id = 'group:planning' ORDER BY participant_id`)
		require.NoError(t, err)
		defer func() { _ = rows.Close() }()
		var got []row
		for rows.Next() {
			var r row
			require.NoError(t, rows.Scan(&r.participant, &r.joinedAt, &r.leftAt))
			got = append(got, r)
		}
		require.NoError(t, rows.Err())

		require.Len(t, got, 2, "exactly one interval seeded per current member")
		assert.Equal(t, "alice", got[0].participant)
		assert.Equal(t, "2026-01-01T00:00:00Z", got[0].joinedAt,
			"backfilled joined_at equals the source membership row")
		assert.False(t, got[0].leftAt.Valid, "backfilled interval is open (left_at NULL)")
		assert.Equal(t, "bob", got[1].participant)
		assert.Equal(t, "2026-01-02T09:30:00Z", got[1].joinedAt)
		assert.False(t, got[1].leftAt.Valid, "backfilled interval is open (left_at NULL)")
	})
}

// TestSQLiteStore_Migration_V8ToV9_EmptyMemberships seeds zero intervals when
// there are no `memberships` rows — the backfill INSERT…SELECT is a clean no-op.
func TestSQLiteStore_Migration_V8ToV9_EmptyMemberships(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	db := buildV8DB(t, path)
	require.NoError(t, db.Close())

	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		var n int
		require.NoError(t, db.QueryRow(`SELECT COUNT(*) FROM membership_intervals`).Scan(&n))
		assert.Equal(t, 0, n, "no memberships ⇒ no intervals")
	})
}

// TestSQLiteStore_SchemaV9_OpenIntervalUniqueness pins the invariant guard: a
// second OPEN interval (left_at NULL) for the same (channel_id, participant_id)
// fails the partial unique index, but a CLOSED interval (non-NULL left_at) for
// an already-open pair is allowed — the index is partial on `left_at IS NULL`,
// which is what lets join → leave → rejoin keep a closed stint beside an open
// one (PR 3's lifecycle).
func TestSQLiteStore_SchemaV9_OpenIntervalUniqueness(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	require.NoError(t, store.CreateChannel(context.Background(), Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	}))

	withDB(t, path, func(db *sql.DB) {
		// First open interval — fine.
		_, err := db.Exec(
			`INSERT INTO membership_intervals (channel_id, participant_id, joined_at, left_at)
			   VALUES ('group:planning', 'alice', '2026-01-01T00:00:00Z', NULL)`)
		require.NoError(t, err)

		// Second open interval for the same pair — rejected by ux_…_open.
		_, err = db.Exec(
			`INSERT INTO membership_intervals (channel_id, participant_id, joined_at, left_at)
			   VALUES ('group:planning', 'alice', '2026-01-03T00:00:00Z', NULL)`)
		require.Error(t, err, "a double-open must fail the partial unique index")

		// A CLOSED interval for the same pair is allowed (partial index skips it).
		_, err = db.Exec(
			`INSERT INTO membership_intervals (channel_id, participant_id, joined_at, left_at)
			   VALUES ('group:planning', 'alice', '2025-12-01T00:00:00Z', '2025-12-15T00:00:00Z')`)
		require.NoError(t, err, "a closed interval beside an open one is allowed")
	})
}

// TestSQLiteStore_SchemaV9_CascadeOnChannelDelete asserts ON DELETE CASCADE:
// removing a channel discards its `membership_intervals` rows along with its
// memberships and messages, so a deleted channel is consistently unrecallable.
func TestSQLiteStore_SchemaV9_CascadeOnChannelDelete(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	ctx := context.Background()
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	}))

	withDB(t, path, func(db *sql.DB) {
		_, err := db.Exec(
			`INSERT INTO membership_intervals (channel_id, participant_id, joined_at, left_at)
			   VALUES ('group:planning', 'alice', '2026-01-01T00:00:00Z', NULL)`)
		require.NoError(t, err)
	})

	require.NoError(t, store.DeleteChannel(ctx, "group:planning"))

	withDB(t, path, func(db *sql.DB) {
		var n int
		require.NoError(t, db.QueryRow(
			`SELECT COUNT(*) FROM membership_intervals WHERE channel_id = 'group:planning'`).Scan(&n))
		assert.Equal(t, 0, n, "deleting a channel cascades to its membership_intervals")
	})
}

// buildV8DB hand-builds a v8-shaped database at `path` and returns the open
// handle with user_version stamped to 8, so the next NewSQLiteStore open runs
// only the v8→v9 step. The caller owns closing the handle.
func buildV8DB(t *testing.T, path string) *sql.DB {
	t.Helper()
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
	_, err = db.Exec(`PRAGMA user_version = 8`)
	require.NoError(t, err)
	return db
}
