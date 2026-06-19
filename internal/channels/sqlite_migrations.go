package channels

// Forward-step schema migrations for the channel store. Split out of
// sqlite_schema.go (which keeps the version const, the schemaV1SQL
// baseline, and the applySchema runner) to stay under the repo's
// 500-line file-size cap. Each future PR adds one migrateV(N-1)ToVN
// function (here, or — now this file is near the same cap — in a dedicated
// sibling file: v8→v9 lives in sqlite_membership_intervals_migration.go and
// v9→v10 in sqlite_messages_fts_migration.go), a `case N:` arm in
// applyMigration, and bumps channelStoreSchemaVersion.

import (
	"database/sql"
	"fmt"
	"strings"
)

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
	case 4:
		return migrateV3ToV4(db)
	case 5:
		return migrateV4ToV5(db)
	case 6:
		return migrateV5ToV6(db)
	case 7:
		return migrateV6ToV7(db)
	case 8:
		return migrateV7ToV8(db)
	case 9:
		return migrateV8ToV9(db)
	case 10:
		return migrateV9ToV10(db)
	default:
		return fmt.Errorf("no migration registered for v%d", target)
	}
}

// migrateV7ToV8 lands the RFC 0050 Phase 1 operator-editable channel config
// columns on `channels`. Forward-only and a pure addition — three additive,
// nullable-or-defaulted columns, no index or table rebuild, every existing
// index left intact:
//
//   - `config_overrides_json TEXT` (nullable, no DEFAULT) holds the sparse
//     per-channel governance override set ([ChannelConfigOverrides]) as JSON.
//     NULL = no override = inherit every knob, so a pre-v8 row reads back
//     byte-identically to today. A single sparse blob — not one column per
//     knob — preserves tri-state inherit semantics (absent key = inherit) and
//     lets future knobs land with no further migration (RFC 0050 §refinement).
//   - `config_revision INTEGER NOT NULL DEFAULT 0` is the store-owned,
//     monotonic per-channel revision the optimistic-concurrency apply path
//     bumps. The 0 backfill is the revision gate's seed-only floor: a channel
//     the store has never had edited sits at 0 (RFC 0050 *Migration*).
//   - `config_change_lineage TEXT` (nullable) is RESERVED / dormant — the
//     governance interaction id of the mutation (RFC Open Q2). Phase 1 plumbs
//     it through [PutChannelConfig] but no production caller populates it yet.
//
// SQLite ≥3.20 supports `ADD COLUMN ... DEFAULT <constant>` without a backfill
// UPDATE, so each column lands in one statement. The whole migration runs in
// one transaction and stamps `user_version` inside it (PR #335 review L3) so
// the schema change and its version bookkeeping commit atomically.
func migrateV7ToV8(db *sql.DB) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	stmts := []string{
		`ALTER TABLE channels ADD COLUMN config_overrides_json TEXT`,
		`ALTER TABLE channels ADD COLUMN config_revision INTEGER NOT NULL DEFAULT 0`,
		`ALTER TABLE channels ADD COLUMN config_change_lineage TEXT`,
	}
	for _, q := range stmts {
		if _, err := tx.Exec(q); err != nil {
			return fmt.Errorf("exec %q: %w", firstLine(q), err)
		}
	}
	if err := stampUserVersionTx(tx, 8); err != nil {
		return err
	}
	return tx.Commit()
}

