package channels

// The planner statistics are refreshed while the store stays open, not only
// when it opens (RFC 0029 Phase 3 plan PR 5; PR 1 review F-3, PR 2 review
// F-28). The open takes statistics once the store holds [statisticsFloor]
// messages, whatever the other tables hold then: a room's membership
// intervals or the society's approvals may number a handful, and numbers that
// young turn the scoped read's membership check, or the expiry sweep, into a
// scan of the table. The hourly refresh re-analyzes a table once it has grown
// tenfold, so the planner's numbers stay within a factor of ten of the
// table's size without waiting for a restart.

import (
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

// refreshProbe drives a store's statistics refresh by hand: each tick runs
// one refresh, and its result comes back on refreshed.
type refreshProbe struct {
	tick      chan time.Time
	refreshed chan error
}

func newRefreshProbe() *refreshProbe {
	return &refreshProbe{tick: make(chan time.Time), refreshed: make(chan error, 1)}
}

func (p *refreshProbe) options(logger *zap.Logger) SQLiteOptions {
	return SQLiteOptions{
		Logger:         logger,
		statisticsSeam: &statisticsSeam{tick: p.tick, refreshed: p.refreshed},
	}
}

// refresh runs one refresh and returns its error.
func (p *refreshProbe) refresh(t *testing.T) error {
	t.Helper()
	select {
	case p.tick <- time.Now():
	case <-time.After(10 * time.Second):
		t.Fatal("the statistics refresh loop is not listening")
	}
	select {
	case err := <-p.refreshed:
		return err
	case <-time.After(10 * time.Second):
		t.Fatal("the statistics refresh did not finish")
		return nil
	}
}

// statRows is the row count sqlite_stat1 records for an index: the first of
// its numbers. Zero when the index has no statistics.
func statRows(t *testing.T, db *sql.DB, index string) int {
	t.Helper()
	var stat string
	err := db.QueryRow(`SELECT stat FROM sqlite_stat1 WHERE idx = ?`, index).Scan(&stat)
	if err == sql.ErrNoRows {
		return 0
	}
	require.NoError(t, err)
	var rows int
	_, err = fmt.Sscan(strings.Fields(stat)[0], &rows)
	require.NoError(t, err)
	return rows
}

// addMembershipIntervals opens n more intervals in group:room-0, one per new
// participant.
func addMembershipIntervals(t *testing.T, db *sql.DB, from, n int) {
	t.Helper()
	for i := from; i < from+n; i++ {
		_, err := db.Exec(
			`INSERT INTO membership_intervals (channel_id, participant_id, joined_at) VALUES (?, ?, ?)`,
			"group:room-0", fmt.Sprintf("listener-%03d", i), historyStart)
		require.NoError(t, err)
	}
}

// addApprovals opens n more graph-change approvals, each its own actor and
// hash, as the queue holds them.
func addApprovals(t *testing.T, db *sql.DB, from, n int) {
	t.Helper()
	for i := from; i < from+n; i++ {
		_, err := db.Exec(
			`INSERT INTO society_approvals (id, kind, actor_id, action_hash, nonce, expires_at, created_at)
			 VALUES (?, 'graph_change', ?, ?, ?, ?, ?)`,
			fmt.Sprintf("hold-%03d", i), fmt.Sprintf("operator-%03d", i), fmt.Sprintf("hash-%03d", i),
			fmt.Sprintf("nonce-%03d", i), historyStart.Add(time.Hour).Unix(), historyStart.Unix())
		require.NoError(t, err)
	}
}

// TestSQLiteStore_StatisticsRefresh_ReanalyzesATableThatGrewTenfold pins the
// two findings the refresh answers, one table each: the store opens at the
// floor with two rows in the table, which the open analyzes; the table then
// grows twenty-onefold while the store stays open, and the next refresh
// records its new size.
func TestSQLiteStore_StatisticsRefresh_ReanalyzesATableThatGrewTenfold(t *testing.T) {
	cases := []struct {
		name   string
		index  string
		seeded int // rows seedChannelHistory leaves in the table
		add    func(t *testing.T, db *sql.DB, from, n int)
	}{
		{"PR 1 F-3: a room's membership intervals", "idx_membership_intervals_lookup", 1, addMembershipIntervals},
		{"PR 2 F-28: the society's approvals", "idx_society_approvals_state_expiry", 0, addApprovals},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "channels.db")
			seed, err := NewSQLiteStore(path, SQLiteOptions{})
			require.NoError(t, err)
			seedDB := seed.(*sqliteStore).db
			seedChannelHistory(t, seedDB, 1, statisticsFloor)
			tc.add(t, seedDB, 0, 2-tc.seeded)
			require.NoError(t, seed.Close())

			probe := newRefreshProbe()
			store, err := NewSQLiteStore(path, probe.options(nil))
			require.NoError(t, err)
			t.Cleanup(func() { _ = store.Close() })
			db := store.(*sqliteStore).db
			require.Equal(t, 2, statRows(t, db, tc.index), "precondition: the open analyzes the table at two rows")

			tc.add(t, db, 2, 40)
			require.NoError(t, probe.refresh(t))
			assert.Equal(t, 42, statRows(t, db, tc.index),
				"a table twenty-one times the size it was analyzed at is analyzed again while the store stays open")
		})
	}
}

