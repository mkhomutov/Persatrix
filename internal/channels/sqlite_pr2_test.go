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

// TestSQLiteStore_CreateChannel_GroupID_MustMatchName pins PR #231 review
// SF-2: CreateChannel rejects a group row whose PK disagrees with its
// declared name. Without this guard, a REST handler could persist
// `(ID="group:foo", Name="bar")` and silently desync every downstream
// memory and observability lookup keyed on the canonical id.
func TestSQLiteStore_CreateChannel_GroupID_MustMatchName(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()

	err := store.CreateChannel(ctx, Channel{
		ID: "group:foo", Name: "bar", Type: ChannelTypeGroup,
	})
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidChannelType,
		"id/name mismatch must surface as ErrInvalidChannelType")

	// The matching pair still succeeds.
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:foo", Name: "foo", Type: ChannelTypeGroup,
	}))
}

// TestSQLiteStore_CreateChannel_NonGroup_NameStoredAsNull pins SF-4: DM
// and thread rows persist `name` as NULL, not as a placeholder of `id`.
// We open the underlying *sql.DB to read the column directly because the
// public scan path reports the same string in either case (Name == "").
func TestSQLiteStore_CreateChannel_NonGroup_NameStoredAsNull(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()

	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "dm:agent-a:agent-b", Type: ChannelTypeDM,
	}))
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "thread:msg-123", Type: ChannelTypeThread,
	}))

	db, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	defer func() { _ = db.Close() }()

	for _, id := range []string{"dm:agent-a:agent-b", "thread:msg-123"} {
		var name sql.NullString
		require.NoError(t,
			db.QueryRow(`SELECT name FROM channels WHERE id = ?`, id).Scan(&name),
			"id=%s", id)
		assert.False(t, name.Valid, "name must be NULL for non-group row id=%s (got %q)", id, name.String)
	}
}

// TestSQLiteStore_PartialUniqueIndex_GroupNameOnly pins SF-4: two group
// rows still cannot share a name, but a DM/thread row coexists with a
// group row of the same name (the partial index excludes them, and they
// store NULL anyway).
func TestSQLiteStore_PartialUniqueIndex_GroupNameOnly(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()

	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	}))

	// Second group with the same name → still rejected.
	err := store.CreateChannel(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	})
	assert.ErrorIs(t, err, ErrChannelExists)

	// A DM row sharing "planning" as a hypothetical name is unrepresentable
	// (the writer hard-codes NULL for non-group); but a DM row coexists
	// fine with the group row above — the partial index doesn't apply.
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "dm:agent-a:agent-b", Type: ChannelTypeDM,
	}))
}

// TestSQLiteStore_SchemaVersion_Stamped pins the v1→v2 migration:
// PRAGMA user_version reports 2 after a fresh store opens, and the
// `ux_channels_name_group` partial index exists.
func TestSQLiteStore_SchemaVersion_Stamped(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	db, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	defer func() { _ = db.Close() }()

	var version int
	require.NoError(t, db.QueryRow(`PRAGMA user_version`).Scan(&version))
	assert.Equal(t, channelStoreSchemaVersion, version,
		"PRAGMA user_version should equal channelStoreSchemaVersion")

	var indexCount int
	require.NoError(t,
		db.QueryRow(`SELECT COUNT(1) FROM sqlite_master WHERE type='index' AND name='ux_channels_name_group'`).
			Scan(&indexCount))
	assert.Equal(t, 1, indexCount, "partial unique index must exist")
}

// TestSQLiteStore_Migration_V1ToV2_PreservesRows simulates an existing v1
// database opened by a v2 binary: rows survive, DM/thread placeholder
// names become NULL, the partial unique index materialises.
func TestSQLiteStore_Migration_V1ToV2_PreservesRows(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")

	// Hand-create a v1-shaped database: name TEXT NOT NULL UNIQUE, no
	// user_version stamp.
	db, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	_, err = db.Exec(schemaV1SQL)
	require.NoError(t, err)
	// Insert a v1 group + a v1-style DM row whose `name` is the placeholder id.
	_, err = db.Exec(
		`INSERT INTO channels (id, name, channel_type, description, created_at)
		 VALUES (?, ?, 'group', '', datetime('now')),
		        (?, ?, 'dm',    '', datetime('now')),
		        (?, ?, 'thread','', datetime('now'))`,
		"group:planning", "planning",
		"dm:a:b", "dm:a:b",
		"thread:msg-1", "thread:msg-1",
	)
	require.NoError(t, err)
	require.NoError(t, db.Close())

	// Reopen via NewSQLiteStore — applySchema should detect user_version=0
	// (treated as v1), run the v1→v2 migration, and stamp version 2.
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	chs, err := store.ListChannels(context.Background())
	require.NoError(t, err)
	require.Len(t, chs, 3)

	byID := make(map[string]Channel, 3)
	for _, c := range chs {
		byID[c.ID] = c
	}
	assert.Equal(t, "planning", byID["group:planning"].Name, "group name preserved")
	assert.Equal(t, "", byID["dm:a:b"].Name, "DM placeholder name should now be NULL → empty")
	assert.Equal(t, "", byID["thread:msg-1"].Name, "thread placeholder name should now be NULL → empty")

	// Re-open is idempotent (no migration runs the second time).
	require.NoError(t, store.Close())
	store2, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store2.Close() })

	chs2, err := store2.ListChannels(context.Background())
	require.NoError(t, err)
	assert.Len(t, chs2, 3)
}
