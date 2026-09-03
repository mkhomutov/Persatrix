// ISSUE-0130 shape (b) — v0.3.15 workstream B PR B1 — the
// `messages.principal_id` column migration (channelStoreSchemaVersion
// v11 → v12). Lives in its own sibling file per the sqlite_migrations.go
// header convention (that file is near the 500-line cap; v8→v9, v9→v10 and
// v10→v11 set the per-migration-file precedent).
package channels

import (
	"database/sql"
	"fmt"
)

// migrateV11ToV12 lands the ISSUE-0130 shape (b) tenant column on `messages`.
// Forward-only and a pure addition — one additive, defaulted column, no index
// or table rebuild, every existing index left intact:
//
//   - `principal_id TEXT NOT NULL DEFAULT ”` is the tenant the publish was
//     attributed to. Every writer names the column explicitly ([sqliteStore]
//     .AddMessage), so the DEFAULT is never a value a new row takes — it is
//     purely the backfill, and it is deliberately EMPTY rather than
//     [DefaultPrincipalID].
//
// # Why the backfill is `''` and not `'local'`
//
// A message that predates the column carries no evidence of who caused it.
// `'local'` would be the wrong way to say that, because `'local'` is also a
// real answer — "this publish had no verified tenant" — that a v12 writer
// stamps on an unauthenticated publish, and on every publish under
// `auth.mode: disabled`. Backfilling `'local'` makes those two indis-
// tinguishable on read, and PR B2's consumer branches on exactly that
// distinction: it treats a PRESENT principal as attribution and derives
// persona memory under it. Under `'local'` the first post-upgrade catch-up
// would read every pre-migration row as attributed and derive one
// authenticated person's content into the shared tenant — the ISSUE-0130
// leak, reopened for the upgrade window. `”` is absent to every reader
// (`seed_principal_metadata` rejects it, as does `principal_id_from_metadata`),
// so those rows stay unattributable and the shape-(a) skip still covers them.
//
// The v12 column has never shipped in a tagged release, so this default is
// not yet frozen history; once v0.3.15 ships it is, and a change would need
// v13.
//
// # Why the backfill is not a downgrade of what is already stored
//
// `messages` has never held a tenant, so there is nothing to lose. Contrast
// the persona-memory store's own v11, which backfilled `'local'` onto rows
// that DID have an owner in the operator's head — the v0.3.14 activation-day
// hazard. Here the column and its first writer land together (below), so the
// only rows that read back `'local'` are the ones for which that is the
// truth.
//
// # No reader in this PR
//
// The column ships DARK on the read side: [scanMessage] populates
// [ChannelMessage.PrincipalID] and the REST DTO surfaces it, but no query
// filters on it and no persona consumes it. Verbatim recall (RFC 0035 §F /
// RFC 0036 §C) stays membership-and-epoch scoped — a principal predicate
// there would be a *different* feature, and a wrong one: channel history is
// the room's shared transcript, not per-tenant memory. PR B2 is the consumer,
// seeding `_build_replay_event` so catch-up derivation lands under the tenant
// that actually spoke instead of the shared `local` bucket.
//
// SQLite ≥3.20 supports `ADD COLUMN ... DEFAULT <constant>` without a
// backfill UPDATE, so the column lands in one statement. The migration runs
// in one transaction and stamps `user_version` inside it (PR #335 review L3)
// so the schema change and its version bookkeeping commit atomically.
//
// `messages_fts` (v10) is an external-content index over `content` only, so
// the added column does not touch it and no `('rebuild')` is required.
func migrateV11ToV12(db *sql.DB) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	if _, err := tx.Exec(
		`ALTER TABLE messages ADD COLUMN principal_id TEXT NOT NULL DEFAULT ''`,
	); err != nil {
		return fmt.Errorf("add messages.principal_id: %w", err)
	}
	if err := stampUserVersionTx(tx, 12); err != nil {
		return err
	}
	return tx.Commit()
}
