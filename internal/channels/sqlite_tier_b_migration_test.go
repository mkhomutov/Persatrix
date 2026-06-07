// RFC 0030 Tier B (v0.3.8) PR 2b — the `memberships.threshold` +
// `memberships.tier_b_active` columns migration (channelStoreSchemaVersion
// v6 → v7), plus the store-API round-trip for the two new per-member fields.
//
// The migration is forward-only and a pure addition:
//   - `threshold REAL` is nullable (no DEFAULT) so every pre-v7 row reads back
//     as NULL → unset → bias-to-silence (the conservative Tier B default).
//   - `tier_b_active INTEGER NOT NULL DEFAULT 0` backfills every pre-v7 row to
//     0 (a legacy `always` member that keeps replying unconditionally), so the
//     feature is additive and a v0.3.7 database behaves byte-identically.
//
// The per-member signal is what makes Tier B governable per-member rather than
// per-channel: only members declared with the open-floor participant
// vocabulary (`participant`/`chair`) carry `tier_b_active = 1` and run the
// salience bid. The store persists the resolved Member fields verbatim; the
// disposition → (tier_b_active, threshold) derivation happens at the config /
// REST boundary that still sees the disposition (see [resolveTierBSignal]).
package channels

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"

	_ "modernc.org/sqlite"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestSQLiteStore_SchemaV7_FreshDB_MembershipsHasTierBColumns asserts the
// memberships table grows the two Tier B columns on a fresh database.
func TestSQLiteStore_SchemaV7_FreshDB_MembershipsHasTierBColumns(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		cols := tableColumns(t, db, "memberships")
		assert.Contains(t, cols, "threshold", "memberships.threshold missing")
		assert.Contains(t, cols, "tier_b_active", "memberships.tier_b_active missing")
	})
}

// TestSQLiteStore_SchemaV7_Migration_Idempotent pins the schema version at the
// newest migration. Reopening the same file is a no-op (no duplicate-column
// error, user_version stable at the latest).
func TestSQLiteStore_SchemaV7_Migration_Idempotent(t *testing.T) {
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
			"user_version stamped to the latest schema version; reopen is a no-op")
		assert.Equal(t, 7, channelStoreSchemaVersion, "RFC 0030 Tier B PR 2b bumps the channel store to v7")
	})
}

// TestSQLiteStore_Migration_V6ToV7_PreservesRows pins the data migration: an
// existing v6 database whose `memberships` rows pre-date the Tier B columns is
// carried forward unchanged, picking up `threshold = NULL` (unset) and
// `tier_b_active = 0` (legacy always) — no data loss, v0.3.7 behaviour intact.
func TestSQLiteStore_Migration_V6ToV7_PreservesRows(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")

	// Hand-build a v6-shaped database: baseline + v1→v2 … v5→v6, then stamp
	// user_version=6 so the v7 binary's loop runs only the v6→v7 step.
	db, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	_, err = db.Exec(schemaV1SQL)
	require.NoError(t, err)
	require.NoError(t, migrateV1ToV2(db))
	require.NoError(t, migrateV2ToV3(db))
	require.NoError(t, migrateV3ToV4(db))
	require.NoError(t, migrateV4ToV5(db))
	require.NoError(t, migrateV5ToV6(db))
	_, err = db.Exec(`PRAGMA user_version = 6`)
	require.NoError(t, err)

	// A pre-v7 group channel + a membership row (no threshold / tier_b_active
	// columns yet).
	_, err = db.Exec(
		`INSERT INTO channels (id, name, channel_type, description, created_at, session_id, epoch_id)
		   VALUES ('group:planning', 'planning', 'group', '', '2026-01-01T00:00:00Z', 'legacy', 'live')`)
	require.NoError(t, err)
	_, err = db.Exec(
		`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at)
		   VALUES ('group:planning', 'alice', 'always', '2026-01-01T00:00:00Z')`)
	require.NoError(t, err)
	require.NoError(t, db.Close())

	// Reopen — the v6→v7 migration runs and adds the columns.
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		var version int
		require.NoError(t, db.QueryRow(`PRAGMA user_version`).Scan(&version))
		assert.Equal(t, 7, version, "v6→v7 ran")

		var threshold sql.NullFloat64
		var tierB int
		require.NoError(t, db.QueryRow(
			`SELECT threshold, tier_b_active FROM memberships
			   WHERE channel_id = 'group:planning' AND participant_id = 'alice'`).
			Scan(&threshold, &tierB))
		assert.False(t, threshold.Valid, "pre-v7 membership row backfilled to NULL threshold (unset)")
		assert.Equal(t, 0, tierB, "pre-v7 membership row backfilled to tier_b_active=0 (legacy always)")
	})

	// The row is still a member with its original policy — no data loss.
	members, err := store.GetMembers(context.Background(), "group:planning")
	require.NoError(t, err)
	require.Len(t, members, 1)
	assert.Equal(t, "alice", members[0].ParticipantID)
	assert.Equal(t, RespondAlways, members[0].RespondPolicy)
	assert.Nil(t, members[0].Threshold)
	assert.False(t, members[0].TierBActive)
}

