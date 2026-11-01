package channels

// Planner statistics for the channels store: what SQLite's query planner
// knows about how many rows each table and index holds, which decides
// whether a read walks an index or scans a table. The store gathers them
// when it opens and refreshes them every hour while it stays open, as SQLite
// advises for a long-lived connection (RFC 0029 Phase 3 plan PRs 1 and 5).

import (
	"database/sql"
	"time"

	"go.uber.org/zap"
)

// statisticsFloor is how many messages a store must hold before it gathers
// planner statistics, at open or at a refresh. Statistics from a smaller
// store mislead the planner on the publish path: from 70 messages down it
// plans the cap prune ([pruneOldestSQL]) as a scan of the whole table, where
// a larger store finds each row it deletes by id, and the store keeps those
// numbers until the table grows tenfold. A deployment that took them early
// and then ran to the cap would pay ~11 ms a publish at 50 000 messages,
// against ~1 ms. Below the floor the store runs without statistics, where
// all three channel-time reads walk their index. The floor sits more than
// ten times above the turn, measured on SQLite 3.53.4 with one to fifty
// channels.
const statisticsFloor = 1000

// statisticsRefreshInterval is how often an open store refreshes its planner
// statistics. A refresh that finds nothing to analyze costs one row count and
// a brief write lock, so hourly is cheap, and it keeps a table's numbers no
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
// 0x02 alone would analyze only tables with an unanalyzed index; 0x10000 adds
// the tenfold check, which SQLite otherwise makes only for tables this
// connection has queried — at open, none. The mask leaves out 0x10's analysis
// limit, so the statistics are exact: a limited ANALYZE samples ~2 000 index
// entries and would record the epoch index as about as selective as a
// channel. A full one took ~0.2 s per 200 000 messages on the development
// Mac, and runs only when a table needs one.
//
// Every form of the pragma takes the write lock, even when it analyzes
// nothing, so a call waits under busy_timeout for any other writer to the
// file. Only the orchestrator opens it.
//
// The floor counts messages alone. Once it is met, the other tables are
// analyzed at whatever size they have, a handful of membership intervals or
// society approvals included; the hourly refresh ([sqliteStore.refreshLoop])
// re-analyzes each one after it grows tenfold, so those young numbers do not
// outlive the next hour.
func gatherPlannerStatistics(db *sql.DB) error {
	var held int
	if err := db.QueryRow(`SELECT COUNT(*) FROM (SELECT 1 FROM messages LIMIT ?)`,
		statisticsFloor).Scan(&held); err != nil {
		return err
	}
	if held < statisticsFloor {
		return nil
	}
	_, err := db.Exec(`PRAGMA optimize=0x10002`)
	return err
}

// statisticsSeam stands in for the refresh loop's ticker in tests: each value
// on tick runs one refresh, whose error is then sent on refreshed. Only this
// package's tests set it, through [SQLiteOptions].
type statisticsSeam struct {
	tick      <-chan time.Time
	refreshed chan<- error
}

// startRefreshLoop starts the hourly refresh; [sqliteStore.Close] stops it.
func (s *sqliteStore) startRefreshLoop(seam *statisticsSeam) {
	s.refreshStop = make(chan struct{})
	s.refreshDone = make(chan struct{})
	go s.refreshLoop(seam)
}

// refreshLoop runs [gatherPlannerStatistics] on every tick until the store
// closes. A refresh queues on the store's one connection like any other call,
// so it never runs inside a channel or society transaction; one that needs a
// full ANALYZE holds the connection for it, which happens only when a table
// has grown or shrunk tenfold. A failed refresh is logged and the next tick
// tries again: statistics are advice, and every read answers without them.
//
// SQLite suggests plain `PRAGMA optimize` for the periodic call. That form
// sets the 0x10 analysis limit, which would record the epoch index as about
// as selective as a channel, so the refresh keeps the open's exact mask and
// its floor.
func (s *sqliteStore) refreshLoop(seam *statisticsSeam) {
	defer close(s.refreshDone)
	var tick <-chan time.Time
	var refreshed chan<- error
	if seam != nil {
		tick, refreshed = seam.tick, seam.refreshed
	} else {
		ticker := time.NewTicker(statisticsRefreshInterval)
		defer ticker.Stop()
		tick = ticker.C
	}
	for {
		select {
		case <-s.refreshStop:
			return
		case <-tick:
		}
		err := gatherPlannerStatistics(s.db)
		if err != nil {
			s.logger.Warn("channels: refresh planner statistics failed; the next refresh retries",
				zap.Error(err))
		}
		if refreshed != nil {
			select {
			case refreshed <- err:
			case <-s.refreshStop:
				return
			}
		}
	}
}

// stopRefreshLoop ends the refresh loop and waits for it to return, so no
// refresh runs on a closed connection. Safe to call more than once, and a
// no-op on a store whose loop never started.
func (s *sqliteStore) stopRefreshLoop() {
	if s.refreshStop == nil {
		return
	}
	s.refreshStopOnce.Do(func() { close(s.refreshStop) })
	<-s.refreshDone
}
