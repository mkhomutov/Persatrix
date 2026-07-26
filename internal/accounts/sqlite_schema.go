package accounts

import (
	"database/sql"
	"fmt"
)

// accountStoreSchemaVersion is the latest schema version this binary
// knows how to produce — the same versioned-migration discipline as
// internal/channels/sqlite_schema.go: one numbered `case` arm per
// migration, `PRAGMA user_version` stamped inside the migration's own
// transaction, idempotent on reopen (RFC 0039 §B).
//
// History:
//
//	v1 — RFC 0039 PR 1 baseline: the `accounts` and `sessions` tables
//	    plus `idx_sessions_account`. `role` / `auth_method` / `status`
//	    are TEXT with no CHECK constraint — the allowlists in
//	    accounts.go own the vocabulary at the write boundary, per the
//	    RFC 0016 extensible-string pattern, so a new role is a one-line
//	    change with no migration. The §A 1:1 account↔participant
//	    invariant is the UNIQUE on `participant_id`; relaxing it to
//	    1:many (RFC Open Question #5) is a deliberate later migration
//	    that drops the constraint. `sessions` ships with the schema so
//	    the FK is born enforced; its store logic is the next PR's.
const accountStoreSchemaVersion = 1

// schemaV1SQL is the RFC 0039 §B baseline, applied (and stamped) by
// migrateV0ToV1 on a fresh database. Timestamps are INTEGER unix
// seconds (UTC) per the RFC's schema, unlike channels' DATETIME —
// comparisons against `expires_at` happen in Go against a numeric
// column, never via SQLite date parsing.
const schemaV1SQL = `
CREATE TABLE accounts (
    id             TEXT PRIMARY KEY,
    username       TEXT NOT NULL UNIQUE,
    auth_method    TEXT NOT NULL DEFAULT 'password',
    password_hash  TEXT,
    role           TEXT NOT NULL DEFAULT 'user',
    participant_id TEXT NOT NULL UNIQUE,
    status         TEXT NOT NULL DEFAULT 'active',
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

CREATE TABLE sessions (
    token_hash   TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL REFERENCES accounts(id),
    issued_at    INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    last_used_at INTEGER NOT NULL,
    revoked_at   INTEGER
);

CREATE INDEX idx_sessions_account ON sessions(account_id);
`

// applySchema brings a freshly opened database (or one created by an
// earlier binary) up to [accountStoreSchemaVersion]. Unlike the
// channels runner there is no pre-stamping v1 era to special-case: the
// baseline itself is migration v1, so every version this store has ever
// produced was stamped inside its own transaction.
func applySchema(db *sql.DB) error {
	// Defensive PRAGMA — the DSN sets it, but sessions.account_id →
	// accounts.id must be enforced (not decorative, §B) even for a
	// future caller wiring a pre-existing *sql.DB through.
	if _, err := db.Exec(`PRAGMA foreign_keys = ON;`); err != nil {
		return fmt.Errorf("accounts: enable foreign_keys: %w", err)
	}

	var current int
	if err := db.QueryRow(`PRAGMA user_version;`).Scan(&current); err != nil {
		return fmt.Errorf("accounts: read user_version: %w", err)
	}
	if current > accountStoreSchemaVersion {
		return fmt.Errorf("accounts: database user_version=%d is newer than supported %d (downgrade unsupported)",
			current, accountStoreSchemaVersion)
	}

	for v := current + 1; v <= accountStoreSchemaVersion; v++ {
		if err := applyMigration(db, v); err != nil {
			return fmt.Errorf("accounts: migrate to v%d: %w", v, err)
		}
	}
	return nil
}

// applyMigration runs the SQL for a single forward step. Future PRs add
// `case N:` arms and bump the const above.
func applyMigration(db *sql.DB, target int) error {
	switch target {
	case 1:
		return migrateV0ToV1(db)
	default:
		return fmt.Errorf("no migration registered for v%d", target)
	}
}

// stampUserVersionTx writes `PRAGMA user_version = target` inside the
// supplied transaction, as every migration's final statement before
// Commit — the schema change and its version bookkeeping commit (or
// roll back) atomically, the channels PR #335 L3 discipline. SQLite
// does not honour `?` placeholders for PRAGMA; interpolating the int
// constant is safe because it is never user input.
func stampUserVersionTx(tx *sql.Tx, target int) error {
	if _, err := tx.Exec(fmt.Sprintf(`PRAGMA user_version = %d`, target)); err != nil {
		return fmt.Errorf("stamp user_version=%d: %w", target, err)
	}
	return nil
}

// migrateV0ToV1 creates the §B baseline on a fresh database.
func migrateV0ToV1(db *sql.DB) error {
	tx, err := db.Begin()
	if err != nil {
		return fmt.Errorf("begin: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	if _, err := tx.Exec(schemaV1SQL); err != nil {
		return fmt.Errorf("apply v1 baseline: %w", err)
	}
	if err := stampUserVersionTx(tx, 1); err != nil {
		return err
	}
	return tx.Commit()
}
