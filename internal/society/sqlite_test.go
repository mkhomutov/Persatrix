package society

// The society store on the channels store's connection (RFC 0029 Phase 3
// plan PR 3): its constructor, the signing key, the one-transaction call, and
// publishes and society writes sharing the connection. The graph and the
// approval rows have their own test files.

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// testClock is the time the test stores stamp a new row with.
var testClock = time.Unix(1_800_000_000, 0).UTC()

// openChannels opens a channels store at path, at the latest schema.
func openChannels(t *testing.T, path string) channels.ChannelStore {
	t.Helper()
	cs, err := channels.NewSQLiteStore(path, channels.SQLiteOptions{})
	require.NoError(t, err)
	return cs
}

// newTestStore opens a fresh channels store and a society store on its
// connection, with the store's clock fixed at testClock.
func newTestStore(t *testing.T) (*sqliteStore, channels.ChannelStore) {
	t.Helper()
	cs := openChannels(t, filepath.Join(t.TempDir(), "channels.db"))
	t.Cleanup(func() { _ = cs.Close() })
	return storeOn(t, cs), cs
}

// storeOn builds the society store on cs's connection.
func storeOn(t *testing.T, cs channels.ChannelStore) *sqliteStore {
	t.Helper()
	src, ok := cs.(ConnSource)
	require.True(t, ok, "the SQLite channel store hands over its connection")
	st, err := NewSQLiteStore(src)
	require.NoError(t, err)
	s := st.(*sqliteStore)
	s.now = func() time.Time { return testClock }
	return s
}

func TestNewSQLiteStore_RunsOnTheChannelStoresOwnConnection(t *testing.T) {
	s, cs := newTestStore(t)
	assert.Same(t, cs.(ConnSource).SocietyConn(), s.db,
		"the society store must share the channel store's connection, never open its own (S-2)")
}

// connOf hands over a connection the channels store did not make.
type connOf struct{ db *sql.DB }

func (c connOf) SocietyConn() *sql.DB { return c.db }

// openRawDB opens a SQLite file with no channels schema in it.
func openRawDB(t *testing.T, path string) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite", path)
	require.NoError(t, err)
	t.Cleanup(func() { _ = db.Close() })
	return db
}

func TestNewSQLiteStore_RefusesAConnectionWithoutTheSocietyTables(t *testing.T) {
	db := openRawDB(t, filepath.Join(t.TempDir(), "empty.db"))
	_, err := NewSQLiteStore(connOf{db})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "society_meta")
}

func TestKey_IsMadeOnceAcrossConcurrentFirstCalls(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	const callers = 8
	keys := make([]Key, callers)
	errs := make([]error, callers)
	var wg sync.WaitGroup
	for i := range callers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			keys[i], errs[i] = s.Key(ctx, KeyApprovalSigning)
		}()
	}
	wg.Wait()
	for i := range callers {
		require.NoError(t, errs[i])
		assert.Len(t, keys[i], KeySize)
		assert.Equal(t, keys[0], keys[i], "every first call must get the one key")
	}
	var rows int
	require.NoError(t, s.db.QueryRow(`SELECT COUNT(*) FROM society_keys`).Scan(&rows))
	assert.Equal(t, 1, rows)
}

func TestKey_SurvivesAReopen(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	ctx := context.Background()
	cs := openChannels(t, path)
	first, err := storeOn(t, cs).Key(ctx, KeyApprovalSigning)
	require.NoError(t, err)
	require.NoError(t, cs.Close())

	cs = openChannels(t, path)
	t.Cleanup(func() { _ = cs.Close() })
	again, err := storeOn(t, cs).Key(ctx, KeyApprovalSigning)
	require.NoError(t, err)
	assert.Equal(t, first, again, "a restart must keep the key, so earlier tokens still check")
}

func TestKey_IsOnePerPurpose(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	a, err := s.Key(ctx, KeyApprovalSigning)
	require.NoError(t, err)
	b, err := s.Key(ctx, "another-purpose")
	require.NoError(t, err)
	assert.NotEqual(t, a, b)
	_, err = s.Key(ctx, "")
	assert.ErrorIs(t, err, ErrInvalid)
}

func TestKey_NeverFormatsItsBytes(t *testing.T) {
	s, _ := newTestStore(t)
	key, err := s.Key(context.Background(), KeyApprovalSigning)
	require.NoError(t, err)
	hexKey := fmt.Sprintf("%x", []byte(key))

	core, logs := observer.New(zap.DebugLevel)
	zap.New(core).Info("key", zap.Any("key", key), zap.Stringer("stringer", key))
	encoded, err := json.Marshal(map[string]any{"key": key})
	require.NoError(t, err)
	for _, out := range []string{
		fmt.Sprint(key), fmt.Sprintf("%v %+v %#v %s %x %q", key, key, key, key, key, key),
		string(encoded), fmt.Sprint(logs.All()[0].ContextMap()),
	} {
		assert.NotContains(t, out, hexKey)
		assert.NotContains(t, out, string(key))
		assert.Contains(t, out, "redacted")
	}
}

