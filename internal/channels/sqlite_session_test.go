// RFC 0031 Phase 1 PR 2 — store-side session_id round-trip + v2→v3 upgrade.
package channels

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"
	"time"

	_ "modernc.org/sqlite"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
)

// TestSQLiteStore_CreateChannel_PersistsSessionID asserts a caller-supplied
// session_id round-trips through CreateChannel and GetChannel.
func TestSQLiteStore_CreateChannel_PersistsSessionID(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()

	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
		SessionID: "run-a",
	}))

	got, err := store.GetChannel(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, "run-a", got.SessionID)
}

// TestSQLiteStore_CreateChannel_DefaultsToLegacy asserts an empty
// SessionID stamps "legacy" at the store boundary so an older or
// session-unaware caller still produces a queryable row.
func TestSQLiteStore_CreateChannel_DefaultsToLegacy(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()

	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	}))
	got, err := store.GetChannel(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, "legacy", got.SessionID)
}

// TestSQLiteStore_CreateChannelWithMembers_PersistsSessionID mirrors the
// single-channel case for the atomic-create-with-members helper.
func TestSQLiteStore_CreateChannelWithMembers_PersistsSessionID(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()

	require.NoError(t, store.CreateChannelWithMembers(ctx,
		Channel{ID: "group:planning", Name: "planning", Type: ChannelTypeGroup, SessionID: "run-b"},
		[]Member{{ParticipantID: "alice", RespondPolicy: RespondAlways}},
	))
	got, err := store.GetChannel(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, "run-b", got.SessionID)
}

// TestSQLiteStore_GetOrCreateDM_DefaultsToLegacy asserts that DMs created
// implicitly carry the legacy session_id (Phase 3 CLI will let operators
// promote them).
func TestSQLiteStore_GetOrCreateDM_DefaultsToLegacy(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	ch, err := store.GetOrCreateDM(ctx, "agent-a", "agent-b")
	require.NoError(t, err)
	assert.Equal(t, "legacy", ch.SessionID)
}

// TestSQLiteStore_PublishMessage_PersistsSessionID asserts a publish that
// carries an explicit session_id stores it, and a publish that omits it
// inherits "legacy" via the column default.
func TestSQLiteStore_PublishMessage_PersistsSessionID(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice")

	explicitID := uuid.NewString()
	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID: explicitID, ChannelID: id, SenderID: "alice",
		Content: "with-session", SessionID: "run-a",
	}))
	defaultID := uuid.NewString()
	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID: defaultID, ChannelID: id, SenderID: "alice", Content: "no-session",
	}))

	got, err := store.GetMessage(ctx, explicitID)
	require.NoError(t, err)
	assert.Equal(t, "run-a", got.SessionID)

	got2, err := store.GetMessage(ctx, defaultID)
	require.NoError(t, err)
	assert.Equal(t, "legacy", got2.SessionID, "empty SessionID defaults to legacy")
}

// TestSQLiteStore_GetHistory_ReturnsSessionID asserts the history read path
// surfaces session_id on each row so future filter logic (Phase 2) and
// any operator triage tool can inspect the tag without a separate query.
func TestSQLiteStore_GetHistory_ReturnsSessionID(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice")

	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
		Content: "msg", SessionID: "run-a",
	}))

	hist, err := store.GetHistory(ctx, id, 10, time.Time{})
	require.NoError(t, err)
	require.Len(t, hist, 1)
	assert.Equal(t, "run-a", hist[0].SessionID)
}