// migrateV6ToV7 adds the RFC 0030 Tier B (v0.3.8) per-member salience-bid
// signals to `memberships`. Forward-only and a pure addition:
//
//   - `threshold REAL` is nullable with NO default, so every pre-v7 row reads
//     back as NULL → unset → bias-to-silence (the conservative Tier B
//     default). A non-NULL default would be wrong here: there is no neutral
//     numeric threshold, and 0.0 vs unset are deliberately distinct (see
//     [MemberConfig.Threshold]).
//   - `salience_gated INTEGER NOT NULL DEFAULT 0` backfills every pre-v7 row to
//     0 — a legacy `always` member that keeps replying unconditionally — so a
//     v0.3.7 database behaves byte-identically. Only members reconciled from
//     the participant/chair vocabulary (or REST-added with it) write 1.
//
// SQLite ≥3.20 supports `ADD COLUMN ... DEFAULT <constant>` without a backfill
// UPDATE, so both columns land in one statement each. The whole migration runs
// in one transaction and stamps `user_version` inside it (PR #335 review L3)
// so the schema change and its version bookkeeping commit atomically. No index
// or table rebuild — the additive columns leave every existing index intact.
func migrateV6ToV7(db *sql.DB) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	stmts := []string{
		`ALTER TABLE memberships ADD COLUMN threshold REAL`,
		`ALTER TABLE memberships ADD COLUMN salience_gated INTEGER NOT NULL DEFAULT 0`,
	}
	for _, q := range stmts {
		if _, err := tx.Exec(q); err != nil {
			return fmt.Errorf("exec %q: %w", firstLine(q), err)
		}
	}
	if err := stampUserVersionTx(tx, 7); err != nil {
		return err
	}
	return tx.Commit()
}

// migrateV5ToV6 adds the run/test-isolation `epoch_id` axis (ISSUE-0085 PR 2)
// to `channels` and `messages` — the Go-store sibling of persona-memory
// migration v12. Forward-only and a pure addition:
//
//   - ALTER TABLE channels / messages: SQLite ≥3.20 supports
//     ADD COLUMN ... NOT NULL DEFAULT '<constant>' without a backfill UPDATE,
//     so every pre-v6 row picks up `epoch_id = 'live'` ([DefaultEpochID]) in
//     one statement — single-world deployments are byte-identical.
//   - idx_channels_epoch / idx_messages_epoch: per-table standalone lookup
//     indexes mirroring the persona-memory v12 `idx_<tier>_epoch` shape. Epoch
//     is a residual equality filter (like principal), not a recall anchor, so
//     the migration leaves the v3 covering index `idx_messages_channel_session`
//     untouched rather than rebuilding it to carry the epoch dimension.
//
// Unlike the v3 `session_id` migration there is no index *replacement* here:
// v6 is purely additive, so the whole migration runs inside one transaction
// and a partial failure rolls back cleanly — no foreign-keys-off rebuild
// dance. The `user_version` stamp commits inside the tx (PR #335 review L3)
// so the schema change and its version bookkeeping are atomic.
func migrateV5ToV6(db *sql.DB) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	stmts := []string{
		`ALTER TABLE channels ADD COLUMN epoch_id TEXT NOT NULL DEFAULT 'live'`,
		`ALTER TABLE messages ADD COLUMN epoch_id TEXT NOT NULL DEFAULT 'live'`,
		`CREATE INDEX idx_channels_epoch ON channels(epoch_id)`,
		`CREATE INDEX idx_messages_epoch ON messages(epoch_id)`,
	}
	for _, q := range stmts {
		if _, err := tx.Exec(q); err != nil {
			return fmt.Errorf("exec %q: %w", firstLine(q), err)
		}
	}
	if err := stampUserVersionTx(tx, 6); err != nil {
		return err
	}
	return tx.Commit()
}

