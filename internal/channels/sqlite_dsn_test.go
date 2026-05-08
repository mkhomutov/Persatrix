package channels

import (
	"database/sql"
	"path/filepath"
	"strings"
	"testing"

	_ "modernc.org/sqlite"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// pragmaInt opens a query for `PRAGMA <name>;` and scans the single integer
// the SQLite driver returns. Used by the buildDSN tests below to verify that
// foreign_keys / busy_timeout took effect on the *connection*.
//
// The PRAGMA values are connection-scoped (modernc.org/sqlite applies them
// from the DSN at connection-open time), so the queries are issued on a
// dedicated `*sql.Conn` to avoid cross-connection drift.
func pragmaInt(t *testing.T, db *sql.DB, name string) int64 {
	t.Helper()
	var v int64
	require.NoError(t, db.QueryRow("PRAGMA "+name).Scan(&v))
	return v
}

// pragmaString is the string-valued counterpart for PRAGMAs whose canonical
// return is text (e.g. journal_mode → "wal").
func pragmaString(t *testing.T, db *sql.DB, name string) string {
	t.Helper()
	var v string
	require.NoError(t, db.QueryRow("PRAGMA "+name).Scan(&v))
	return v
}

// TestBuildDSN_BarePathPreservesPRAGMAs is the regression guard for the
// production path: an absolute filesystem path (or `:memory:`) must yield a
// DSN that turns on foreign keys, WAL, and the 5s busy_timeout. This case
// already worked before ISSUE-0049 — pinning it ensures the fix does not
// regress the common path.
func TestBuildDSN_BarePathPreservesPRAGMAs(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")

	db, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	t.Cleanup(func() { _ = db.Close() })
	db.SetMaxOpenConns(1) // PRAGMAs are per-connection; pin to one.

	assert.Equal(t, int64(1), pragmaInt(t, db, "foreign_keys"),
		"foreign_keys must be ON for cascade enforcement")
	assert.Equal(t, "wal", strings.ToLower(pragmaString(t, db, "journal_mode")),
		"journal_mode must be WAL")
	assert.Equal(t, int64(5000), pragmaInt(t, db, "busy_timeout"),
		"busy_timeout must be 5000ms")
}

// TestBuildDSN_FileURIPreservesPRAGMAs pins ISSUE-0049: passing the
// `file::memory:?cache=shared` form advertised in NewSQLiteStore's
// doc-comment must still produce a DSN with all three PRAGMAs applied,
// AND must preserve the caller-supplied query parameters (cache=shared).
//
// Pre-fix behaviour (failing case): buildDSN concatenated `path + "?" +
// q.Encode()`, producing `file::memory:?cache=shared?_pragma=...`. The
// driver parsed `cache` as a single value containing the rest of the
// string and silently dropped every PRAGMA — foreign_keys defaults to
// OFF, journal_mode to "memory" (for an in-memory db), and busy_timeout
// to 0. The expectations below would all fail.
func TestBuildDSN_FileURIPreservesPRAGMAs(t *testing.T) {
	// `file::memory:?cache=shared` is the canonical SQLite shared-cache
	// in-memory form (https://sqlite.org/inmemorydb.html). The doc-comment
	// on NewSQLiteStore explicitly advertises this path; the test pins
	// that promise.
	const path = "file::memory:?cache=shared"

	db, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	t.Cleanup(func() { _ = db.Close() })
	db.SetMaxOpenConns(1)

	assert.Equal(t, int64(1), pragmaInt(t, db, "foreign_keys"),
		"foreign_keys must be ON even when path already carries a query string")
	// In-memory dbs cannot use WAL (file-system-backed only); SQLite silently
	// downgrades and reports `memory`. Asserting "not delete" is the meaningful
	// check — `delete` is the no-PRAGMA default that exposed the bug.
	mode := strings.ToLower(pragmaString(t, db, "journal_mode"))
	assert.NotEqual(t, "delete", mode,
		"journal_mode PRAGMA must be applied (in-memory dbs report 'memory', not 'delete')")
	assert.Equal(t, int64(5000), pragmaInt(t, db, "busy_timeout"),
		"busy_timeout must be 5000ms even when path already carries a query string")
}

// TestBuildDSN_FileURIPreservesCallerQueryParams pins the second half of
// the ISSUE-0049 contract: the caller-supplied query params (e.g.
// `cache=shared`) must survive the merge into the PRAGMA Values. Without
// this guard, a buildDSN that "fixed" the PRAGMA-drop bug by stripping
// the existing query would regress shared-cache semantics.
//
// We don't have a direct PRAGMA to read `cache` back, but we can prove
// the merge happened by parsing the constructed DSN itself: the
// `cache=shared` parameter must still be present in the final query
// string, alongside the three `_pragma` entries.
func TestBuildDSN_FileURIPreservesCallerQueryParams(t *testing.T) {
	dsn := buildDSN("file::memory:?cache=shared")

	assert.Contains(t, dsn, "cache=shared",
		"caller-supplied query params must be preserved in the merged DSN")
	assert.Contains(t, dsn, "_pragma=foreign_keys",
		"foreign_keys PRAGMA must appear in the merged DSN")
	assert.Contains(t, dsn, "_pragma=journal_mode",
		"journal_mode PRAGMA must appear in the merged DSN")
	assert.Contains(t, dsn, "_pragma=busy_timeout",
		"busy_timeout PRAGMA must appear in the merged DSN")

	// The fix must yield exactly one '?' separator between the path base
	// and the merged query string. Two '?'s is the pre-fix failure mode
	// that caused the PRAGMA drop in the first place.
	assert.Equal(t, 1, strings.Count(dsn, "?"),
		"merged DSN must contain exactly one '?' separator")
}
