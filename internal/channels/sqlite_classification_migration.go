// RFC 0037 PR 1 (v0.3.12) — the `channels.classification` column migration
// (channelStoreSchemaVersion v10 → v11). Lives in its own sibling file per the
// sqlite_migrations.go header convention (that file is near the 500-line cap;
// v8→v9 and v9→v10 set the per-migration-file precedent).
package channels

import (
	"database/sql"
	"fmt"
)

// migrateV10ToV11 lands the RFC 0037 §B channel classification column on
// `channels`. Forward-only and a pure addition — one additive, defaulted
// column, no index or table rebuild, every existing index left intact:
//
//   - `classification TEXT NOT NULL DEFAULT 'internal'` is the channel's §A
//     confidentiality lattice level (`public` | `internal` | `restricted` |
//     `secret`). The `'internal'` DEFAULT doubles as the backfill and is §A
//     rule (a) applied to every pre-v11 row: a channel that predates
//     classification (or a future INSERT that omits the column) is
//     confidential-by-default — labeled `internal`, never `public`.
//
// The literal must stay in lock-step with [DefaultClassification]
// (classification.go); it is spelled out here because migration SQL is
// frozen history — a future rename of the constant must not silently rewrite
// what v11 backfilled.
//
// Writers in this PR: [sqliteStore.GetOrCreateDM] stamps the
// `dm_default_classification` knob at DM creation. Group channels take the
// DEFAULT until the declared `classification` is threaded through the create
// path with the wire lift (RFC 0037 PR 2). No reader exists until the §D
// hard gate / §F recall filter PRs — the column is dark substrate, so a
// v0.3.11 database behaves byte-identically after this migration.
//
// SQLite ≥3.20 supports `ADD COLUMN ... DEFAULT <constant>` without a
// backfill UPDATE, so the column lands in one statement. The migration runs
// in one transaction and stamps `user_version` inside it (PR #335 review L3)
// so the schema change and its version bookkeeping commit atomically.
func migrateV10ToV11(db *sql.DB) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	if _, err := tx.Exec(
		`ALTER TABLE channels ADD COLUMN classification TEXT NOT NULL DEFAULT 'internal'`,
	); err != nil {
		return fmt.Errorf("add channels.classification: %w", err)
	}
	if err := stampUserVersionTx(tx, 11); err != nil {
		return err
	}
	return tx.Commit()
}
