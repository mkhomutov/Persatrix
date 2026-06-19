package channels

// RFC 0036 PR 1 — the channel-store v9→v10 migration that lands the
// `messages_fts` FTS5 index over `messages.content`. Split into its own file
// (mirroring sqlite_membership_intervals_migration.go) because
// sqlite_migrations.go is at the repo's 500-line cap; the `case 10:` arm that
// dispatches to migrateV9ToV10 stays in that file's applyMigration runner.

import (
	"database/sql"
	"fmt"
)

// FTS5 DDL for the verbatim-recall index. External-content table over
// `messages.content` (mirroring the episodic tier's `episodes_fts`), kept in
// sync by three triggers. RFC 0036 PR 2's scoped search MATCHes against it.
const (
	// `content=messages, content_rowid=rowid` makes this an EXTERNAL-CONTENT
	// table: it stores only the inverted index, reading column values back from
	// `messages` by rowid. `messages.id` is a TEXT PRIMARY KEY, so the table
	// keeps SQLite's implicit integer rowid and `content_rowid=rowid` aliases
	// it. CAVEAT: that rowid is NOT preserved across an explicit `VACUUM`; no
	// channel-store code path runs `VACUUM` today, so the risk is latent — but
	// any future compaction MUST be followed by an `INSERT INTO
	// messages_fts(messages_fts) VALUES ('rebuild')`.
	messagesFTSTableDDL = `CREATE VIRTUAL TABLE messages_fts USING fts5(
        content,
        content=messages,
        content_rowid=rowid
    )`

	// messages_ai mirrors every INSERT into the index.
	messagesFTSInsertTriggerDDL = `CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
    END`

	// messages_ad removes a row from the index on hard DELETE — load-bearing
	// for the RFC 0036 §H retention story: a cap-pruned or operator-deleted
	// message must disappear from recall, not linger in the index.
	messagesFTSDeleteTriggerDDL = `CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
    END`

	// messages_au is DEFENSIVE-SYMMETRIC: `messages` is insert-and-cap-prune
	// only (no production path UPDATEs a row's content), so this trigger is
	// expected never to fire. It ships for index correctness if that ever
	// changes, matching the episodic tier's `episodes_au`.
	messagesFTSUpdateTriggerDDL = `CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
        INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
    END`

	// External-content tables start empty; the `('rebuild')` backfill is
	// mandatory, populating the index from the existing `messages` rows.
	messagesFTSRebuildDDL = `INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')`
)

// migrateV9ToV10 lands the RFC 0036 `messages_fts` FTS5 index — the verbatim
// search surface over `messages.content` that PR 2's scoped recall query joins
// against the RFC 0035 `membership_intervals` ledger. See the
// [channelStoreSchemaVersion] v10 history note for the full rationale.
// Forward-only and a pure addition: one virtual table + three triggers + a
// rebuild from `messages`, no existing table / row / index touched, so every
// `channels` / `messages` query reads back byte-identically post-v10.
//
// FTS5 availability is probed once here and the decision delegated to
// [applyMessagesFTSMigration]; when FTS5 is absent the migration still advances
// to v10 (no virtual table), and PR 2's query detects the missing table to take
// its LIKE fallback — the same graceful degradation the episodic tier ships.
func migrateV9ToV10(db *sql.DB) error {
	return applyMessagesFTSMigration(db, fts5Available(db))
}

// applyMessagesFTSMigration performs the v9→v10 step. `ftsAvailable` is taken
// as a parameter (rather than probed here) so a test can exercise the
// FTS5-unavailable degradation path deterministically even on a build where
// FTS5 is present.
//
// The whole migration runs in one transaction and stamps `user_version` inside
// it (PR #335 review L3) so the schema change and its version bookkeeping commit
// (or roll back) atomically.
func applyMessagesFTSMigration(db *sql.DB, ftsAvailable bool) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	if ftsAvailable {
		stmts := []string{
			messagesFTSTableDDL,
			messagesFTSInsertTriggerDDL,
			messagesFTSDeleteTriggerDDL,
			messagesFTSUpdateTriggerDDL,
			messagesFTSRebuildDDL, // last: index exists + triggers armed before backfill
		}
		for _, q := range stmts {
			if _, err := tx.Exec(q); err != nil {
				return fmt.Errorf("exec %q: %w", firstLine(q), err)
			}
		}
	}
	// FTS5 unavailable: `messages_fts` is simply absent. RFC 0036 PR 2's scoped
	// search detects the missing table and takes its LIKE fallback; the version
	// line still advances so it stays monotonic across builds.

	if err := stampUserVersionTx(tx, 10); err != nil {
		return err
	}
	return tx.Commit()
}

// fts5Available reports whether this SQLite build supports FTS5, probed with a
// throwaway virtual table (the Go-store sibling of the episodic tier's
// `_fts5_available`). modernc.org/sqlite ships FTS5 compiled in, so this returns
// true in every supported build; the probe exists so the channel-store open
// degrades gracefully (skipping `messages_fts`) on a hypothetical FTS5-less
// build rather than failing outright. The probe runs outside the migration
// transaction; the create-then-drop nets to nothing and `IF NOT EXISTS` keeps a
// re-probe idempotent.
func fts5Available(db *sql.DB) bool {
	if _, err := db.Exec(`CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(probe)`); err != nil {
		return false
	}
	_, _ = db.Exec(`DROP TABLE IF EXISTS _fts5_probe`)
	return true
}
