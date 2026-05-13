package channels

import (
	"database/sql"
	"fmt"
	"strings"
)

// channelStoreSchemaVersion is the latest schema version this binary knows
// how to produce. Incremented in lock-step with each new entry in
// [applyMigration]. The store stamps `PRAGMA user_version` to this value
// after a successful run.
//
// History:
//
//	v1 — PR #231 (RFC 0011 Phase 1a) baseline: channels.name TEXT NOT NULL
//	    UNIQUE; DM/thread rows stored the canonical id as a placeholder.
//	v2 — PR #245 (RFC 0011 Phase 1b, this PR) closes review SF-4: name is
//	    relaxed to TEXT NULL, group-only uniqueness moved to a partial
//	    unique index ux_channels_name_group. The placeholder shim that
//	    previously stored the canonical id in `name` for DM/thread rows
//	    is dropped — those rows now hold NULL there.
//	v3 — RFC 0031 Phase 1 PR 2: introduces the `sessions` table and adds
//	    `session_id TEXT NOT NULL DEFAULT 'legacy'` to `channels` and
//	    `messages`. Replaces the chronological-scan index with the
//	    covering `(channel_id, session_id, timestamp DESC)` shape that
//	    keeps the today's `channel_id`-only history scan cheap AND lets
//	    Phase 2's per-session filter use the same index. Adds
//	    `idx_channels_session` for per-session channel lookups. The
//	    `legacy` session is a synthetic carve-out (no row in `sessions`)
//	    — Phase 3 CLI's `persatrix session new --label legacy` is
//	    rejected, per OQ #2 resolution.
const channelStoreSchemaVersion = 3

// schemaV1SQL is the original schema shipped in PR #231. Applied verbatim
// when opening a fresh database; the v1→v2 migration below uses
// `ALTER TABLE` so existing v1 databases land at the same shape.
const schemaV1SQL = `
CREATE TABLE IF NOT EXISTS channels (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    channel_type TEXT NOT NULL CHECK (channel_type IN ('group', 'dm', 'thread')),
    description  TEXT NOT NULL DEFAULT '',
    created_at   DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
    channel_id     TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    participant_id TEXT NOT NULL,
    respond_policy TEXT NOT NULL DEFAULT 'when_mentioned'
        CHECK (respond_policy IN ('when_mentioned', 'always', 'never')),
    joined_at      DATETIME NOT NULL,
    PRIMARY KEY (channel_id, participant_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id         TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    sender_id  TEXT NOT NULL,
    content    TEXT NOT NULL,
    timestamp  DATETIME NOT NULL,
    thread_id  TEXT REFERENCES messages(id) ON DELETE CASCADE,
    mentions   TEXT NOT NULL DEFAULT '[]',
    metadata   TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_messages_channel_ts ON messages(channel_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_messages_thread     ON messages(thread_id) WHERE thread_id IS NOT NULL;
`

// applySchema brings a freshly opened database (or an existing one created
// by an earlier binary) up to [channelStoreSchemaVersion]. The PRAGMA-
// driven, hand-rolled migration runner is intentionally minimal — we ship
// it now (v1→v2) so the first schema change after PR #231 lands cleanly,
// without taking a dependency on golang-migrate. Each future PR adds one
// `case N:` block and bumps the const above.
//
// PR #231 review SF-4: the v1 schema declared
// `channels.name TEXT NOT NULL UNIQUE`, which forced DM and thread rows
// (which have no user-visible name) to store the canonical id as a
// placeholder. The reader-side `if name != ch.ID` shim then translated
// the placeholder back to an empty Name on scan. v2 drops the shim:
// `name` is nullable, and a partial UNIQUE INDEX enforces uniqueness for
// `channel_type = 'group'` only.
//
// PR #335 review L3 (RFC 0031 Phase 1 PR 2): each migration stamps its
// own `PRAGMA user_version = N` *inside* its transaction. The earlier
// shape stamped the version once after the loop succeeded — if any step
// committed and the final stamp then failed (lock contention, I/O), the
// next boot would read a stale `user_version` and silently re-run earlier
// migrations on a newer-shape schema. For v1→v2 specifically that means
// rebuilding `channels` while copying only the v1 columns, dropping any
// `session_id` data v2→v3 had since added. Folding the stamp into each
// migration's tx makes the schema change and its version-bookkeeping
// commit (or roll back) atomically. The two
// `Test{V1ToV2,V2ToV3}_StampsUserVersionInTransaction` tests pin the
// property at the single-step boundary so a future regression surfaces.
func applySchema(db *sql.DB) error {
	// Apply the v1 baseline first — `IF NOT EXISTS` guards make this a
	// no-op for existing databases, while a fresh database lands at v1
	// before the migration loop below stamps user_version.
	if _, err := db.Exec(schemaV1SQL); err != nil {
		return fmt.Errorf("channels: apply schema: %w", err)
	}
	// Defensive PRAGMA — DSN sets it, but a future caller wiring a
	// pre-existing *sql.DB through a yet-to-be-added constructor should
	// still see foreign keys enforced.
	if _, err := db.Exec(`PRAGMA foreign_keys = ON;`); err != nil {
		return fmt.Errorf("channels: enable foreign_keys: %w", err)
	}

	var current int
	if err := db.QueryRow(`PRAGMA user_version;`).Scan(&current); err != nil {
		return fmt.Errorf("channels: read user_version: %w", err)
	}
	// First-time CREATE on a fresh database: PRAGMA reports 0; treat that
	// as v1 (the baseline we just applied) so the v1→v2 step runs once.
	if current == 0 {
		current = 1
	}
	if current > channelStoreSchemaVersion {
		return fmt.Errorf("channels: database user_version=%d is newer than supported %d (downgrade unsupported)",
			current, channelStoreSchemaVersion)
	}

	// Each migration stamps `user_version` inside its own tx — see header
	// comment for the L3 hazard this closes. No post-loop stamp here.
	for v := current + 1; v <= channelStoreSchemaVersion; v++ {
		if err := applyMigration(db, v); err != nil {
			return fmt.Errorf("channels: migrate to v%d: %w", v, err)
		}
	}
	return nil
}

