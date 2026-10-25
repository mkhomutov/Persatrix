package channels

// Channels-store v13 → v14: messages by channel in time order again
// (ISSUE-0188, RFC 0029 Phase 3 plan PR 1).
//
// Two things are pinned. The migration itself: a populated v13 store opens at
// v14 with the index and every row intact. And what the index is for: the
// three reads that sort or scan without it — a channel's newest messages, the
// cap prune's oldest, and the scoped window each persona reads on delivery —
// walk it, on a store with planner statistics and on one without. The plan
// tests explain the store's own SQL ([historyQuery], [pruneOldestSQL],
// [scopedHistoryQuery]), so a later migration that drops the index, or a
// query edit the index stops serving, fails here.

import (
	"context"
	"database/sql"
	"fmt"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"
)

// historyReader is the participant every seeded channel has an open
// membership interval for, so the scoped window read returns rows.
const historyReader = "alice-person"

// historyStart is the first seeded message's time; the interval opens before it.
var historyStart = time.Date(2026, 10, 1, 9, 0, 0, 0, time.UTC)

// migrateThroughV13 hand-builds a v13 store at `path`, as a v0.3.x release
// left it, so the next open runs exactly the v13 → v14 step.
func migrateThroughV13(t *testing.T, path string) *sql.DB {
	t.Helper()
	db := migrateThroughV11(t, path)
	require.NoError(t, migrateV11ToV12(db))
	require.NoError(t, migrateV12ToV13(db))
	return db
}

// seedChannelHistory writes `channels` group channels of `perChannel`
// messages each, a second apart, every row on the default session and epoch
// as a live deployment's are, with [historyReader] a member throughout. Raw
// SQL in one transaction, so it runs on a hand-built v13 store too.
func seedChannelHistory(t *testing.T, db *sql.DB, channels, perChannel int) {
	t.Helper()
	tx, err := db.Begin()
	require.NoError(t, err)
	defer func() { _ = tx.Rollback() }()
	for c := range channels {
		ch := fmt.Sprintf("group:room-%d", c)
		_, err := tx.Exec(
			`INSERT INTO channels (id, name, channel_type, description, created_at)
			 VALUES (?, ?, 'group', '', ?)`, ch, strings.TrimPrefix(ch, "group:"), historyStart)
		require.NoError(t, err)
		_, err = tx.Exec(
			`INSERT INTO membership_intervals (channel_id, participant_id, joined_at) VALUES (?, ?, ?)`,
			ch, historyReader, historyStart.Add(-time.Hour))
		require.NoError(t, err)
		for i := range perChannel {
			_, err := tx.Exec(
				`INSERT INTO messages (id, channel_id, sender_id, content, timestamp)
				 VALUES (?, ?, ?, ?, ?)`,
				fmt.Sprintf("%s-msg-%04d", ch, i), ch, historyReader,
				fmt.Sprintf("message %d in %s", i, ch), historyStart.Add(time.Duration(i)*time.Second))
			require.NoError(t, err)
		}
	}
	require.NoError(t, tx.Commit())
}

// messageStatRows counts the planner statistics recorded for `messages`'
// indexes; zero when no ANALYZE has written any, or the table is absent.
func messageStatRows(t *testing.T, db *sql.DB) int {
	t.Helper()
	var tables int
	require.NoError(t, db.QueryRow(
		`SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_stat1'`).Scan(&tables))
	if tables == 0 {
		return 0
	}
	var rows int
	require.NoError(t, db.QueryRow(
		`SELECT COUNT(*) FROM sqlite_stat1 WHERE tbl = 'messages' AND idx IS NOT NULL`).Scan(&rows))
	return rows
}

// queryPlan is `EXPLAIN QUERY PLAN` for q, its detail lines joined.
func queryPlan(t *testing.T, db *sql.DB, q string, args ...any) string {
	t.Helper()
	rows, err := db.Query(`EXPLAIN QUERY PLAN `+q, args...)
	require.NoError(t, err)
	defer func() { _ = rows.Close() }()
	var details []string
	for rows.Next() {
		// (id, parent, notused, detail): only the text is stable across
		// SQLite versions.
		var id, parent, notused int
		var detail string
		require.NoError(t, rows.Scan(&id, &parent, &notused, &detail))
		details = append(details, detail)
	}
	require.NoError(t, rows.Err())
	return strings.Join(details, " | ")
}

