package channels

// `messages.principal_id` backfill repair (channelStoreSchemaVersion v12 → v13).
// Lives in its own sibling file per the convention in sqlite_migrations.go.

import (
	"database/sql"
	"errors"
	"fmt"
)

// v12PrincipalBackfillOriginalDefault is the column DEFAULT the FIRST cut of
// [migrateV11ToV12] shipped, before the PR B2 review inverted it to `”`.
//
// It is the DETECTOR, and it is exact: SQLite records a column's default as
// literal SQL text in `PRAGMA table_info`, so a store migrated by the original
// code reports `'local'` (quotes included) and one migrated by the corrected
// code reports `”`. That is what makes this repair targeted instead of
// speculative — it touches only the stores that actually took the bad
// backfill, and is a no-op on every store that never saw it.
const v12PrincipalBackfillOriginalDefault = `'local'`

// migrateV12ToV13 un-attributes rows that the ORIGINAL v11→v12 migration
// backfilled to `local`.
//
// # Why an already-applied migration needs a successor
//
// The PR B2 review changed migrateV11ToV12's `ADD COLUMN` DEFAULT from
// `'local'` to `”`, because `'local'` is a real answer a v12 writer stamps
// ("this publish had no verified tenant" — an unauthenticated publish, or the
// whole deployment under `auth.mode: disabled`) and therefore cannot double
// as "no answer". [seed_principal_metadata] treats a PRESENT principal as
// attribution and `agents.persona_runtime.close_path` derives persona memory
// under it, so a row that predates the column must read as ABSENT.
//
// Editing the migration in place only helps stores that have not run it yet.
// applyMigration is dispatched on `PRAGMA user_version`, and migrateV11ToV12
// stamps 12 inside its own transaction, so a store already at v12 never
// re-enters it and keeps the `'local'` backfill forever. On such a store the
// first post-upgrade catch-up reads every pre-migration row as attributed and
// derives an authenticated person's content into the shared tenant — the
// ISSUE-0130 leak — and the shape-(b) re-derivation guard then STORES that
// digest, so the wrong-tenant episode is never re-derived correctly even
// after the row is fixed. Detect-and-repair is the only shape that reaches
// those stores.
//
// # What it deliberately over-corrects
//
// On an affected store a backfilled `local` and a genuinely-stamped `local`
// are indistinguishable in the data — that indistinguishability IS the bug —
// so this rewrites both. The cost is bounded and one-directional: a message
// legitimately published with no verified tenant, during the window that
// store ran the original code, stops being attributable and its replayed span
// is skipped by the shape-(a) rule rather than derived. That is the
// conservative side (no derivation), against the leak on the other side (an
// authenticated person's content in the shared tenant, made permanent by the
// guard). ISSUE-0130 makes that trade everywhere else too.
//
// A store that migrates v11 → v12 → v13 in one open is untouched: v12 has
// already written `”` everywhere, the detector reads `”`, and the UPDATE
// never runs.
//
// The column DEFAULT itself is left alone. Changing it needs a table rebuild,
// and it is dead either way — [sqliteStore.AddMessage] names `principal_id`
// explicitly on every INSERT, so no row has ever taken the DEFAULT except
// through the backfill this repairs. Leaving it also keeps the detector
// readable after the fact.
//
// Runs in one transaction and stamps `user_version` inside it (PR #335 review
// L3) so the repair and its version bookkeeping commit atomically.
//
// # Why the FTS trigger is dropped around the rewrite
//
// `messages_fts` (v10) indexes `content` only, so this rewrite changes nothing
// it holds — but [messagesFTSUpdateTriggerDDL] is `AFTER UPDATE ON messages`,
// a WHOLE-ROW trigger, not `AFTER UPDATE OF content`. Left in place it fires
// once per repaired row and does a full FTS5 `'delete'` + re-insert of that
// row's content, turning a cheap column rewrite into a re-tokenisation of
// every affected row on the orchestrator's boot path — and on an affected
// store that is every pre-v12 row plus every unauthenticated publish. Worse,
// the trigger's `'delete'` passes `old.content`: if the external-content index
// is ever out of step with `messages` (the v10 header's own VACUUM caveat)
// that corrupts the index for exactly the rows this migration touches.
//
// Dropping and recreating it inside the same transaction keeps the index
// bit-identical (which is correct — no content changed) and keeps the v10
// invariant that the trigger "is expected never to fire" literally true. The
// drop is conditional because a build without FTS5 has no trigger to drop:
// v9->v10 skips the whole FTS block in that case and only advances the
// version line.
func migrateV12ToV13(db *sql.DB) error {
	needsRepair, err := v12PrincipalBackfilledLocal(db)
	if err != nil {
		return err
	}

	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	if needsRepair {
		hadFTSTrigger, err := messagesFTSUpdateTriggerExists(tx)
		if err != nil {
			return err
		}
		if hadFTSTrigger {
			if _, err := tx.Exec(`DROP TRIGGER messages_au`); err != nil {
				return fmt.Errorf("drop messages_au for backfill repair: %w", err)
			}
		}
		if _, err := tx.Exec(
			`UPDATE messages SET principal_id = '' WHERE principal_id = ?`,
			DefaultPrincipalID,
		); err != nil {
			return fmt.Errorf("repair messages.principal_id backfill: %w", err)
		}
		if hadFTSTrigger {
			if _, err := tx.Exec(messagesFTSUpdateTriggerDDL); err != nil {
				return fmt.Errorf("restore messages_au after backfill repair: %w", err)
			}
		}
	}
	if err := stampUserVersionTx(tx, 13); err != nil {
		return err
	}
	return tx.Commit()
}