// stampUserVersionTx writes `PRAGMA user_version = target` inside the
// supplied transaction. SQLite does not honour `?` placeholders for
// PRAGMA, so the value is interpolated as a literal — safe because
// `target` is an int constant chosen by the binary, never user input.
//
// Lifted into a helper because every migration calls it as its final
// statement before `tx.Commit()`. See `applySchema` header for the L3
// rationale.
func stampUserVersionTx(tx *sql.Tx, target int) error {
	if _, err := tx.Exec(fmt.Sprintf(`PRAGMA user_version = %d`, target)); err != nil {
		return fmt.Errorf("stamp user_version=%d: %w", target, err)
	}
	return nil
}

// applyMigration runs the SQL for a single forward step. New steps are
// added as additional `case` arms and the const above is bumped.
func applyMigration(db *sql.DB, target int) error {
	switch target {
	case 2:
		return migrateV1ToV2(db)
	case 3:
		return migrateV2ToV3(db)
	default:
		return fmt.Errorf("no migration registered for v%d", target)
	}
}

// migrateV2ToV3 lands the RFC 0031 Phase 1 per-session storage tags on the
// orchestrator side. Forward-only and idempotent against the v2 shape:
//
//   - CREATE TABLE sessions: empty at migration time; the `legacy` carve-out
//     is synthetic (no row), per [OQ #2] resolution. Phase 3 CLI's
//     `persatrix session new` is the canonical create path.
//   - ALTER TABLE channels / messages: SQLite ≥3.20 supports
//     ADD COLUMN ... NOT NULL DEFAULT '<constant>' without a backfill UPDATE,
//     so every pre-v3 row picks up `session_id = 'legacy'` in one statement.
//   - Index replacement: drop `idx_messages_channel_ts` (v2 chronological
//     scan), create `idx_messages_channel_session(channel_id, session_id,
//     timestamp DESC)`. The leading `channel_id` keeps today's
//     `WHERE channel_id = ? ORDER BY timestamp DESC` scan cheap, and the
//     trailing `session_id` lets Phase 2's per-session filter use the same
//     index without a second one.
//   - `idx_channels_session` supports fast per-session channel listings
//     (Phase 3 CLI `persatrix session list`).
//
// The whole migration runs inside one transaction so a partial failure
// rolls back cleanly — no need for the foreign-keys-off rebuild dance
// migrateV1ToV2 uses.
//
// [OQ #2]: ../../docs/rfcs/0031-per-session-namespacing-channels.md#open-questions
func migrateV2ToV3(db *sql.DB) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	stmts := []string{
		// `created_at` and `archived_at` are REAL (Julian/unix seconds as
		// float) per RFC 0031 §D pseudocode. Inconsistent with the
		// DATETIME-encoded channels/memberships/messages columns, but
		// aligned with the per-session timestamp ergonomics the spec
		// pinned for the dementia-test bridge in Phase 2.
		`CREATE TABLE sessions (
            id            TEXT PRIMARY KEY,
            label         TEXT,
            created_at    REAL NOT NULL,
            archived_at   REAL,
            metadata_json TEXT
        )`,
		`ALTER TABLE channels ADD COLUMN session_id TEXT NOT NULL DEFAULT 'legacy'`,
		`ALTER TABLE messages ADD COLUMN session_id TEXT NOT NULL DEFAULT 'legacy'`,
		`DROP INDEX IF EXISTS idx_messages_channel_ts`,
		`CREATE INDEX idx_messages_channel_session
            ON messages(channel_id, session_id, timestamp DESC)`,
		`CREATE INDEX idx_channels_session ON channels(session_id)`,
	}
	for _, q := range stmts {
		if _, err := tx.Exec(q); err != nil {
			return fmt.Errorf("exec %q: %w", firstLine(q), err)
		}
	}
	// PR #335 review L3: stamp inside the tx so the schema change and
	// the version bookkeeping commit (or roll back) atomically.
	if err := stampUserVersionTx(tx, 3); err != nil {
		return err
	}
	return tx.Commit()
}