// planStore is a store a query-plan test explains its reads on.
type planStore struct {
	name string
	open func(t *testing.T) *sql.DB
}

// planStores are the stores every plan test runs on. Without statistics the
// planner guesses, and the guess is what took the epoch index before v14;
// with them it weighs each index by what it holds, where `idx_messages_epoch`
// matches every row. With one channel, the storage roadmap's ten agents in
// one room, the channel matches every row too, so only the order the index
// gives tells the two apart. Statistics come from ANALYZE, which the store's
// open-time `PRAGMA optimize` runs.
//
// The two reopened stores are what a restart leaves: whatever statistics the
// store's own open gathers from the messages it finds. Statistics from twenty
// messages would plan the cap prune as a scan of the whole table, so the open
// must gather none; [statisticsFloor] messages give the youngest statistics it
// records.
func planStores() []planStore {
	reopened := func(messages int) func(t *testing.T) *sql.DB {
		return func(t *testing.T) *sql.DB {
			path := filepath.Join(t.TempDir(), "channels.db")
			seeded, err := NewSQLiteStore(path, SQLiteOptions{})
			require.NoError(t, err)
			seedChannelHistory(t, seeded.(*sqliteStore).db, 1, messages)
			require.NoError(t, seeded.Close())
			store, err := NewSQLiteStore(path, SQLiteOptions{})
			require.NoError(t, err)
			t.Cleanup(func() { _ = store.Close() })
			return store.(*sqliteStore).db
		}
	}
	analyzed := func(channels int) func(t *testing.T) *sql.DB {
		return func(t *testing.T) *sql.DB {
			store, err := NewSQLiteStore(filepath.Join(t.TempDir(), "channels.db"), SQLiteOptions{})
			require.NoError(t, err)
			t.Cleanup(func() { _ = store.Close() })
			db := store.(*sqliteStore).db
			seedChannelHistory(t, db, channels, 300)
			_, err = db.Exec(`ANALYZE`)
			require.NoError(t, err)
			require.NotZero(t, messageStatRows(t, db), "precondition: statistics recorded")
			return db
		}
	}
	return []planStore{
		{"without statistics", func(t *testing.T) *sql.DB {
			store, err := NewSQLiteStore(filepath.Join(t.TempDir(), "channels.db"), SQLiteOptions{})
			require.NoError(t, err)
			t.Cleanup(func() { _ = store.Close() })
			db := store.(*sqliteStore).db
			require.Zero(t, messageStatRows(t, db), "precondition: no statistics")
			return db
		}},
		{"with statistics", analyzed(4)},
		{"with statistics, one channel", analyzed(1)},
		{"reopened young, with twenty messages", reopened(20)},
		{"reopened at the statistics floor", func(t *testing.T) *sql.DB {
			db := reopened(statisticsFloor)(t)
			require.NotZero(t, messageStatRows(t, db), "precondition: the open gathered statistics")
			return db
		}},
	}
}

// assertWalksChannelTimeIndex fails when the plan sorts the channel or scans
// by epoch — ISSUE-0188's two symptoms — when any step reads every message,
// as the cap prune's outer DELETE does under statistics from a tiny store, or
// when the read goes through another index.
func assertWalksChannelTimeIndex(t *testing.T, plan string) {
	t.Helper()
	assert.NotContains(t, plan, "USE TEMP B-TREE", "the read sorts the channel; plan=%s", plan)
	assert.NotContains(t, plan, "idx_messages_epoch", "the read scans the store by epoch; plan=%s", plan)
	for _, step := range strings.Split(plan, " | ") {
		// `SCAN messages`, `SCAN m` or `SCAN m USING …`; `SCAN mi` is the
		// membership table, whose plan is the RFC 0029 Phase 3 plan's F-3.
		scanned, _, _ := strings.Cut(strings.TrimPrefix(step, "SCAN "), " ")
		assert.False(t, strings.HasPrefix(step, "SCAN ") && (scanned == "messages" || scanned == "m"),
			"the read scans every message (%s); plan=%s", step, plan)
	}
	assert.Contains(t, plan, "idx_messages_channel_time", "the read must walk the channel-time index; plan=%s", plan)
}

