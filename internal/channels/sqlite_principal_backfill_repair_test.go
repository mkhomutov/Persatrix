package channels

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// migrateV11ToV12Original replays the ORIGINAL v11→v12 migration — the one
// that shipped before the PR B2 review inverted its backfill — so the repair
// can be tested against the state it actually exists to fix. Deliberately a
// verbatim copy of the old statement rather than a call into the current
// migration: the point is the `'local'` DEFAULT the current code no longer
// writes, and a test that could not reproduce it would pass against a repair
// that did nothing.
func migrateV11ToV12Original(t *testing.T, db *sql.DB) {
	t.Helper()
	tx, err := db.Begin()
	require.NoError(t, err)
	_, err = tx.Exec(
		`ALTER TABLE messages ADD COLUMN principal_id TEXT NOT NULL DEFAULT 'local'`,
	)
	require.NoError(t, err)
	require.NoError(t, stampUserVersionTx(tx, 12))
	require.NoError(t, tx.Commit())
}

// seedChannelAndMessage inserts one channel plus one PRE-v12 message using the
// same column shape the v11 sibling tests use (RFC 3339 timestamps, the
// NOT NULL companions), so a row survives the store's own scan path.
func seedChannelAndMessage(t *testing.T, db *sql.DB, msgID, content string) {
	t.Helper()
	_, err := db.Exec(
		`INSERT OR IGNORE INTO channels (id, name, channel_type, description, created_at, session_id, epoch_id, classification)
		   VALUES ('group:planning', 'planning', 'group', '', '2026-01-01T00:00:00Z', 'legacy', 'live', 'internal')`)
	require.NoError(t, err)
	_, err = db.Exec(
		`INSERT INTO messages (id, channel_id, sender_id, content, timestamp, mentions, metadata, session_id, epoch_id)
		   VALUES (?, 'group:planning', 'alice-person', ?, '2026-01-01T00:00:00Z', '[]', '{}', 'legacy', 'live')`,
		msgID, content)
	require.NoError(t, err)
}

// seedStampedMessage inserts a POST-v12 message that names its principal —
// the row a v12 writer produces.
func seedStampedMessage(t *testing.T, db *sql.DB, msgID, content, principal string) {
	t.Helper()
	_, err := db.Exec(
		`INSERT INTO messages (id, channel_id, sender_id, content, timestamp, mentions, metadata, session_id, epoch_id, principal_id)
		   VALUES (?, 'group:planning', 'alice-person', ?, '2026-01-02T00:00:00Z', '[]', '{}', 'legacy', 'live', ?)`,
		msgID, content, principal)
	require.NoError(t, err)
}

// TestSQLiteStore_SchemaV13_Migration_Idempotent pins the literal latest
// version (the newest migration's test owns the literal pin, per the v5/v6
// test-header convention) and that a reopen is a no-op.
func TestSQLiteStore_SchemaV13_Migration_Idempotent(t *testing.T) {
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
		assert.Equal(t, 13, version,
			"channelStoreSchemaVersion is v13 as of the ISSUE-0130 (b) backfill repair")
		assert.Equal(t, channelStoreSchemaVersion, version,
			"the literal pin and the const must agree")
	})
}

// TestMigrateV12ToV13_RepairsTheOriginalLocalBackfill is the whole reason v13
// exists. A store that ran the ORIGINAL v12 is already at user_version 12, so
// the corrected migration can never reach it: every pre-v12 row still reads as
// a PRESENT `local`, which PR B2's consumer treats as attribution and derives
// persona memory under — the ISSUE-0130 leak, which the shape-(b)
// re-derivation guard would then make permanent by storing the digest.
func TestMigrateV12ToV13_RepairsTheOriginalLocalBackfill(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	db := migrateThroughV11(t, path)
	t.Cleanup(func() { _ = db.Close() })

	seedChannelAndMessage(t, db, "msg-pre-v12", "said before the column existed")

	migrateV11ToV12Original(t, db)
	require.Equal(t, DefaultPrincipalID, messagePrincipal(t, db, "msg-pre-v12"),
		"precondition: the original migration backfilled `local`")

	needsRepair, err := v12PrincipalBackfilledLocal(db)
	require.NoError(t, err)
	assert.True(t, needsRepair,
		"the recorded column DEFAULT is what identifies an affected store")

	require.NoError(t, migrateV12ToV13(db))

	assert.Equal(t, "", messagePrincipal(t, db, "msg-pre-v12"),
		"a row that predates the column must read as ABSENT, not as the real answer `local`")

	var version int
	require.NoError(t, db.QueryRow(`PRAGMA user_version`).Scan(&version))
	assert.Equal(t, 13, version, "the repair stamps its own version")
}

