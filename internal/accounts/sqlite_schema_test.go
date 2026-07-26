package accounts

import (
	"database/sql"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// openRaw opens the database file without the store, for seeding and
// inspecting schema state around [Open].
func openRaw(t *testing.T, path string) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	db.SetMaxOpenConns(1)
	t.Cleanup(func() { _ = db.Close() })
	return db
}

func userVersion(t *testing.T, db *sql.DB) int {
	t.Helper()
	var v int
	require.NoError(t, db.QueryRow(`PRAGMA user_version;`).Scan(&v))
	return v
}

func tableNames(t *testing.T, db *sql.DB) map[string]bool {
	t.Helper()
	rows, err := db.Query(`SELECT name FROM sqlite_master WHERE type IN ('table','index') AND name NOT LIKE 'sqlite_%'`)
	require.NoError(t, err)
	defer rows.Close()
	out := map[string]bool{}
	for rows.Next() {
		var name string
		require.NoError(t, rows.Scan(&name))
		out[name] = true
	}
	require.NoError(t, rows.Err())
	return out
}

func TestOpen_FreshDatabase_LandsAtV1WithBaselineSchema(t *testing.T) {
	path := filepath.Join(t.TempDir(), "accounts.db")
	s, err := Open(path)
	require.NoError(t, err)
	require.NoError(t, s.Close())

	db := openRaw(t, path)
	assert.Equal(t, accountStoreSchemaVersion, userVersion(t, db),
		"the v1 baseline must stamp user_version inside its own migration")
	names := tableNames(t, db)
	assert.True(t, names["accounts"])
	assert.True(t, names["sessions"])
	assert.True(t, names["idx_sessions_account"])
}

func TestOpen_Reopen_IsIdempotent(t *testing.T) {
	path := filepath.Join(t.TempDir(), "accounts.db")
	s, err := Open(path)
	require.NoError(t, err)
	require.NoError(t, s.Close())

	before := tableNames(t, openRaw(t, path))

	s, err = Open(path)
	require.NoError(t, err, "reopening an up-to-date database must be a no-op")
	require.NoError(t, s.Close())

	db := openRaw(t, path)
	assert.Equal(t, accountStoreSchemaVersion, userVersion(t, db))
	assert.Equal(t, before, tableNames(t, db), "reopen must not add, drop, or resurrect schema objects")
}

func TestOpen_NewerDatabase_IsRejected(t *testing.T) {
	path := filepath.Join(t.TempDir(), "accounts.db")
	db := openRaw(t, path)
	_, err := db.Exec(`PRAGMA user_version = 99`)
	require.NoError(t, err)
	require.NoError(t, db.Close())

	_, err = Open(path)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "downgrade unsupported")
}

// TestMigrationV1_FailureRollsBackAtomically pins the stamp-in-transaction
// discipline at the v1 boundary (the channels PR #335 L3 property): a
// migration that fails partway must leave neither its DDL nor its
// user_version stamp behind. Seeding a bare `sessions` table (unstamped)
// makes migrateV0ToV1's second CREATE fail after the first succeeded —
// if the accounts table or a stamped version survives, the migration is
// not atomic.
func TestMigrationV1_FailureRollsBackAtomically(t *testing.T) {
	path := filepath.Join(t.TempDir(), "accounts.db")
	db := openRaw(t, path)
	_, err := db.Exec(`CREATE TABLE sessions (dummy TEXT)`)
	require.NoError(t, err)
	require.NoError(t, db.Close())

	_, err = Open(path)
	require.Error(t, err, "the v1 migration must fail against the conflicting table")

	after := openRaw(t, path)
	assert.Equal(t, 0, userVersion(t, after),
		"a failed migration must not leave a user_version stamp")
	assert.False(t, tableNames(t, after)["accounts"],
		"the failed migration's earlier DDL must roll back with it")
}

func TestOpen_ForeignKeys_AreEnforced(t *testing.T) {
	path := filepath.Join(t.TempDir(), "accounts.db")
	s, err := Open(path)
	require.NoError(t, err)
	defer s.Close()

	_, err = s.db.Exec(`
		INSERT INTO sessions (token_hash, account_id, issued_at, expires_at, last_used_at)
		VALUES ('deadbeef', 'no-such-account', 0, 0, 0)`)
	require.Error(t, err, "sessions.account_id must be enforced, not decorative (RFC 0039 §B)")
	assert.Contains(t, err.Error(), "FOREIGN KEY")
}