// TestQueryPlan_HistoryNewestFirst_WalksTheChannelTimeIndex covers
// GetHistory, both the first page and a page before a cursor.
func TestQueryPlan_HistoryNewestFirst_WalksTheChannelTimeIndex(t *testing.T) {
	for _, s := range planStores() {
		t.Run(s.name, func(t *testing.T) {
			db := s.open(t)
			for _, before := range []time.Time{{}, historyStart.Add(time.Minute)} {
				q, args := historyQuery("group:room-0", 50, before)
				assertWalksChannelTimeIndex(t, queryPlan(t, db, q, args...))
			}
		})
	}
}

// TestQueryPlan_PruneOldestFirst_WalksTheChannelTimeIndex covers the cap
// prune, which runs on every publish once a channel is full.
func TestQueryPlan_PruneOldestFirst_WalksTheChannelTimeIndex(t *testing.T) {
	for _, s := range planStores() {
		t.Run(s.name, func(t *testing.T) {
			db := s.open(t)
			assertWalksChannelTimeIndex(t, queryPlan(t, db, pruneOldestSQL, "group:room-0", 1))
		})
	}
}

// TestQueryPlan_ScopedWindow_WalksTheChannelTimeIndex covers
// GetHistoryScoped, the window each persona reads when a message arrives.
// Its `epoch_id = ?` is what the planner took the epoch index for.
func TestQueryPlan_ScopedWindow_WalksTheChannelTimeIndex(t *testing.T) {
	for _, s := range planStores() {
		t.Run(s.name, func(t *testing.T) {
			db := s.open(t)
			for _, before := range []time.Time{{}, historyStart.Add(time.Minute)} {
				q, args := scopedHistoryQuery("group:room-0", historyReader, 50, before)
				assertWalksChannelTimeIndex(t, queryPlan(t, db, q, args...))
			}
		})
	}
}

// TestSQLiteStore_Migration_V13ToV14_IndexesAPopulatedStore is the upgrade
// an operator runs: a v13 store with history opens at v14 with the index
// built over the rows it already holds, and none of them changed.
func TestSQLiteStore_Migration_V13ToV14_IndexesAPopulatedStore(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	db := migrateThroughV13(t, path)
	seedChannelHistory(t, db, 2, 120)
	require.NotContains(t, tableIndexes(t, db, "messages"), "idx_messages_channel_time",
		"precondition: a v13 store has no channel-time index")
	require.NoError(t, db.Close())

	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	sdb := store.(*sqliteStore).db

	var version int
	require.NoError(t, sdb.QueryRow(`PRAGMA user_version`).Scan(&version))
	assert.Equal(t, channelStoreSchemaVersion, version, "the open runs v14 and every later step")

	// The index's columns, in order, and which way each sorts.
	rows, err := sdb.Query(
		`SELECT name, "desc" FROM pragma_index_xinfo('idx_messages_channel_time') WHERE key = 1 ORDER BY seqno`)
	require.NoError(t, err)
	var columns []string
	for rows.Next() {
		var name string
		var desc int
		require.NoError(t, rows.Scan(&name, &desc))
		columns = append(columns, fmt.Sprintf("%s desc=%d", name, desc))
	}
	require.NoError(t, rows.Err())
	require.NoError(t, rows.Close())
	assert.Equal(t, []string{"channel_id desc=0", "timestamp desc=1"}, columns)

	indexes := tableIndexes(t, sdb, "messages")
	assert.Contains(t, indexes, "idx_messages_channel_session", "v14 adds an index and drops none")
	assert.Contains(t, indexes, "idx_messages_epoch", "v14 adds an index and drops none")
	assert.NotContains(t, indexes, "idx_messages_channel_ts",
		"the index v3 dropped stays dropped; v14's has its own name")

	var count int
	require.NoError(t, sdb.QueryRow(`SELECT COUNT(*) FROM messages`).Scan(&count))
	assert.Equal(t, 240, count, "no row lost")
	var integrity string
	require.NoError(t, sdb.QueryRow(`PRAGMA integrity_check`).Scan(&integrity))
	assert.Equal(t, "ok", integrity)

	ctx := context.Background()
	newest, err := store.GetHistory(ctx, "group:room-1", 3, time.Time{})
	require.NoError(t, err)
	require.Len(t, newest, 3)
	assert.Equal(t, []string{"group:room-1-msg-0119", "group:room-1-msg-0118", "group:room-1-msg-0117"},
		[]string{newest[0].ID, newest[1].ID, newest[2].ID}, "newest first, as before")
	assert.Equal(t, "message 119 in group:room-1", newest[0].Content, "rows unchanged")

	window, err := store.GetHistoryScoped(ctx, "group:room-0", historyReader, 2, time.Time{})
	require.NoError(t, err)
	require.Len(t, window, 2)
	assert.Equal(t, "group:room-0-msg-0119", window[0].ID)
}