// TestMigrateV12ToV13_IsANoOpOnACorrectlyMigratedStore pins the detector from
// the other side: a store whose v12 wrote `”` is not rewritten, so v13 costs
// nothing on the path every fresh install takes.
func TestMigrateV12ToV13_IsANoOpOnACorrectlyMigratedStore(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	db := migrateThroughV11(t, path)
	t.Cleanup(func() { _ = db.Close() })

	seedChannelAndMessage(t, db, "msg-pre-v12", "older than the column")

	require.NoError(t, migrateV11ToV12(db))
	// A row a v12 WRITER stamped `local` on: a real answer, and the one this
	// migration cannot tell apart from the bad backfill on an affected store.
	seedStampedMessage(t, db, "msg-post-v12", "unauthenticated publish", DefaultPrincipalID)

	needsRepair, err := v12PrincipalBackfilledLocal(db)
	require.NoError(t, err)
	assert.False(t, needsRepair, "a store migrated by the corrected v12 is not affected")

	require.NoError(t, migrateV12ToV13(db))

	assert.Equal(t, "", messagePrincipal(t, db, "msg-pre-v12"),
		"the correctly-backfilled row is unchanged")
	assert.Equal(t, DefaultPrincipalID, messagePrincipal(t, db, "msg-post-v12"),
		"a genuinely-stamped `local` SURVIVES on an unaffected store — v13 must not "+
			"un-attribute real answers it was never asked to repair")
}

// TestMigrateV12ToV13_OverCorrectsOnlyOnAffectedStores states the accepted
// cost in a test rather than only in a comment: on a store that took the bad
// backfill, a genuinely-stamped `local` is indistinguishable from a
// backfilled one, so it is rewritten too. Conservative by design — the row
// becomes unattributable and its replayed span is skipped, against the leak
// on the other side.
func TestMigrateV12ToV13_OverCorrectsOnlyOnAffectedStores(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	db := migrateThroughV11(t, path)
	t.Cleanup(func() { _ = db.Close() })

	seedChannelAndMessage(t, db, "msg-pre-v12", "older than the column")

	migrateV11ToV12Original(t, db)
	seedStampedMessage(t, db, "msg-real-local", "unauthenticated publish", DefaultPrincipalID)
	seedStampedMessage(t, db, "msg-alice", "authenticated publish", "alice-person")

	require.NoError(t, migrateV12ToV13(db))

	assert.Equal(t, "", messagePrincipal(t, db, "msg-real-local"),
		"accepted over-correction: a real `local` on an affected store is rewritten too")
	assert.Equal(t, "alice-person", messagePrincipal(t, db, "msg-alice"),
		"a NAMED tenant is never touched — the repair only ever rewrites `local`")
}

// TestMigrateV12ToV13_SurvivesAMissingMessagesTable pins the partial-baseline
// tolerance every sibling handler has: no `messages` table means nothing to
// repair, not a failed boot.
func TestMigrateV12ToV13_SurvivesAMissingMessagesTable(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	db, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	t.Cleanup(func() { _ = db.Close() })

	needsRepair, err := v12PrincipalBackfilledLocal(db)
	require.NoError(t, err)
	assert.False(t, needsRepair)
	require.NoError(t, migrateV12ToV13(db))
}

// TestSQLiteStore_V12Store_RepairsOnOpen is the end-to-end shape an operator
// actually hits: a store left at v12 by the original code comes back at v13
// with its rows un-attributed on the NEXT open, through the real store
// constructor rather than a direct migration call.
func TestSQLiteStore_V12Store_RepairsOnOpen(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	db := migrateThroughV11(t, path)

	seedChannelAndMessage(t, db, "msg-pre-v12", "said before the column existed")
	migrateV11ToV12Original(t, db)
	require.NoError(t, db.Close())

	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	msg, err := store.GetMessage(context.Background(), "msg-pre-v12")
	require.NoError(t, err)
	assert.Equal(t, "said before the column existed", msg.Content, "no data loss")
	assert.Equal(t, "", msg.PrincipalID,
		"opening a v12 store repairs the backfill the corrected v12 can never reach")
}
