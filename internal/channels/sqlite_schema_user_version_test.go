// RFC 0031 PR #335 review L3 — schema-version stamp atomicity.
//
// Background. Before this commit, `applySchema` ran each migration step in
// its own transaction and then, *outside* that loop, executed a single
// `PRAGMA user_version = <latest>;` statement to record where the database
// had landed. That created a narrow but real hazard window:
//
//  1. Migration v(N-1)→v(N) commits its tx OK.
//  2. The post-loop `PRAGMA user_version = N` statement fails for some
//     transient reason — exclusive lock contention from a concurrent
//     helper process, I/O error, driver hiccup.
//  3. `applySchema` returns the stamp error. The schema is at vN but
//     `user_version` is still whatever it was on entry (commonly 0 for
//     a fresh DB, or N-1 for a pre-existing one).
//  4. Next boot reads the stale `user_version` and re-runs every step
//     from there. For the v1→v2 step specifically, that means rebuilding
//     `channels` from `channels_v2` by copying only the five v1 columns
//     — silently dropping any `session_id` data v2→v3 had since added.
//
// Phase 1 blast radius is small (today every row carries
// `session_id='legacy'`; PR 3 introduces the first divergent writers).
// But the migration in this PR is what makes the column meaningful at
// all, so the safety property worth landing now is: each migration's
// schema changes and its version stamp commit (or roll back) atomically.
//
// These two tests pin that property at the single-step boundary. They
// drive `migrateV1ToV2` and `migrateV2ToV3` directly (white-box, same
// package) so a future refactor that lifts the stamp back to a post-loop
// step, or a new migration that forgets to stamp inside its tx, surfaces
// as a clean failure rather than a silent re-migration on next boot.
//
// Companion file: `sqlite_schema.go` — header `applySchema` block.
package channels

import (
	"database/sql"
	"path/filepath"
	"testing"

	_ "modernc.org/sqlite"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// rawSchemaDB opens a `modernc.org/sqlite` handle at a temp path using
// the same DSN the production store uses (via [buildDSN]) so PRAGMA
// behaviour matches. Tests in this file deliberately bypass
// `NewSQLiteStore` because that constructor calls `applySchema`, which
// runs every migration up to the latest version in one go — useful for
// integration-shaped tests, useless for pinning a single step.
func rawSchemaDB(t *testing.T) (*sql.DB, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "schema_uv.db")
	db, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	t.Cleanup(func() { _ = db.Close() })
	return db, path
}

// readUserVersion reads `PRAGMA user_version` and fails the test on a
// driver error. `PRAGMA user_version` accepts no placeholders so callers
// who *write* the version use `fmt.Sprintf` — this read path is constant.
func readUserVersion(t *testing.T, db *sql.DB) int {
	t.Helper()
	var v int
	require.NoError(t, db.QueryRow(`PRAGMA user_version`).Scan(&v))
	return v
}

// TestMigrateV1ToV2_StampsUserVersionInTransaction asserts that the
// v1→v2 migration sets `PRAGMA user_version = 2` as part of its own
// transaction. The setup mirrors a real first-time boot at the v1
// baseline (PR #231 era): fresh file, `schemaV1SQL` applied, no
// version stamp yet. Calling `migrateV1ToV2` directly must leave the
// database both at the v2 shape AND carrying `user_version = 2` so a
// subsequent crash before the next migration step cannot un-stamp the
// version. See file header for the underlying hazard.
func TestMigrateV1ToV2_StampsUserVersionInTransaction(t *testing.T) {
	db, _ := rawSchemaDB(t)

	_, err := db.Exec(schemaV1SQL)
	require.NoError(t, err, "apply v1 baseline")
	require.Equal(t, 0, readUserVersion(t, db),
		"precondition: schemaV1SQL leaves user_version=0 (applySchema treats this as v1)")

	require.NoError(t, migrateV1ToV2(db))

	assert.Equal(t, 2, readUserVersion(t, db),
		"migrateV1ToV2 must stamp user_version=2 inside its own tx — a post-loop stamp leaves a window where a successful schema commit but a failed stamp causes the next boot to re-run v1→v2 on a newer schema, silently dropping session_id data")
}

// TestMigrateV2ToV3_StampsUserVersionInTransaction asserts the same
// atomicity property for the v2→v3 step. This is the migration this PR
// actually introduces, so the hazard is most acute here: re-running
// v2→v3 is itself idempotent (ALTER ADD COLUMN ... IF NOT EXISTS-style
// guards in SQLite ≥3.20 plus the literal-default contract), but the
// scenario L3 pins is *re-running v1→v2 on a v3-shape table*, which
// happens precisely when v2→v3 has committed and the post-loop stamp
// fails. Pinning v3's stamp inside its tx ensures `user_version=3`
// lands atomically with the schema changes so that scenario is
// unreachable.
func TestMigrateV2ToV3_StampsUserVersionInTransaction(t *testing.T) {
	db, _ := rawSchemaDB(t)

	_, err := db.Exec(schemaV1SQL)
	require.NoError(t, err, "apply v1 baseline")
	require.NoError(t, migrateV1ToV2(db), "advance to v2")
	require.Equal(t, 2, readUserVersion(t, db),
		"precondition: at v2 with user_version=2 before exercising v2→v3")

	require.NoError(t, migrateV2ToV3(db))

	assert.Equal(t, 3, readUserVersion(t, db),
		"migrateV2ToV3 must stamp user_version=3 inside its own tx")
}