// v12PrincipalBackfilledLocal reports whether this store's `messages`
// .principal_id column was added by the ORIGINAL v11→v12 migration, i.e.
// whether its recorded DEFAULT is `'local'` rather than `”`.
//
// Returns false — repair nothing — when the column is absent entirely, which
// is the partial-baseline case every sibling handler also tolerates rather
// than failing a boot on.
//
// Asked through the `pragma_table_info(...)` TABLE-VALUED form rather than the
// `PRAGMA table_info(messages)` statement form (PR B2 review round 5). The
// statement form returns a fixed column list that has grown before (SQLite
// 3.26 added `hidden` to its `_xinfo` sibling), and reading it meant a
// positional six-destination `Scan`. This runs on the orchestrator BOOT path:
// a driver bump that changed that shape would fail `Scan`, propagate out
// through applyMigration into applySchema, and refuse to open the channel
// store — on every store, including the overwhelming majority that need no
// repair at all. The table-valued form names the one column it wants and is
// shape-independent; `sqlite_session_migration_test.go` already uses it.
func v12PrincipalBackfilledLocal(db *sql.DB) (bool, error) {
	var dfltValue sql.NullString
	err := db.QueryRow(
		`SELECT dflt_value FROM pragma_table_info(?) WHERE name = ?`,
		"messages", "principal_id",
	).Scan(&dfltValue)
	if errors.Is(err, sql.ErrNoRows) {
		// No `messages` table, or a v11 store that never got the column.
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("read messages.principal_id default: %w", err)
	}
	return dfltValue.Valid &&
		dfltValue.String == v12PrincipalBackfillOriginalDefault, nil
}

// messagesFTSUpdateTriggerExists reports whether this store carries the v10
// `messages_au` trigger. It is absent on a build without FTS5, where v9->v10
// skips the whole index block and only advances the version line, so the
// repair must not assume it can drop it.
func messagesFTSUpdateTriggerExists(tx *sql.Tx) (bool, error) {
	var name string
	err := tx.QueryRow(
		`SELECT name FROM sqlite_master WHERE type='trigger' AND name='messages_au'`,
	).Scan(&name)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("read messages_au trigger: %w", err)
	}
	return true, nil
}