// TestMigrateV13ToV14_StampsUserVersionInTransaction is the single-step pin
// every migration carries (PR #335 review L3): the step leaves user_version
// at 14 and the index in place, together.
func TestMigrateV13ToV14_StampsUserVersionInTransaction(t *testing.T) {
	db := migrateThroughV13(t, filepath.Join(t.TempDir(), "channels.db"))
	t.Cleanup(func() { _ = db.Close() })

	require.NoError(t, migrateV13ToV14(db))

	var version int
	require.NoError(t, db.QueryRow(`PRAGMA user_version`).Scan(&version))
	assert.Equal(t, 14, version)
	assert.Contains(t, tableIndexes(t, db, "messages"), "idx_messages_channel_time")
}

// TestSQLiteStore_SchemaV14_Migration_Idempotent pins that a reopen is a
// no-op and keeps the index. The literal latest-version pin moved to the v15
// test, which owns it as the newest migration's.
func TestSQLiteStore_SchemaV14_Migration_Idempotent(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")

	store1, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	require.NoError(t, store1.Close())

	store2, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store2.Close() })

	withDB(t, path, func(db *sql.DB) {
		var version int
		require.NoError(t, db.QueryRow(`PRAGMA user_version`).Scan(&version))
		assert.Equal(t, channelStoreSchemaVersion, version,
			"reopen is a no-op: the store settles at the const")
		assert.Contains(t, tableIndexes(t, db, "messages"), "idx_messages_channel_time",
			"the index survives the reopen")
	})
}

// TestSQLiteStore_Open_GathersPlannerStatistics pins the open-time
// `PRAGMA optimize` and its floor: a store one message short of
// [statisticsFloor] reopens with no statistics, and once it holds the floor
// the next open records them for its message indexes, so the planner weighs
// them by what they hold.
func TestSQLiteStore_Open_GathersPlannerStatistics(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store1, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	seedChannelHistory(t, store1.(*sqliteStore).db, 2, statisticsFloor/2)
	_, err = store1.(*sqliteStore).db.Exec(`DELETE FROM messages WHERE id = 'group:room-1-msg-0000'`)
	require.NoError(t, err)
	require.NoError(t, store1.Close())

	short, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	db := short.(*sqliteStore).db
	assert.Zero(t, messageStatRows(t, db), "one message short of the floor, the open gathers no statistics")
	_, err = db.Exec(`INSERT INTO messages (id, channel_id, sender_id, content, timestamp) VALUES (?, ?, ?, ?, ?)`,
		"group:room-1-msg-0000", "group:room-1", historyReader, "back again", historyStart)
	require.NoError(t, err)
	require.NoError(t, short.Close())

	store3, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store3.Close() })

	var stat string
	require.NoError(t, store3.(*sqliteStore).db.QueryRow(
		`SELECT stat FROM sqlite_stat1 WHERE tbl = 'messages' AND idx = 'idx_messages_epoch'`).Scan(&stat),
		"at the floor, the open must record statistics for the message indexes")
	assert.Equal(t, fmt.Sprintf("%[1]d %[1]d", statisticsFloor), stat,
		"every row shares the one epoch, which is what the statistics tell the planner")
}