// TestMigrateV3ToV4_StampsUserVersionInTransaction asserts the same
// atomicity property for the v3→v4 step (ISSUE-0082 PR 1 —
// `session_bindings`). The migration is a single `CREATE TABLE`, so a
// re-run would fail with "table already exists" rather than corrupt data;
// pinning the stamp inside the tx keeps the next boot from attempting that
// re-run at all, consistent with the v1→v2 / v2→v3 discipline.
func TestMigrateV3ToV4_StampsUserVersionInTransaction(t *testing.T) {
	db, _ := rawSchemaDB(t)

	_, err := db.Exec(schemaV1SQL)
	require.NoError(t, err, "apply v1 baseline")
	require.NoError(t, migrateV1ToV2(db), "advance to v2")
	require.NoError(t, migrateV2ToV3(db), "advance to v3")
	require.Equal(t, 3, readUserVersion(t, db),
		"precondition: at v3 with user_version=3 before exercising v3→v4")

	require.NoError(t, migrateV3ToV4(db))

	assert.Equal(t, 4, readUserVersion(t, db),
		"migrateV3ToV4 must stamp user_version=4 inside its own tx")
}

// TestMigrateV4ToV5_StampsUserVersionInTransaction asserts the same
// atomicity property for the v4→v5 step (ISSUE-0083 — sender-axis drop). The
// migration rebuilds `session_bindings` (CREATE new / INSERT collapse / DROP
// old / RENAME); a re-run on the already-rebuilt pair table would fail (the
// old table is gone), so pinning the stamp inside the tx keeps the next boot
// from attempting that re-run at all, consistent with the v1→v2 … v3→v4
// discipline.
func TestMigrateV4ToV5_StampsUserVersionInTransaction(t *testing.T) {
	db, _ := rawSchemaDB(t)

	_, err := db.Exec(schemaV1SQL)
	require.NoError(t, err, "apply v1 baseline")
	require.NoError(t, migrateV1ToV2(db), "advance to v2")
	require.NoError(t, migrateV2ToV3(db), "advance to v3")
	require.NoError(t, migrateV3ToV4(db), "advance to v4")
	require.Equal(t, 4, readUserVersion(t, db),
		"precondition: at v4 with user_version=4 before exercising v4→v5")

	require.NoError(t, migrateV4ToV5(db))

	assert.Equal(t, 5, readUserVersion(t, db),
		"migrateV4ToV5 must stamp user_version=5 inside its own tx")
}

// TestMigrateV5ToV6_StampsUserVersionInTransaction asserts the same
// atomicity property for the v5→v6 step (ISSUE-0085 PR 2 — epoch_id columns).
// The migration is additive (ADD COLUMN + CREATE INDEX); a re-run would fail
// with "duplicate column name" / "index already exists" rather than corrupt
// data, so pinning the stamp inside the tx keeps the next boot from
// attempting that re-run at all, consistent with the v1→v2 … v4→v5
// discipline.
func TestMigrateV5ToV6_StampsUserVersionInTransaction(t *testing.T) {
	db, _ := rawSchemaDB(t)

	_, err := db.Exec(schemaV1SQL)
	require.NoError(t, err, "apply v1 baseline")
	require.NoError(t, migrateV1ToV2(db), "advance to v2")
	require.NoError(t, migrateV2ToV3(db), "advance to v3")
	require.NoError(t, migrateV3ToV4(db), "advance to v4")
	require.NoError(t, migrateV4ToV5(db), "advance to v5")
	require.Equal(t, 5, readUserVersion(t, db),
		"precondition: at v5 with user_version=5 before exercising v5→v6")

	require.NoError(t, migrateV5ToV6(db))

	assert.Equal(t, 6, readUserVersion(t, db),
		"migrateV5ToV6 must stamp user_version=6 inside its own tx")
}

// TestApplySchema_FreshDB_StampsLatestVersion is the integration-shaped
// counterpart to the two single-step tests above. It is intentionally
// duplicative with `TestSQLiteStore_SchemaV3_Migration_Idempotent` (which
// asserts the same property on the second boot); the value here is the
// *first-boot* pin so a future refactor of `applySchema` cannot regress
// the contract without breaking both halves of the matrix.
func TestApplySchema_FreshDB_StampsLatestVersion(t *testing.T) {
	db, _ := rawSchemaDB(t)

	require.NoError(t, applySchema(db))

	assert.Equal(t, channelStoreSchemaVersion, readUserVersion(t, db),
		"applySchema on a fresh DB must leave user_version at the latest schema version")
}