// migrateV1ToV2 relaxes channels.name to TEXT NULL and replaces the column-
// level UNIQUE with a partial unique index scoped to group channels.
//
// SQLite cannot ALTER COLUMN to drop NOT NULL in place, so the migration
// uses the canonical "12-step" table rebuild: create the new table, copy
// rows (NULL-ing the placeholder name for non-group rows), drop the old
// table, rename. Existing FKs (memberships → channels, messages → channels)
// reference channels(id) so the rebuild does not require re-pointing
// children — `ON DELETE CASCADE` on the children remains intact.
//
// PR #245 review (High): per the SQLite "Making Other Kinds of Table
// Schema Changes" guide (https://sqlite.org/lang_altertable.html §7),
// `PRAGMA foreign_keys=OFF` MUST be set *outside* the transaction before
// the rebuild and restored after. The PRAGMA is a no-op inside a
// transaction; running the rebuild with FK enforcement on relies on
// undocumented driver behaviour for the `RENAME TO channels` step to
// re-bind child FKs (memberships, messages → channels.id). The earlier
// implementation worked on modernc.org/sqlite today but is fragile
// against driver changes. The companion regression test
// TestSQLiteStore_Migration_V1ToV2_PreservesChildRows pins this contract
// with seeded membership + message rows.
//
// We capture the previous foreign_keys value so a connection that
// arrived with FK off (an unusual but legal configuration) is left as
// it was found.
func migrateV1ToV2(db *sql.DB) error {
	var prevFK int
	if err := db.QueryRow(`PRAGMA foreign_keys`).Scan(&prevFK); err != nil {
		return fmt.Errorf("channels: read foreign_keys: %w", err)
	}
	if _, err := db.Exec(`PRAGMA foreign_keys = OFF`); err != nil {
		return fmt.Errorf("channels: disable foreign_keys for rebuild: %w", err)
	}
	// Restore the previous PRAGMA value unconditionally — even on the
	// failure path the connection must not leak with FK enforcement
	// silently disabled.
	defer func() {
		restore := "ON"
		if prevFK == 0 {
			restore = "OFF"
		}
		_, _ = db.Exec(`PRAGMA foreign_keys = ` + restore)
	}()

	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	stmts := []string{
		`CREATE TABLE channels_v2 (
            id           TEXT PRIMARY KEY,
            name         TEXT,
            channel_type TEXT NOT NULL CHECK (channel_type IN ('group', 'dm', 'thread')),
            description  TEXT NOT NULL DEFAULT '',
            created_at   DATETIME NOT NULL
        )`,
		// Copy rows; NULL the placeholder `name` for DM/thread rows where
		// PR #231's writer stored the canonical id (storedName fallback).
		`INSERT INTO channels_v2 (id, name, channel_type, description, created_at)
            SELECT id,
                   CASE WHEN channel_type = 'group' THEN name ELSE NULL END,
                   channel_type, description, created_at
              FROM channels`,
		// FK on memberships/messages references channels(id); SQLite's
		// rename-table preserves PK + the FK target, so we drop the old
		// table and rename without touching children.
		`DROP TABLE channels`,
		`ALTER TABLE channels_v2 RENAME TO channels`,
		// Partial unique index: only group rows participate in name
		// uniqueness; DM/thread rows hold NULL and are exempt.
		`CREATE UNIQUE INDEX ux_channels_name_group
            ON channels(name) WHERE channel_type = 'group'`,
	}
	for _, q := range stmts {
		if _, err := tx.Exec(q); err != nil {
			return fmt.Errorf("exec %q: %w", firstLine(q), err)
		}
	}
	// PR #335 review L3: stamp inside the tx so the rebuild and the
	// version bookkeeping commit (or roll back) atomically — a stale
	// stamp here is the entry point to the data-loss scenario described
	// in `applySchema`'s header.
	if err := stampUserVersionTx(tx, 2); err != nil {
		return err
	}
	return tx.Commit()
}

// firstLine returns the first non-empty trimmed line of q for error
// messages. SQLite's own driver error already echoes the failing fragment;
// this helper just keeps our wrapper concise.
func firstLine(q string) string {
	for _, line := range strings.Split(q, "\n") {
		t := strings.TrimSpace(line)
		if t != "" {
			return t
		}
	}
	return q
}