// TestSQLiteStore_StatisticsRefresh_KeepsTheFloor pins that the refresh takes
// no statistics the open would not: below [statisticsFloor] messages it
// gathers none, so a young store's numbers never plan the cap prune as a scan
// (PR 1 review F-11).
func TestSQLiteStore_StatisticsRefresh_KeepsTheFloor(t *testing.T) {
	probe := newRefreshProbe()
	store, err := NewSQLiteStore(filepath.Join(t.TempDir(), "channels.db"), probe.options(nil))
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	db := store.(*sqliteStore).db
	seedChannelHistory(t, db, 1, statisticsFloor-1)

	require.NoError(t, probe.refresh(t))
	assert.Zero(t, messageStatRows(t, db), "one message short of the floor, the refresh gathers no statistics")
}

// TestSQLiteStore_StatisticsRefresh_SurvivesAFailure pins that a refresh that
// cannot take the write lock logs a warning and leaves the loop running, so
// the next refresh gathers what this one could not.
func TestSQLiteStore_StatisticsRefresh_SurvivesAFailure(t *testing.T) {
	t.Parallel() // waits out the store's five-second busy_timeout
	path := filepath.Join(t.TempDir(), "channels.db")
	core, logs := observer.New(zap.WarnLevel)
	probe := newRefreshProbe()
	store, err := NewSQLiteStore(path, probe.options(zap.New(core)))
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	db := store.(*sqliteStore).db
	seedChannelHistory(t, db, 1, statisticsFloor)

	holder, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	t.Cleanup(func() { _ = holder.Close() })
	holder.SetMaxOpenConns(1)
	lock, err := holder.Begin()
	require.NoError(t, err)
	_, err = lock.Exec(`UPDATE messages SET content = content WHERE id = 'group:room-0-msg-0000'`)
	require.NoError(t, err, "take the write lock and hold it")

	assert.Error(t, probe.refresh(t), "the refresh cannot take the write lock")
	assert.Equal(t, 1, logs.FilterMessageSnippet("planner statistics").Len(), "the failure is logged")
	require.NoError(t, lock.Rollback())

	require.NoError(t, probe.refresh(t), "the loop is still running")
	assert.NotZero(t, messageStatRows(t, db), "the next refresh gathers the statistics")
}

// TestSQLiteStore_Close_StopsTheStatisticsRefresh pins that Close ends the
// refresh loop before it closes the connection, so no refresh runs on a
// closed store, and that a second Close is harmless.
func TestSQLiteStore_Close_StopsTheStatisticsRefresh(t *testing.T) {
	probe := newRefreshProbe()
	store, err := NewSQLiteStore(filepath.Join(t.TempDir(), "channels.db"), probe.options(nil))
	require.NoError(t, err)
	require.NoError(t, store.Close())

	select {
	case <-store.(*sqliteStore).refreshDone:
	case <-time.After(10 * time.Second):
		t.Fatal("Close returned with the refresh loop still running")
	}
	select {
	case probe.tick <- time.Now():
		t.Fatal("a closed store's refresh loop took a tick")
	default:
	}
	assert.NoError(t, store.Close(), "a second Close")
}

// TestSQLiteStore_StatisticsRefreshInterval pins the cadence: hourly, as
// SQLite advises for a long-lived connection, so a table's numbers are never
// more than an hour behind its tenfold growth.
func TestSQLiteStore_StatisticsRefreshInterval(t *testing.T) {
	assert.Equal(t, time.Hour, statisticsRefreshInterval)
}