// migrateV4ToV5 drops the sender (`user_id`) axis from `session_bindings`
// (ISSUE-0083). RFC 0031 §A's scope-axes amendment redefines a session as
// room continuity keyed `(agent, channel)`: the sender axis only ever changed
// the multi-party-room case, and changed it wrongly — agent X talking in one
// `group:` room with senders Alice and Bob got two sessions, fragmenting its
// episodic memory of one conversation by who spoke. DMs are unaffected (one
// sender, so the triple already collapses to the pair).
//
// SQLite cannot DROP a PRIMARY-KEY column in place, so the table is rebuilt.
// Existing triple-keyed rows collapse onto the `(agent, channel)` pair with
// `INSERT OR IGNORE ... ORDER BY created_at ASC`: the earliest-created binding
// for each pair inserts first and wins; later rows for the same pair hit the
// new PK and are ignored. A multi-party room therefore keeps its *oldest*
// session id, so the room's longest-running continuity survives the collapse.
//
// The losing sessions' rows in the `sessions` registry are deliberately left
// in place — archive/list still resolve them, and RFC 0013 makes row deletion
// compliance-erasure territory, not a migration's job. Only the binding map
// collapses; no `sessions` row and no memory row is deleted. This is
// forward-continuity only: persona memory already written under a collapsed-
// away session stays in the persona's store but falls outside default room
// recall (which filters to the surviving session + `legacy`), so pre-upgrade
// turns from a losing-session speaker are not recalled by default. That
// cross-subsystem consequence is recorded in the ISSUE-0083 resolution note.
//
// `session_bindings` has no FOREIGN KEY and nothing references it (RFC 0031
// §G code-enforced integrity), so the rebuild needs no foreign-keys-off dance.
// The whole migration runs in one transaction and stamps `user_version`
// inside it (PR #335 review L3) so schema and bookkeeping commit atomically.
func migrateV4ToV5(db *sql.DB) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	stmts := []string{
		`CREATE TABLE session_bindings_v5 (
            agent_id   TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (agent_id, channel_id)
        )`,
		// Earliest-created binding per (agent, channel) wins the collapse:
		// the SELECT feeds rows oldest-first, and OR IGNORE drops every later
		// row that would conflict on the new pair PK.
		`INSERT OR IGNORE INTO session_bindings_v5 (agent_id, channel_id, session_id, created_at)
            SELECT agent_id, channel_id, session_id, created_at
              FROM session_bindings
             ORDER BY created_at ASC`,
		`DROP TABLE session_bindings`,
		`ALTER TABLE session_bindings_v5 RENAME TO session_bindings`,
	}
	for _, q := range stmts {
		if _, err := tx.Exec(q); err != nil {
			return fmt.Errorf("exec %q: %w", firstLine(q), err)
		}
	}
	if err := stampUserVersionTx(tx, 5); err != nil {
		return err
	}
	return tx.Commit()
}

// migrateV3ToV4 adds the `session_bindings` table (ISSUE-0082 PR 1). The
// orchestrator's SessionResolver mints one session id per
// `(agent_id, channel_id, user_id)` triple and persists the mapping here,
// so the id is authoritative and survives a persona-process restart — the
// dementia-test multi-day-arc property recorded in RFC 0031 §B.
//
// Forward-only and a pure addition: it creates one new table and touches no
// existing row, so there is no backfill and the v1→v2 foreign-keys-off
// rebuild dance is unnecessary. `created_at` is REAL (unix seconds as a
// float) to match the sibling `sessions` table, not the DATETIME-encoded
// channels/messages columns.
//
// No FOREIGN KEY ties `session_id` to `sessions(id)`: the resolver writes
// the binding and its `sessions` row in one atomic transaction, so that mint
// — not a schema constraint — is what guarantees every binding references a
// registered session. Integrity is therefore code-enforced, consistent with
// RFC 0031 §G's code-enforced (not DB-enforced) stance for `session_id`.
//
// The whole migration runs in one transaction and stamps `user_version`
// inside it (PR #335 review L3) so the schema change and its version
// bookkeeping commit or roll back atomically.
func migrateV3ToV4(db *sql.DB) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	stmts := []string{
		`CREATE TABLE session_bindings (
            agent_id   TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            user_id    TEXT NOT NULL,
            session_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (agent_id, channel_id, user_id)
        )`,
	}
	for _, q := range stmts {
		if _, err := tx.Exec(q); err != nil {
			return fmt.Errorf("exec %q: %w", firstLine(q), err)
		}
	}
	if err := stampUserVersionTx(tx, 4); err != nil {
		return err
	}
	return tx.Commit()
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
