package channels

// Planner statistics for the channels store: what SQLite's query planner
// knows about how many rows each table and index holds, which decides
// whether a read walks an index or scans a table. The store gathers them
// when it opens and refreshes them every hour while it stays open, as SQLite
// advises for a long-lived connection (RFC 0029 Phase 3 plan PRs 1 and 5).

import (
	"context"
	"database/sql"
	"time"

	"go.uber.org/zap"
)

// statisticsFloor is how many messages a store must hold before it gathers
// planner statistics, at open or at a refresh. Statistics from a smaller
// store mislead the planner on the publish path: from 70 messages down it
// plans the cap prune ([pruneOldestSQL]) as a scan of the whole table, where
// a larger store finds each row it deletes by id, and keeps doing so until
// the table is analyzed again ten times larger. Before the hourly refresh
// that meant until the next restart: a store analyzed young that then ran to
// the cap paid ~11 ms a publish at 50 000 messages, against ~1 ms. Below the
// floor the store runs without statistics, where all three channel-time
// reads walk their index. The floor sits more than ten times above the turn,
// measured on SQLite 3.53.4 with one to fifty channels.
const statisticsFloor = 1000

// statisticsRefreshInterval is how often an open store refreshes its planner
// statistics. A refresh that finds nothing to analyze costs one bounded row
// count and a brief write lock, so hourly is cheap, and it keeps a table's numbers no
// more than an hour behind its tenfold growth.
const statisticsRefreshInterval = time.Hour

// gatherPlannerStatistics runs `PRAGMA optimize=0x10002`, the form SQLite
// advises when a long-lived connection opens, once the store holds
// [statisticsFloor] messages; below that it gathers nothing, reading at most
// the floor's worth of rows to tell. The pragma runs ANALYZE on each table
// with an index that has no statistics, or whose row count has changed
// tenfold since its last ANALYZE, and analyzes nothing otherwise. Without
// statistics the planner guesses each index's selectivity; the guess is what
// sent the scoped window read to `idx_messages_epoch`, whose one value
// matches every message (ISSUE-0188). With them it sees that, so no
// epoch-filtered read walks the whole store. Running after [applySchema] also
// gives a newly built index its statistics, as SQLite advises after a CREATE
// INDEX.
//
// Both statements run on one connection, so no delete between them can
// carry the pragma below the floor.
//
// 0x02 alone would analyze only tables with an unanalyzed index; 0x10000 adds
// the tenfold check, which SQLite otherwise makes only for tables this
// connection has queried — at open, none. The mask leaves out 0x10's analysis
// limit, so the statistics are exact: a limited ANALYZE samples ~2 000 index
// entries and would record the epoch index as about as selective as a
// channel. A full one took ~0.2 s per 200 000 messages on the development
// Mac, and runs only when a table needs one: an index with no statistics yet,
// or a table grown or shrunk tenfold.
//
// Every form of the pragma takes the write lock, even when it analyzes
// nothing, so a call waits under busy_timeout for any other writer to the
// file. Only the orchestrator opens it.
//
// The floor counts messages alone, and gates every table. Once it is met,
// the other tables are analyzed at whatever size they have, so a handful of
// membership intervals or society approvals can be analyzed young: at the
// open, or at the first refresh after a table empty until then gets rows.
// While the store holds the floor, the hourly refresh
// ([sqliteStore.refreshLoop]) analyzes each table again after it grows
// tenfold, so those numbers are out by less than ten times for at most an
// hour. A store that falls back below the floor refreshes nothing, and keeps
// whatever statistics it had until it holds the floor again.
func gatherPlannerStatistics(ctx context.Context, db *sql.DB) error {
	conn, err := db.Conn(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = conn.Close() }()
	var held int
	if err := conn.QueryRowContext(ctx, `SELECT COUNT(*) FROM (SELECT 1 FROM messages LIMIT ?)`,
		statisticsFloor).Scan(&held); err != nil {
		return err
	}
	if held < statisticsFloor {
		return nil
	}
	_, err = conn.ExecContext(ctx, `PRAGMA optimize=0x10002`)
	return err
}

// statisticsSeam stands in for the refresh loop's ticker in tests: each value
// on tick runs one refresh, or, with no tick, a real ticker runs every
// interval; each refresh's error is then sent on refreshed. Only this
// package's tests set it, through [SQLiteOptions].
type statisticsSeam struct {
	tick      <-chan time.Time
	interval  time.Duration
	refreshed chan<- error
}

// startRefreshLoop starts the hourly refresh; [sqliteStore.Close] stops it.
func (s *sqliteStore) startRefreshLoop(seam *statisticsSeam) {
	ctx, cancel := context.WithCancel(context.Background())
	s.refreshCancel = cancel
	s.refreshDone = make(chan struct{})
	go s.refreshLoop(ctx, seam)
}

// refreshLoop runs [gatherPlannerStatistics] on every tick until the store
// closes. A refresh queues on the store's one connection like any other call,
// so it never runs inside a channel or society transaction; one that needs a
// full ANALYZE holds the connection for it. Close cancels the refresh's
// context, which stops it waiting for the connection or interrupts its
// statement, so Close never waits on another holder of the connection. A
// failed refresh is logged and the next tick tries again: statistics are
// advice, and every read answers without them. One that Close stopped is not
// logged.
//
// SQLite suggests plain `PRAGMA optimize` for the periodic call. That form
// sets the 0x10 analysis limit, which would record the epoch index as about
// as selective as a channel, so the refresh keeps the open's exact mask and
// its floor.
func (s *sqliteStore) refreshLoop(ctx context.Context, seam *statisticsSeam) {
	defer close(s.refreshDone)
	interval := statisticsRefreshInterval
	var tick <-chan time.Time
	var refreshed chan<- error
	if seam != nil {
		tick, refreshed = seam.tick, seam.refreshed
		if seam.interval > 0 {
			interval = seam.interval
		}
	}
	if tick == nil {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		tick = ticker.C
	}
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick:
		}
		err := gatherPlannerStatistics(ctx, s.db)
		if err != nil && ctx.Err() == nil {
			s.logger.Warn("channels: refresh planner statistics failed; the next refresh retries",
				zap.Error(err))
		}
		if refreshed != nil {
			select {
			case refreshed <- err:
			case <-ctx.Done():
				return
			}
		}
	}
}

// stopRefreshLoop cancels the refresh loop and waits for it to return, so no
// refresh runs on a closed connection. Safe to call more than once, and a
// no-op on a store whose loop never started.
func (s *sqliteStore) stopRefreshLoop() {
	if s.refreshCancel == nil {
		return
	}
	s.refreshCancel()
	<-s.refreshDone
}