// TestSQLiteStore_Open_ReanalyzesATableThatGrewTenfold pins the 0x10000 bit
// of the open-time mask. Statistics taken while a store was young would
// otherwise stay until something queried the table on the opening
// connection, which at open nothing has: plain `PRAGMA optimize`, or 0x02
// alone, leaves the young numbers in place. The store's own open takes none
// below [statisticsFloor], so the young ones here come from an ANALYZE run by
// hand, as an operator's might.
func TestSQLiteStore_Open_ReanalyzesATableThatGrewTenfold(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	epochStat := func(store ChannelStore) string {
		var stat string
		require.NoError(t, store.(*sqliteStore).db.QueryRow(
			`SELECT stat FROM sqlite_stat1 WHERE tbl = 'messages' AND idx = 'idx_messages_epoch'`).Scan(&stat))
		return stat
	}

	young := statisticsFloor / 20
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	db := store.(*sqliteStore).db
	seedChannelHistory(t, db, 1, young)
	_, err = db.Exec(`ANALYZE`)
	require.NoError(t, err)
	require.Equal(t, fmt.Sprintf("%[1]d %[1]d", young), epochStat(store), "precondition: the young store's statistics")
	tx, err := db.Begin()
	require.NoError(t, err)
	for i := young; i < statisticsFloor; i++ {
		_, err := tx.Exec(`INSERT INTO messages (id, channel_id, sender_id, content, timestamp) VALUES (?, ?, ?, ?, ?)`,
			fmt.Sprintf("late-%04d", i), "group:room-0", historyReader, "later", historyStart.Add(time.Duration(i)*time.Second))
		require.NoError(t, err)
	}
	require.NoError(t, tx.Commit())
	require.NoError(t, store.Close())

	reopened, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = reopened.Close() })
	assert.Equal(t, fmt.Sprintf("%[1]d %[1]d", statisticsFloor), epochStat(reopened),
		"a table twenty times the size it was analyzed at is analyzed again when the store opens")
}

// TestSQLiteStore_Open_SurvivesAFailedOptimize pins that the statistics are
// advice, not schema: when another connection holds the write lock past
// busy_timeout, the open logs a warning and the store still works.
func TestSQLiteStore_Open_SurvivesAFailedOptimize(t *testing.T) {
	t.Parallel() // waits out the store's five-second busy_timeout
	path := filepath.Join(t.TempDir(), "channels.db")
	store1, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	seedChannelHistory(t, store1.(*sqliteStore).db, 1, statisticsFloor)
	require.NoError(t, store1.Close())

	holder, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	t.Cleanup(func() { _ = holder.Close() })
	holder.SetMaxOpenConns(1)
	lock, err := holder.Begin()
	require.NoError(t, err)
	_, err = lock.Exec(`UPDATE messages SET content = content WHERE id = 'group:room-0-msg-0000'`)
	require.NoError(t, err, "take the write lock and hold it")

	core, logs := observer.New(zap.WarnLevel)
	store2, err := NewSQLiteStore(path, SQLiteOptions{Logger: zap.New(core)})
	require.NoError(t, lock.Rollback())
	require.NoError(t, err, "a store whose statistics could not be gathered still opens")
	t.Cleanup(func() { _ = store2.Close() })

	assert.Equal(t, 1, logs.FilterMessageSnippet("planner statistics").Len(), "the failure is logged")
	got, err := store2.GetHistory(context.Background(), "group:room-0", 1, time.Time{})
	require.NoError(t, err)
	assert.Len(t, got, 1)
}