// TestSQLiteStore_CreateChannelWithMembers_RoundTripsTierBFields proves the
// per-member Tier B signals persist + read back verbatim through the config /
// REST create path. The store does NOT derive them — the caller sets them.
func TestSQLiteStore_CreateChannelWithMembers_RoundTripsTierBFields(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	thr := 0.3
	require.NoError(t, store.CreateChannelWithMembers(context.Background(),
		Channel{ID: "group:planning", Name: "planning", Type: ChannelTypeGroup},
		[]Member{
			// A participant: salience-gated, unset threshold.
			{ParticipantID: "alice", RespondPolicy: RespondAlways, TierBActive: true},
			// A chair-like participant: salience-gated with an explicit bar.
			{ParticipantID: "bob", RespondPolicy: RespondAlways, TierBActive: true, Threshold: &thr},
			// A legacy always member: NOT salience-gated.
			{ParticipantID: "carol", RespondPolicy: RespondAlways},
		}))

	members, err := store.GetMembers(context.Background(), "group:planning")
	require.NoError(t, err)
	byID := map[string]Member{}
	for _, m := range members {
		byID[m.ParticipantID] = m
	}

	assert.True(t, byID["alice"].TierBActive)
	assert.Nil(t, byID["alice"].Threshold, "unset threshold round-trips as nil, not 0.0")

	assert.True(t, byID["bob"].TierBActive)
	require.NotNil(t, byID["bob"].Threshold)
	assert.Equal(t, 0.3, *byID["bob"].Threshold)

	assert.False(t, byID["carol"].TierBActive, "legacy always member is not salience-gated")
	assert.Nil(t, byID["carol"].Threshold)
}

// TestSQLiteStore_AddMember_DerivesTierBFromDisposition pins the REST
// single-add path: AddMember receives the raw disposition and derives the
// persisted Tier B signals from it (participant/chair → gated; chair → low
// default threshold; legacy always → not gated).
func TestSQLiteStore_AddMember_DerivesTierBFromDisposition(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	ctx := context.Background()
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	}))

	require.NoError(t, store.AddMember(ctx, "group:planning", "alice", RespondParticipant))
	require.NoError(t, store.AddMember(ctx, "group:planning", "bob", RespondChair))
	require.NoError(t, store.AddMember(ctx, "group:planning", "carol", RespondAlways))

	got := func(id string) Member {
		m, err := store.GetMember(ctx, "group:planning", id)
		require.NoError(t, err)
		return m
	}

	alice := got("alice")
	assert.Equal(t, RespondAlways, alice.RespondPolicy, "participant normalizes to always on the wire")
	assert.True(t, alice.TierBActive, "participant is salience-gated")
	assert.Nil(t, alice.Threshold, "a plain participant carries no explicit threshold")

	bob := got("bob")
	assert.True(t, bob.TierBActive, "chair is salience-gated")
	require.NotNil(t, bob.Threshold)
	assert.Equal(t, DefaultChairThreshold, *bob.Threshold, "chair picks up the low default threshold")

	carol := got("carol")
	assert.False(t, carol.TierBActive, "legacy always member is not salience-gated")
	assert.Nil(t, carol.Threshold)
}