// TestSQLiteStore_SessionsWritesCounter_IncrementsOnWrites asserts the
// orchestrator-side `sessions.writes` counter (RFC 0031 §F) increments
// once per CreateChannel and once per PublishMessage, with the
// session_id attribute set to the row's session id.
func TestSQLiteStore_SessionsWritesCounter_IncrementsOnWrites(t *testing.T) {
	reader := metric.NewManualReader()
	mp := metric.NewMeterProvider(metric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	meter := mp.Meter("test")
	counter, err := meter.Int64Counter("sessions.writes")
	require.NoError(t, err)

	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{
		SessionMetrics: &SessionMetrics{Writes: counter},
	})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()

	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
		SessionID: "run-a",
	}))
	require.NoError(t, store.AddMember(ctx, "group:planning", "alice", RespondAlways))
	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: "group:planning", SenderID: "alice",
		Content: "hi", SessionID: "run-a",
	}))

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))

	total := sumDataPointForAttr(t, rm, "sessions.writes", "session_id", "run-a")
	assert.Equal(t, int64(2), total,
		"sessions.writes{session_id=run-a} = 1 (CreateChannel) + 1 (PublishMessage)")
}

// TestSQLiteStore_Migration_V2ToV3_LegacyRowsCarryLegacy simulates an
// existing v2 database opened by a v3 binary: existing channels + messages
// are stamped with `session_id='legacy'`, the new index set is in place,
// and the `sessions` table exists empty.
func TestSQLiteStore_Migration_V2ToV3_LegacyRowsCarryLegacy(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")

	// Hand-build a v2-shaped database by running the v1 baseline + the
	// v1→v2 migration directly, then stamp user_version=2 so the v3
	// binary's loop runs only the v2→v3 step.
	db, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	_, err = db.Exec(schemaV1SQL)
	require.NoError(t, err)
	require.NoError(t, migrateV1ToV2(db))
	_, err = db.Exec(`PRAGMA user_version = 2`)
	require.NoError(t, err)

	// Seed v2 rows. The columns are the v2 shape (no session_id).
	_, err = db.Exec(
		`INSERT INTO channels (id, name, channel_type, description, created_at)
		 VALUES ('group:planning', 'planning', 'group', '', datetime('now'))`)
	require.NoError(t, err)
	_, err = db.Exec(
		`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at)
		 VALUES ('group:planning', 'alice', 'always', datetime('now'))`)
	require.NoError(t, err)
	_, err = db.Exec(
		`INSERT INTO messages (id, channel_id, sender_id, content, timestamp)
		 VALUES ('m-1', 'group:planning', 'alice', 'pre-upgrade', datetime('now'))`)
	require.NoError(t, err)
	require.NoError(t, db.Close())

	// Reopen — v2→v3 migration should run, backfilling the new column with
	// the literal default.
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	got, err := store.GetChannel(context.Background(), "group:planning")
	require.NoError(t, err)
	assert.Equal(t, "legacy", got.SessionID, "pre-existing channel rows carry legacy")

	msg, err := store.GetMessage(context.Background(), "m-1")
	require.NoError(t, err)
	assert.Equal(t, "legacy", msg.SessionID, "pre-existing message rows carry legacy")

	withDB(t, path, func(db *sql.DB) {
		var version int
		require.NoError(t, db.QueryRow(`PRAGMA user_version`).Scan(&version))
		assert.Equal(t, 3, version)

		// Sessions table is present and empty.
		var n int
		require.NoError(t, db.QueryRow(`SELECT COUNT(1) FROM sessions`).Scan(&n))
		assert.Equal(t, 0, n)
	})
}

// sumDataPointForAttr collects the Sum[int64] data point for `metric` whose
// attribute set contains `attrKey=attrVal` and returns the cumulative value.
// Returns 0 if no matching point is found.
func sumDataPointForAttr(t *testing.T, rm metricdata.ResourceMetrics, name, attrKey, attrVal string) int64 {
	t.Helper()
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != name {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			if !ok {
				t.Fatalf("metric %s is not Sum[int64]: %T", name, m.Data)
			}
			for _, dp := range sum.DataPoints {
				v, ok := dp.Attributes.Value(attribute.Key(attrKey))
				if !ok {
					continue
				}
				if v.AsString() == attrVal {
					return dp.Value
				}
			}
		}
	}
	return 0
}
