// RFC 0050 Phase 1 PR 1 — the persisted per-channel governance override
// storage layer: the channel-store schema migration (v7 → v8) that lands the
// `config_overrides_json` / `config_revision` / `config_change_lineage`
// columns, plus the [ChannelStore.GetChannelConfig] / [PutChannelConfig]
// accessors and their optimistic-concurrency contract.
//
// PR 1 is storage only: the overrides are written and read but not yet
// consulted by the router (PR 2's apply path) — so these tests assert the
// persistence/round-trip/revision properties, not any runtime behaviour.
package channels

import (
	"context"
	"database/sql"
	"errors"
	"path/filepath"
	"testing"

	_ "modernc.org/sqlite"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestSQLiteStore_SchemaV8_FreshDB_ChannelsHasConfigColumns asserts the
// `channels` table grows the three RFC 0050 columns on a fresh database.
func TestSQLiteStore_SchemaV8_FreshDB_ChannelsHasConfigColumns(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		cols := tableColumns(t, db, "channels")
		assert.Contains(t, cols, "config_overrides_json", "channels.config_overrides_json missing")
		assert.Contains(t, cols, "config_revision", "channels.config_revision missing")
		assert.Contains(t, cols, "config_change_lineage", "channels.config_change_lineage missing")
	})
}

// TestSQLiteStore_SchemaV8_Migration_Idempotent pins the schema version at the
// newest migration. Reopening the same file is a no-op (no duplicate-column
// error, user_version stable at the latest).
func TestSQLiteStore_SchemaV8_Migration_Idempotent(t *testing.T) {
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
		// The literal-version pin moved to the newest migration's test
		// (TestSQLiteStore_SchemaV9_Migration_Idempotent) per the convention the
		// v5..v8 test headers document; this test now only asserts that a reopen
		// is a no-op at whatever the latest version is.
	})
}

// TestSQLiteStore_Migration_V7ToV8_PreservesRows pins the data migration: an
// existing v7 database whose `channels` rows pre-date the RFC 0050 columns is
// carried forward unchanged, every row picking up config_revision=0 (seed-only
// under the revision gate), a NULL overrides blob (inherit-all) and a NULL
// lineage — no data loss, v0.3.8 behaviour intact.
func TestSQLiteStore_Migration_V7ToV8_PreservesRows(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")

	// Hand-build a v7-shaped database: baseline + v1→v2 … v6→v7, then stamp
	// user_version=7 so the v8 binary's loop runs only the v7→v8 step.
	db, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	_, err = db.Exec(schemaV1SQL)
	require.NoError(t, err)
	require.NoError(t, migrateV1ToV2(db))
	require.NoError(t, migrateV2ToV3(db))
	require.NoError(t, migrateV3ToV4(db))
	require.NoError(t, migrateV4ToV5(db))
	require.NoError(t, migrateV5ToV6(db))
	require.NoError(t, migrateV6ToV7(db))
	_, err = db.Exec(`PRAGMA user_version = 7`)
	require.NoError(t, err)

	// A pre-v8 group channel (no config_* columns yet).
	_, err = db.Exec(
		`INSERT INTO channels (id, name, channel_type, description, created_at, session_id, epoch_id)
		   VALUES ('group:planning', 'planning', 'group', '', '2026-01-01T00:00:00Z', 'legacy', 'live')`)
	require.NoError(t, err)
	require.NoError(t, db.Close())

	// Reopen — the v7→v8 migration runs and adds the columns.
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		var version int
		require.NoError(t, db.QueryRow(`PRAGMA user_version`).Scan(&version))
		// NewSQLiteStore migrates all the way to the latest version, so a
		// hand-built v7 DB lands at channelStoreSchemaVersion (≥9 since the RFC
		// 0035 ledger migration). What this test pins is the v7→v8 config-column
		// backfill below, which the migration chain still applies en route.
		assert.GreaterOrEqual(t, version, 8, "the v7→v8 migration ran (chain continues to latest)")

		var overrides, lineage sql.NullString
		var revision int64
		require.NoError(t, db.QueryRow(
			`SELECT config_overrides_json, config_revision, config_change_lineage
			   FROM channels WHERE id = 'group:planning'`).
			Scan(&overrides, &revision, &lineage))
		assert.False(t, overrides.Valid, "pre-v8 row backfilled to NULL overrides (inherit-all)")
		assert.Equal(t, int64(0), revision, "pre-v8 row backfilled to config_revision=0 (seed-only under the gate)")
		assert.False(t, lineage.Valid, "lineage column ships dormant (NULL)")
	})

	// The row is still a resolvable channel — no data loss.
	ch, err := store.GetChannel(context.Background(), "group:planning")
	require.NoError(t, err)
	assert.Equal(t, "planning", ch.Name)
}

