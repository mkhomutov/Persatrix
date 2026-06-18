package channels

// RFC 0035 PR 1 — the channel-store v8→v9 migration that lands the
// `membership_intervals` ledger. Split into its own file because
// sqlite_migrations.go is at the repo's 500-line cap; the `case 9:` arm that
// dispatches to migrateV8ToV9 stays in that file's applyMigration runner.

import (
	"database/sql"
	"fmt"
)

// migrateV8ToV9 lands the RFC 0035 `membership_intervals` ledger — the
// append-only record of every membership stint, one row per
// `(channel_id, participant_id, joined_at, left_at)` — so the store can answer
// the one question the current-state-only `memberships` projection cannot:
// *was participant X a member of channel Y at time T?* (the time-range filter
// RFC 0036 verbatim recall scopes on). See the [channelStoreSchemaVersion] v9
// history note for the full rationale. Forward-only and a pure addition: one
// table + two indexes + a backfill from `memberships`, no existing table /
// row / index touched, so every `memberships` / `messages` query reads back
// byte-identically post-v9.
//
// Notable choices:
//   - `joined_at` / `left_at` are DATETIME (not the `sessions` table's REAL
//     choice) so RFC 0036's join compares them against `messages.timestamp`
//     directly. `left_at` is NULL while a stint is open.
//   - `ON DELETE CASCADE` mirrors `memberships` / `messages`: deleting a
//     channel discards its intervals too, so a deleted channel is
//     consistently unrecallable.
//   - `ux_membership_intervals_open` is the open-interval INVARIANT GUARD, not
//     a convenience — a second OPEN stint for a pair fails the INSERT loudly
//     rather than silently corrupting history (PR 3's write path leans on it).
//     The index is partial (WHERE left_at IS NULL), so a CLOSED stint may sit
//     beside the open one — which is what lets a join → leave → rejoin keep a
//     closed `[t0,t1)` interval next to an open `[t2,NULL)` one.
//   - The §D backfill seeds one OPEN interval per current `memberships` row,
//     copying its `joined_at`. The ledger is exact from v9 forward; stints
//     removed before v9 are unrecoverable (accepted gap, RFC §D).
//
// The whole migration runs in one transaction and stamps `user_version`
// inside it (PR #335 review L3) so the schema change and its version
// bookkeeping commit (or roll back) atomically — the correctness bar for an
// access-control record, since RFC 0036's `EXISTS` join *is* the recall
// authorization decision.
func migrateV8ToV9(db *sql.DB) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	stmts := []string{
		`CREATE TABLE membership_intervals (
            id             INTEGER PRIMARY KEY,
            channel_id     TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
            participant_id TEXT NOT NULL,
            joined_at      DATETIME NOT NULL,
            left_at        DATETIME
        )`,
		`CREATE INDEX idx_membership_intervals_lookup
            ON membership_intervals(channel_id, participant_id, joined_at)`,
		`CREATE UNIQUE INDEX ux_membership_intervals_open
            ON membership_intervals(channel_id, participant_id) WHERE left_at IS NULL`,
		// §D backfill: one OPEN interval per current member, joined_at copied.
		`INSERT INTO membership_intervals (channel_id, participant_id, joined_at, left_at)
            SELECT channel_id, participant_id, joined_at, NULL FROM memberships`,
	}
	for _, q := range stmts {
		if _, err := tx.Exec(q); err != nil {
			return fmt.Errorf("exec %q: %w", firstLine(q), err)
		}
	}
	if err := stampUserVersionTx(tx, 9); err != nil {
		return err
	}
	return tx.Commit()
}
