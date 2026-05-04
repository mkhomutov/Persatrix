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
const channelStoreSchemaVersion = 2

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
// PR #231 review SF-4 (this PR): the v1 schema declared
// `channels.name TEXT NOT NULL UNIQUE`, which forced DM and thread rows
// (which have no user-visible name) to store the canonical id as a
// placeholder. The reader-side `if name != ch.ID` shim then translated
// the placeholder back to an empty Name on scan. v2 drops the shim:
// `name` is nullable, and a partial UNIQUE INDEX enforces uniqueness for
// `channel_type = 'group'` only.
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

	for v := current + 1; v <= channelStoreSchemaVersion; v++ {
		if err := applyMigration(db, v); err != nil {
			return fmt.Errorf("channels: migrate to v%d: %w", v, err)
		}
	}

	// PRAGMA user_version takes a literal — `?` placeholders are not
	// honoured. The value is internally generated, never user-supplied,
	// so the `Sprintf` is safe.
	if _, err := db.Exec(fmt.Sprintf(`PRAGMA user_version = %d;`, channelStoreSchemaVersion)); err != nil {
		return fmt.Errorf("channels: stamp user_version: %w", err)
	}
	return nil
}

// applyMigration runs the SQL for a single forward step. New steps are
// added as additional `case` arms and the const above is bumped.
func applyMigration(db *sql.DB, target int) error {
	switch target {
	case 2:
		return migrateV1ToV2(db)
	default:
		return fmt.Errorf("no migration registered for v%d", target)
	}
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
func migrateV1ToV2(db *sql.DB) error {
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