// TestSQLiteStore_GetChannelConfig_AbsentOverrides_IsInheritAll asserts a
// freshly-created channel reads back empty overrides (inherit-all) at revision
// 0 — the never-edited baseline.
func TestSQLiteStore_GetChannelConfig_AbsentOverrides_IsInheritAll(t *testing.T) {
	store, ctx, _ := newConfigStore(t)
	mustCreateGroup(t, store, "planning")

	overrides, revision, err := store.GetChannelConfig(ctx, "group:planning")
	require.NoError(t, err)
	assert.True(t, overrides.IsEmpty(), "an unedited channel inherits all knobs")
	assert.Equal(t, int64(0), revision, "an unedited channel sits at revision 0")
}

// TestSQLiteStore_PutChannelConfig_RoundTrips proves the sparse, tri-state
// override set persists and reads back verbatim, and that the revision is
// bumped on write.
func TestSQLiteStore_PutChannelConfig_RoundTrips(t *testing.T) {
	store, ctx, _ := newConfigStore(t)
	mustCreateGroup(t, store, "planning")

	fc := false
	maxMembers := 12
	budget := int64(50_000)
	idle := 0 // explicit "idle rotation off" — distinct from absent
	want := ChannelConfigOverrides{
		FloorControl:                  &fc,
		SalienceMaxChannelMembers:     &maxMembers,
		InteractionBudgetTokens:       &budget,
		InteractionIdleTimeoutSeconds: &idle,
	}
	require.NoError(t, store.PutChannelConfig(ctx, "group:planning", want, 0, ""))

	got, revision, err := store.GetChannelConfig(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, int64(1), revision, "first apply bumps revision 0 → 1")
	require.NotNil(t, got.FloorControl)
	assert.False(t, *got.FloorControl, "explicit floor_control:false round-trips (not collapsed to absent)")
	require.NotNil(t, got.SalienceMaxChannelMembers)
	assert.Equal(t, 12, *got.SalienceMaxChannelMembers)
	require.NotNil(t, got.InteractionBudgetTokens)
	assert.Equal(t, int64(50_000), *got.InteractionBudgetTokens)
	require.NotNil(t, got.InteractionIdleTimeoutSeconds)
	assert.Equal(t, 0, *got.InteractionIdleTimeoutSeconds, "explicit idle=0 round-trips, distinct from absent")
	// The unset knobs stay unset (inherit).
	assert.Nil(t, got.EndVoteThreshold, "an unset knob stays absent → inherit")
	assert.Nil(t, got.EscalationChairID)
}

// TestSQLiteStore_PutChannelConfig_RevisionMonotonic asserts each successful
// apply bumps the revision by exactly one.
func TestSQLiteStore_PutChannelConfig_RevisionMonotonic(t *testing.T) {
	store, ctx, _ := newConfigStore(t)
	mustCreateGroup(t, store, "planning")

	k := 3
	for want := int64(1); want <= 3; want++ {
		require.NoError(t, store.PutChannelConfig(ctx, "group:planning",
			ChannelConfigOverrides{EndVoteThreshold: &k}, want-1, ""))
		_, revision, err := store.GetChannelConfig(ctx, "group:planning")
		require.NoError(t, err)
		assert.Equal(t, want, revision)
	}
}

// TestSQLiteStore_PutChannelConfig_StaleRevisionConflict pins the optimistic-
// concurrency primitive: a Put with an expectedRevision that no longer matches
// the store returns a typed conflict error and writes nothing.
func TestSQLiteStore_PutChannelConfig_StaleRevisionConflict(t *testing.T) {
	store, ctx, _ := newConfigStore(t)
	mustCreateGroup(t, store, "planning")

	maxMembers := 9
	require.NoError(t, store.PutChannelConfig(ctx, "group:planning",
		ChannelConfigOverrides{SalienceMaxChannelMembers: &maxMembers}, 0, ""))
	// Store is now at revision 1. A writer that still believes it is 0 loses.
	other := 99
	err := store.PutChannelConfig(ctx, "group:planning",
		ChannelConfigOverrides{SalienceMaxChannelMembers: &other}, 0, "")
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrConfigRevisionConflict)

	var conflict *ConfigRevisionConflictError
	require.True(t, errors.As(err, &conflict), "conflict surfaces a typed error carrying the revisions")
	assert.Equal(t, int64(0), conflict.Expected)
	assert.Equal(t, int64(1), conflict.Actual)

	// The conflicting write left the store untouched.
	got, revision, err := store.GetChannelConfig(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, int64(1), revision, "a lost write does not bump the revision")
	require.NotNil(t, got.SalienceMaxChannelMembers)
	assert.Equal(t, 9, *got.SalienceMaxChannelMembers, "the winning write's value is intact")
}

