package channels

import (
	"database/sql"
	"fmt"
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
//	v4 — ISSUE-0082 PR 1: introduces the `session_bindings` table — the
//	    per-request `(agent_id, channel_id, user_id) → session_id` map the
//	    orchestrator's SessionResolver mints into. Authoritative + persisted
//	    so a per-conversation session id survives a persona-process restart
//	    (RFC 0031 §B session unit). Pure addition: no existing column or row
//	    is touched.
//	v5 — ISSUE-0083 (scope-axes reframing): drops the `user_id` (sender) axis
//	    from `session_bindings`, rebuilding it onto the `(agent_id,
//	    channel_id)` pair — room continuity, per the RFC 0031 §A amendment.
//	    The sender axis fragmented a multi-party room (one session per
//	    speaker); the channel axis alone already isolates DMs (distinct
//	    channel ids). Existing triple bindings collapse to the pair, the
//	    earliest-created winning so a room keeps its oldest continuity.
//	v6 — ISSUE-0085 PR 2 (epoch axis): adds `epoch_id TEXT NOT NULL DEFAULT
//	    'live'` to `channels` and `messages` — the run/test-isolation sibling
//	    of the v3 `session_id` operator namespace — plus per-table
//	    `idx_<table>_epoch` lookup indexes. Where `session_id` is the
//	    room-continuity axis (with a `legacy` carve-out), `epoch_id` is the
//	    strict-equality isolation axis ([DefaultEpochID], no carve-out). The
//	    Go-store half of the axis whose persona-memory half is migration v12;
//	    a pure additive column, every existing row backfilled to `live` by the
//	    SQL DEFAULT. No recall change and no non-default writer yet — the
//	    producer lights up with the gRPC rail (PR 4).
//	v7 — RFC 0030 Tier B PR 2b (v0.3.8): adds the per-member salience-bid
//	    signals to `memberships` — a nullable `threshold REAL` (unset → NULL →
//	    bias-to-silence) and `tier_b_active INTEGER NOT NULL DEFAULT 0` (1 iff
//	    the member was declared with the open-floor `participant`/`chair`
//	    vocabulary). The two carry, via the `ChannelMessageEvent` wire, the
//	    inputs the agent-side relevance bid reads. A pure addition: every
//	    pre-v7 row reads back as an un-gated legacy `always` member, so a
//	    v0.3.7 database behaves byte-identically.
const channelStoreSchemaVersion = 7

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
//
// PR #335 review M1: `schemaV1SQL` is applied only when `user_version`
// is 0 — i.e. on a truly uninitialised database (or a PR #231-era v1 DB
// that pre-dates version stamping; the `IF NOT EXISTS` guards inside
// `schemaV1SQL` keep that case idempotent). Earlier shapes ran the v1
// baseline unconditionally and relied on `IF NOT EXISTS` to make it a
// no-op. That assumption broke once a later migration dropped something
// `schemaV1SQL` creates: `IF NOT EXISTS` checks by *name*, so a dropped
// index (e.g. `idx_messages_channel_ts`, replaced by v2→v3) is silently
// resurrected on the next open — the database ends up carrying both the
// dropped index and its replacement, in perpetuity, with per-INSERT /
// per-DELETE write amplification. Gating on `current == 0` removes the
// whole class. The
// `TestSQLiteStore_SchemaV3_Reopen_DoesNotResurrectDroppedV2Index` test
// pins the property — extension of the existing
// `..._Migration_Idempotent` test which only checked `user_version`
// stability, not the index set.
func applySchema(db *sql.DB) error {
	// Defensive PRAGMA — DSN sets it, but a future caller wiring a
	// pre-existing *sql.DB through a yet-to-be-added constructor should
	// still see foreign keys enforced. Applied first so it covers the
	// v1-baseline path as well as the migration loop.
	if _, err := db.Exec(`PRAGMA foreign_keys = ON;`); err != nil {
		return fmt.Errorf("channels: enable foreign_keys: %w", err)
	}

	var current int
	if err := db.QueryRow(`PRAGMA user_version;`).Scan(&current); err != nil {
		return fmt.Errorf("channels: read user_version: %w", err)
	}
	// Apply the v1 baseline only on first-time CREATE: PRAGMA reports 0
	// on a fresh database (or a PR #231-era v1 DB that pre-dates
	// stamping). Skipping it for already-stamped databases is what
	// closes M1 — see header for the index-resurrection hazard.
	if current == 0 {
		if _, err := db.Exec(schemaV1SQL); err != nil {
			return fmt.Errorf("channels: apply schema: %w", err)
		}
		// Treat as v1 so the v1→v2 step runs once.
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
