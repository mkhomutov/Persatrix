package channels

// Channel-time index (channelStoreSchemaVersion v13 → v14, ISSUE-0188).
// Lives in its own sibling file per the convention in sqlite_migrations.go.

import (
	"database/sql"
	"fmt"
)

// channelTimeIndexDDL indexes messages by channel in time order: the order a
// channel's history is read in, newest first, and the order its cap prune
// deletes in, oldest first.
const channelTimeIndexDDL = `CREATE INDEX IF NOT EXISTS idx_messages_channel_time
    ON messages(channel_id, timestamp DESC)`

// migrateV13ToV14 gives the channels store back the index it lost at v3.
//
// v2 → v3 (RFC 0031's session migration) dropped `idx_messages_channel_ts`
// for `idx_messages_channel_session(channel_id, session_id, timestamp DESC)`,
// on the reasoning that its leading `channel_id` kept the history scan cheap.
// It did not: with `session_id` between them, that index orders a channel's
// rows by session first, so three hot reads lost their order. A channel's
// newest messages ([historyQuery]) and the cap prune's oldest
// ([pruneOldestSQL]) sort the whole channel; and the scoped window every
// persona reads on delivery ([scopedHistoryQuery]) took v6's
// `idx_messages_epoch` instead, whose one value `live` matches every row in
// the store. The v0.4.0 storage benchmark measured a full channel's window
// read at ~47 ms and its publish at ~12 ms, against 0.2 ms and 1.2 ms with
// this index, and SQLite holds a society only with it (RFC 0029 Phase 3
// plan, S-1).
//
// No query changes: with this index the planner walks it for all three
// reads, both without statistics and with the ones the store gathers. The
// store's open-time `PRAGMA optimize` ([gatherPlannerStatistics]) gives it
// statistics that show `idx_messages_epoch` matching everything, so no
// epoch-filtered read falls back to walking the store, but only once it
// holds [statisticsFloor] messages: statistics from a smaller store would
// turn the cap prune into a scan of the whole table.
//
// The index takes a new name, not v1's, so the v3 tests that pin
// `idx_messages_channel_ts` staying dropped keep their meaning. Building it
// reads every message once, on the first open after the upgrade: about a
// quarter of a second per 200 000 messages on the development Mac. A pure
// addition — no row, column or other index is touched, and `messages_fts`
// (v10) indexes `content` only. One transaction stamps the version with the
// index (PR #335 review L3); `IF NOT EXISTS` tolerates a store someone
// indexed by hand under this name.
func migrateV13ToV14(db *sql.DB) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	if _, err := tx.Exec(channelTimeIndexDDL); err != nil {
		return fmt.Errorf("create idx_messages_channel_time: %w", err)
	}
	if err := stampUserVersionTx(tx, 14); err != nil {
		return err
	}
	return tx.Commit()
}