// TestSQLiteStore_PutChannelConfig_EmptyOverridesAreInheritAll asserts that
// clearing every knob persists as inherit-all (NULL blob) while still bumping
// the revision — an apply is an apply even when it unsets everything.
func TestSQLiteStore_PutChannelConfig_EmptyOverridesAreInheritAll(t *testing.T) {
	store, ctx, path := newConfigStore(t)
	mustCreateGroup(t, store, "planning")

	maxMembers := 7
	require.NoError(t, store.PutChannelConfig(ctx, "group:planning",
		ChannelConfigOverrides{SalienceMaxChannelMembers: &maxMembers}, 0, ""))
	// Now unset everything.
	require.NoError(t, store.PutChannelConfig(ctx, "group:planning",
		ChannelConfigOverrides{}, 1, ""))

	got, revision, err := store.GetChannelConfig(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, int64(2), revision, "unsetting everything still counts as an apply")
	assert.True(t, got.IsEmpty(), "all knobs cleared → inherit-all")

	// The empty override persists as a NULL blob, identical to a never-written
	// channel — not a literal "{}" string.
	withDB(t, path, func(db *sql.DB) {
		var blob sql.NullString
		require.NoError(t, db.QueryRow(
			`SELECT config_overrides_json FROM channels WHERE id = 'group:planning'`).Scan(&blob))
		assert.False(t, blob.Valid, "inherit-all persists as NULL, not an empty JSON object")
	})
}

// TestSQLiteStore_PutChannelConfig_EmptyOnPristineChannelIsNoOp asserts that a
// no-content apply against a never-edited channel (revision 0) does NOT bump the
// revision. There is nothing to clear, and a gratuitous bump would shadow the
// channel's config/channels.yaml block under RFC 0050's revision gate (which
// seeds a YAML block — revision absent = 0 — only while the store is at revision
// 0). The clear-everything-after-editing case (revision > 0) still bumps — see
// TestSQLiteStore_PutChannelConfig_EmptyOverridesAreInheritAll.
func TestSQLiteStore_PutChannelConfig_EmptyOnPristineChannelIsNoOp(t *testing.T) {
	store, ctx, path := newConfigStore(t)
	mustCreateGroup(t, store, "planning")

	// Apply inherit-all to a channel that already inherits everything.
	require.NoError(t, store.PutChannelConfig(ctx, "group:planning",
		ChannelConfigOverrides{}, 0, ""))

	_, revision, err := store.GetChannelConfig(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, int64(0), revision,
		"an empty apply on a pristine channel is a no-op — revision stays 0 so YAML still seeds")

	// The row is untouched: still a NULL blob, identical to a never-written channel.
	withDB(t, path, func(db *sql.DB) {
		var blob sql.NullString
		require.NoError(t, db.QueryRow(
			`SELECT config_overrides_json FROM channels WHERE id = 'group:planning'`).Scan(&blob))
		assert.False(t, blob.Valid, "pristine channel stays NULL (inherit-all), unbumped")
	})
}

// TestSQLiteStore_PutChannelConfig_PersistsLineage asserts the (otherwise
// dormant) lineage argument is written through when supplied.
func TestSQLiteStore_PutChannelConfig_PersistsLineage(t *testing.T) {
	store, ctx, path := newConfigStore(t)
	mustCreateGroup(t, store, "planning")

	maxMembers := 5
	require.NoError(t, store.PutChannelConfig(ctx, "group:planning",
		ChannelConfigOverrides{SalienceMaxChannelMembers: &maxMembers}, 0, "interaction:abc123"))

	withDB(t, path, func(db *sql.DB) {
		var lineage sql.NullString
		require.NoError(t, db.QueryRow(
			`SELECT config_change_lineage FROM channels WHERE id = 'group:planning'`).Scan(&lineage))
		require.True(t, lineage.Valid)
		assert.Equal(t, "interaction:abc123", lineage.String)
	})
}

// TestSQLiteStore_ChannelConfig_MissingChannel asserts both accessors surface
// [ErrChannelNotFound] for an id the store has never seen.
func TestSQLiteStore_ChannelConfig_MissingChannel(t *testing.T) {
	store, ctx, _ := newConfigStore(t)

	_, _, err := store.GetChannelConfig(ctx, "group:ghost")
	assert.ErrorIs(t, err, ErrChannelNotFound)

	maxMembers := 4
	err = store.PutChannelConfig(ctx, "group:ghost",
		ChannelConfigOverrides{SalienceMaxChannelMembers: &maxMembers}, 0, "")
	assert.ErrorIs(t, err, ErrChannelNotFound)
}

// newConfigStore opens a fresh on-disk store and returns it with a context and
// its on-disk path. On-disk (not :memory:) so the lineage / NULL-blob
// assertions can reopen the file via withDB.
func newConfigStore(t *testing.T) (store ChannelStore, ctx context.Context, path string) {
	t.Helper()
	path = filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	return store, context.Background(), path
}
