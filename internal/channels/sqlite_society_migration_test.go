package channels

// Channels-store v14 → v15: the society tables (RFC 0029 Phase 3 plan PR 2).
//
// Pinned here: a populated v14 store opens at v15 with every society table and
// index, empty but for the graph-version row, and nothing else in the store
// changed; a step that cannot finish leaves the store at v14; and a store
// newer than its binary is refused. The tables' constraints are pinned in
// sqlite_society_constraints_test.go; the society store's calls on them in
// internal/society.

import (
	"database/sql"
	"fmt"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// societyTables is every table v15 adds, sorted.
var societyTables = []string{
	"society_approvals", "society_keys", "society_manages", "society_members",
	"society_meta", "society_organizations", "society_roles",
}

// societyIndexes is every index v15 adds, by table, each with its shape as
// [indexShapes] reads it: its columns in order, and a partial index's WHERE.
var societyIndexes = map[string]map[string]string{
	"society_approvals": {
		"idx_society_approvals_actor":        "actor_id, created_at",
		"idx_society_approvals_state_expiry": "state, expires_at",
		"idx_society_approvals_tie_group":    "tie_group WHERE tie_group IS NOT NULL",
		"ux_society_approvals_open":          "actor_id, action_hash WHERE state IN ('pending', 'approved')",
	},
	"society_manages": {"idx_society_manages_report": "organization_id, report_id"},
	"society_members": {
		"idx_society_members_agent": "agent_id",
		"idx_society_members_role":  "organization_id, role_id",
	},
}

// migrateThroughV14 hand-builds a v14 store at `path`, as PR 1 left it, so the
// next open runs exactly the v14 → v15 step.
func migrateThroughV14(t *testing.T, path string) *sql.DB {
	t.Helper()
	db := migrateThroughV13(t, path)
	require.NoError(t, migrateV13ToV14(db))
	return db
}

// newSocietyDB opens a fresh store at the latest version and returns its own
// connection, the one the society tables share (plan S-2).
func newSocietyDB(t *testing.T) *sql.DB {
	t.Helper()
	store, err := NewSQLiteStore(filepath.Join(t.TempDir(), "channels.db"), SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	return store.(*sqliteStore).db
}

// schemaObjects lists the store's tables, indexes and triggers with their SQL,
// leaving out the society tables with their indexes, and SQLite's own.
func schemaObjects(t *testing.T, db *sql.DB) []string {
	t.Helper()
	rows, err := db.Query(
		`SELECT type, name, COALESCE(sql, '') FROM sqlite_master
		 WHERE tbl_name NOT LIKE 'society\_%' ESCAPE '\' AND name NOT LIKE 'sqlite\_%' ESCAPE '\'
		 ORDER BY type, name`)
	require.NoError(t, err)
	defer func() { _ = rows.Close() }()
	var objects []string
	for rows.Next() {
		var kind, name, ddl string
		require.NoError(t, rows.Scan(&kind, &name, &ddl))
		objects = append(objects, kind+" "+name+": "+ddl)
	}
	require.NoError(t, rows.Err())
	return objects
}

// societyTableNames lists the society tables a store holds, sorted.
func societyTableNames(t *testing.T, db *sql.DB) []string {
	t.Helper()
	rows, err := db.Query(
		`SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'society\_%' ESCAPE '\' ORDER BY name`)
	require.NoError(t, err)
	defer func() { _ = rows.Close() }()
	var names []string
	for rows.Next() {
		var name string
		require.NoError(t, rows.Scan(&name))
		names = append(names, name)
	}
	require.NoError(t, rows.Err())
	return names
}

// indexShapes maps each index on `table` (SQLite's own left out) to its
// columns in order, each marked DESC when it sorts that way, followed by its
// WHERE when it is partial; nil when the table has none. So an index rebuilt
// on other columns, or with another condition, fails the pin, not only one
// that is dropped or renamed.
func indexShapes(t *testing.T, db *sql.DB, table string) map[string]string {
	t.Helper()
	var shapes map[string]string
	for _, name := range tableIndexes(t, db, table) {
		columns := func() []string {
			rows, err := db.Query(
				`SELECT name, "desc" FROM pragma_index_xinfo(?) WHERE key = 1 ORDER BY seqno`, name)
			require.NoError(t, err)
			defer func() { _ = rows.Close() }()
			var columns []string
			for rows.Next() {
				var column string
				var desc int
				require.NoError(t, rows.Scan(&column, &desc))
				if desc == 1 {
					column += " DESC"
				}
				columns = append(columns, column)
			}
			require.NoError(t, rows.Err())
			return columns
		}()
		var ddl string
		require.NoError(t, db.QueryRow(
			`SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?`, name).Scan(&ddl))
		shape := strings.Join(columns, ", ")
		if _, where, partial := strings.Cut(strings.Join(strings.Fields(ddl), " "), " WHERE "); partial {
			shape += " WHERE " + where
		}
		if shapes == nil {
			shapes = map[string]string{}
		}
		shapes[name] = shape
	}
	return shapes
}

// TestSQLiteStore_Migration_V14ToV15_AddsTheSocietyTablesToAPopulatedStore is
// the upgrade an operator runs: a v14 store with history opens at v15 with
// the society tables beside the channel tables, and nothing the store held
// before has changed.
func TestSQLiteStore_Migration_V14ToV15_AddsTheSocietyTablesToAPopulatedStore(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	db := migrateThroughV14(t, path)
	seedChannelHistory(t, db, 2, 50)
	require.Empty(t, societyTableNames(t, db), "precondition: a v14 store has no society table")
	before := schemaObjects(t, db)
	require.NoError(t, db.Close())

	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	sdb := store.(*sqliteStore).db

	assert.Equal(t, channelStoreSchemaVersion, readUserVersion(t, sdb), "the open runs v15 and every later step")
	assert.Equal(t, societyTables, societyTableNames(t, sdb))
	assert.Equal(t, before, schemaObjects(t, sdb), "v15 adds tables and changes nothing else")
	for _, table := range societyTables {
		assert.Equal(t, societyIndexes[table], indexShapes(t, sdb, table), "%s's indexes", table)
	}

	for _, table := range societyTables {
		var count int
		require.NoError(t, sdb.QueryRow(`SELECT COUNT(*) FROM `+table).Scan(&count))
		want := 0
		if table == "society_meta" {
			want = 1
		}
		assert.Equal(t, want, count, "%s starts empty but for the graph-version row", table)
	}
	var version int64
	require.NoError(t, sdb.QueryRow(`SELECT graph_version FROM society_meta WHERE id = 1`).Scan(&version))
	assert.Zero(t, version, "no graph has been loaded")

	var messages int
	require.NoError(t, sdb.QueryRow(`SELECT COUNT(*) FROM messages`).Scan(&messages))
	assert.Equal(t, 100, messages, "no row lost")
	var integrity string
	require.NoError(t, sdb.QueryRow(`PRAGMA integrity_check`).Scan(&integrity))
	assert.Equal(t, "ok", integrity)
	var fkViolations int
	require.NoError(t, sdb.QueryRow(`SELECT COUNT(*) FROM pragma_foreign_key_check`).Scan(&fkViolations))
	assert.Zero(t, fkViolations)
}

// TestMigrateV14ToV15_StampsUserVersionInTransaction is the single-step pin
// every migration carries (PR #335 review L3): the step leaves user_version
// at 15 and the tables in place, together.
func TestMigrateV14ToV15_StampsUserVersionInTransaction(t *testing.T) {
	db := migrateThroughV14(t, filepath.Join(t.TempDir(), "channels.db"))
	t.Cleanup(func() { _ = db.Close() })

	require.NoError(t, migrateV14ToV15(db))

	assert.Equal(t, 15, readUserVersion(t, db))
	assert.Equal(t, societyTables, societyTableNames(t, db))
}

// TestSQLiteStore_Migration_V14ToV15_LeavesAStoreItCannotFinishAtV14: the step
// is one transaction, so a store that already holds a table of one of its
// names (here the last one it creates) fails the open and keeps v14, with
// none of the step's other tables.
func TestSQLiteStore_Migration_V14ToV15_LeavesAStoreItCannotFinishAtV14(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	db := migrateThroughV14(t, path)
	_, err := db.Exec(`CREATE TABLE society_keys (purpose TEXT)`)
	require.NoError(t, err)
	require.NoError(t, db.Close())

	_, err = NewSQLiteStore(path, SQLiteOptions{})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "migrate to v15")

	withDB(t, path, func(db *sql.DB) {
		assert.Equal(t, 14, readUserVersion(t, db))
		assert.Equal(t, []string{"society_keys"}, societyTableNames(t, db),
			"the step's other tables rolled back with it")
	})
}

// TestSQLiteStore_SchemaV15_Migration_Idempotent pins the literal latest
// version (the newest migration's test owns the literal pin) and that a
// reopen is a no-op: the graph-version row is seeded once, not per open.
func TestSQLiteStore_SchemaV15_Migration_Idempotent(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")

	store1, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	require.NoError(t, store1.Close())

	store2, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store2.Close() })

	withDB(t, path, func(db *sql.DB) {
		version := readUserVersion(t, db)
		assert.Equal(t, 15, version,
			"channelStoreSchemaVersion is v15 as of the society tables (RFC 0029 Phase 3)")
		assert.Equal(t, channelStoreSchemaVersion, version,
			"the literal pin and the const must agree")
		var rows int
		require.NoError(t, db.QueryRow(`SELECT COUNT(*) FROM society_meta`).Scan(&rows))
		assert.Equal(t, 1, rows, "the reopen seeds no second graph-version row")
	})
}

// TestSQLiteStore_Open_RefusesAStoreNewerThanItsBinary is what a v14 binary
// does with a v15 store: it refuses to open a store one version newer than
// it knows, and leaves the store as it found it, society tables included, so
// rolling a binary back never runs it on tables it cannot read.
func TestSQLiteStore_Open_RefusesAStoreNewerThanItsBinary(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	require.NoError(t, store.Close())
	newer := channelStoreSchemaVersion + 1
	withDB(t, path, func(db *sql.DB) {
		_, err := db.Exec(fmt.Sprintf(`PRAGMA user_version = %d`, newer))
		require.NoError(t, err)
	})

	_, err = NewSQLiteStore(path, SQLiteOptions{})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "downgrade unsupported")

	withDB(t, path, func(db *sql.DB) {
		assert.Equal(t, newer, readUserVersion(t, db), "the refused store keeps its version")
		assert.Equal(t, societyTables, societyTableNames(t, db), "and its society tables")
	})
}