func TestInTx_CommitsAGraphChangeWithItsApproval(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	require.NoError(t, s.ReplaceGraph(ctx, lab()))
	a, err := s.OpenApproval(ctx, graphChange("change-1"))
	require.NoError(t, err)

	require.NoError(t, s.InTx(ctx, func(tx Tx) error {
		g, err := tx.Graph(ctx)
		if err != nil {
			return err
		}
		g.Organizations[0].Manages = append(g.Organizations[0].Manages,
			Manages{ManagerID: "moss-fox", ReportID: "tide-wren", Scope: "experiments"})
		g.Version++
		if err := tx.ReplaceGraph(ctx, g); err != nil {
			return err
		}
		_, err = tx.MoveApproval(ctx, a.ID, Move{From: StatePending, To: StateApproved,
			DecidedBy: "operator-1", DecidedAt: testClock})
		return err
	}))

	g, err := s.Graph(ctx)
	require.NoError(t, err)
	assert.Equal(t, int64(2), g.Version)
	assert.Len(t, g.Organizations[0].Manages, 3)
	got, err := s.Approval(ctx, a.ID)
	require.NoError(t, err)
	assert.Equal(t, StateApproved, got.State)
}

func TestInTx_AnErrorLeavesTheGraphAndTheApprovalUnchanged(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	require.NoError(t, s.ReplaceGraph(ctx, lab()))
	a, err := s.OpenApproval(ctx, graphChange("change-1"))
	require.NoError(t, err)
	refused := errors.New("the change makes a cycle")

	err = s.InTx(ctx, func(tx Tx) error {
		g := lab()
		g.Version = 2
		g.Organizations[0].Manages = nil
		if err := tx.ReplaceGraph(ctx, g); err != nil {
			return err
		}
		if _, err := tx.MoveApproval(ctx, a.ID, Move{From: StatePending, To: StateApproved,
			DecidedBy: "operator-1", DecidedAt: testClock}); err != nil {
			return err
		}
		return refused
	})
	require.ErrorIs(t, err, refused)

	g, err := s.Graph(ctx)
	require.NoError(t, err)
	assert.Equal(t, lab(), g, "a failed transaction must leave the graph as it was")
	got, err := s.Approval(ctx, a.ID)
	require.NoError(t, err)
	assert.Equal(t, StatePending, got.State, "and the approval pending")
	assert.Empty(t, got.DecidedBy)
}

// The benchmark's second finding as a unit test: on the shared connection a
// society write never makes a publish fail, and no society call fails either.
func TestSharedConnection_PublishesAndSocietyWritesRunTogether(t *testing.T) {
	s, cs := newTestStore(t)
	ctx := context.Background()
	const ch = "group:lab"
	members := []channels.Member{{ParticipantID: "ember-owl"}, {ParticipantID: "moss-fox"}}
	require.NoError(t, cs.CreateChannelWithMembers(ctx,
		channels.Channel{ID: ch, Name: "lab", Type: channels.ChannelTypeGroup}, members))
	require.NoError(t, s.ReplaceGraph(ctx, lab()))

	const rounds = 40
	errs := make(chan error, 4*rounds)
	var wg sync.WaitGroup
	for w := range 2 {
		wg.Add(2)
		go func() {
			defer wg.Done()
			for i := range rounds {
				errs <- cs.PublishMessage(ctx, channels.ChannelMessage{
					ID: fmt.Sprintf("m-%d-%d", w, i), ChannelID: ch,
					SenderID: members[w].ParticipantID, Content: "status update",
				})
			}
		}()
		go func() {
			defer wg.Done()
			for i := range rounds {
				errs <- s.InTx(ctx, func(tx Tx) error {
					a, err := tx.OpenApproval(ctx, action(fmt.Sprintf("a-%d-%d", w, i), "moss-fox"))
					if err != nil {
						return err
					}
					if _, err := tx.MoveApproval(ctx, a.ID, Move{From: StatePending, To: StateDenied,
						DecidedBy: "operator-1", DecidedAt: testClock}); err != nil {
						return err
					}
					_, err = tx.Graph(ctx)
					return err
				})
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		require.NoError(t, err)
	}
	history, err := cs.GetHistory(ctx, ch, 4*rounds, time.Time{})
	require.NoError(t, err)
	assert.Len(t, history, 2*rounds)
}
