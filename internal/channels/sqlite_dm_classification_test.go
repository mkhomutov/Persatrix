// RFC 0037 PR 1 (v0.3.12) — DM-creation classification stamping (§B) plus the
// thread-inheritance-by-construction pin. DMs open on demand with no config
// block, so [sqliteStore.GetOrCreateDM] stamping the operator's
// `dm_default_classification` knob is their only declaration point. The
// column is dark (no store API reads it), so assertions use raw SQL.
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

// TestGetOrCreateDM_StampsInternalByDefault: a store built without the knob
// stamps `internal` — §A rule (a), matching the pre-RFC-0037 world exactly
// (the migration DEFAULT and the stamp agree).
func TestGetOrCreateDM_StampsInternalByDefault(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	dm, err := store.GetOrCreateDM(context.Background(), "alice", "bob")
	require.NoError(t, err)

	withDB(t, path, func(db *sql.DB) {
		assert.Equal(t, "internal", channelClassification(t, db, dm.ID),
			"an unconfigured deployment stamps DMs internal (§A rule (a))")
	})
}

// TestGetOrCreateDM_StampsConfiguredDefault: the `dm_default_classification`
// knob reaches the row verbatim when it names a known lattice level.
func TestGetOrCreateDM_StampsConfiguredDefault(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{
		DMDefaultClassification: ClassificationRestricted,
	})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	dm, err := store.GetOrCreateDM(context.Background(), "alice", "bob")
	require.NoError(t, err)

	withDB(t, path, func(db *sql.DB) {
		assert.Equal(t, "restricted", channelClassification(t, db, dm.ID),
			"the configured dm_default_classification is stamped at creation")
	})
}

// TestGetOrCreateDM_UnknownKnobFailsClosedToInternal: an out-of-vocabulary
// option value (a caller that bypassed [Config.Validate]) normalizes to
// `internal` at store construction — rule (a) again, never `public`, never
// the raw string into the column.
func TestGetOrCreateDM_UnknownKnobFailsClosedToInternal(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{
		DMDefaultClassification: Classification("confidential"),
	})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	dm, err := store.GetOrCreateDM(context.Background(), "alice", "bob")
	require.NoError(t, err)

	withDB(t, path, func(db *sql.DB) {
		assert.Equal(t, "internal", channelClassification(t, db, dm.ID),
			"an unknown knob value must stamp internal, not leak the raw string")
	})
}

// TestGetOrCreateDM_ExistingDMKeepsItsClassification: GetOrCreateDM is
// idempotent — a DM created under one default is NOT re-stamped when resolved
// again under a different one. Reclassifying an existing DM is the audited
// reclassification machinery's job (a later RFC 0037 PR), not a side effect
// of a lookup.
func TestGetOrCreateDM_ExistingDMKeepsItsClassification(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store1, err := NewSQLiteStore(path, SQLiteOptions{
		DMDefaultClassification: ClassificationRestricted,
	})
	require.NoError(t, err)
	dm, err := store1.GetOrCreateDM(context.Background(), "alice", "bob")
	require.NoError(t, err)
	require.NoError(t, store1.Close())

	// Reopen with a different default — the resolve path must not rewrite.
	store2, err := NewSQLiteStore(path, SQLiteOptions{
		DMDefaultClassification: ClassificationPublic,
	})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store2.Close() })

	again, err := store2.GetOrCreateDM(context.Background(), "alice", "bob")
	require.NoError(t, err)
	assert.Equal(t, dm.ID, again.ID)

	withDB(t, path, func(db *sql.DB) {
		assert.Equal(t, "restricted", channelClassification(t, db, dm.ID),
			"an existing DM keeps its creation-time classification")
	})
}

// TestThreadReplies_NoThreadChannelRow_InheritByConstruction pins the §B
// thread rule as it is actually implemented: there is NO stamping code path
// for threads because no production path creates a `thread:` channel row —
// a threaded reply is a `messages` row in the PARENT channel (the
// `thread_id` FK), so it carries the parent's classification by
// construction. If a future change starts materializing thread channels,
// this test fails and the §B copy-parent rule must be implemented with it.
func TestThreadReplies_NoThreadChannelRow_InheritByConstruction(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	ctx := context.Background()
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	}))
	require.NoError(t, store.AddMember(ctx, "group:planning", "alice", RespondAlways))

	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID: "m-parent", ChannelID: "group:planning", SenderID: "alice",
		Content: "parent",
	}))
	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID: "m-reply", ChannelID: "group:planning", SenderID: "alice",
		Content: "threaded reply", ThreadID: "m-parent",
	}))

	withDB(t, path, func(db *sql.DB) {
		var channelRows int
		require.NoError(t, db.QueryRow(
			`SELECT COUNT(*) FROM channels`).Scan(&channelRows))
		assert.Equal(t, 1, channelRows,
			"a threaded reply creates no thread: channel row — it lives in the parent")

		var replyChannel string
		require.NoError(t, db.QueryRow(
			`SELECT channel_id FROM messages WHERE id = 'm-reply'`).Scan(&replyChannel))
		assert.Equal(t, "group:planning", replyChannel,
			"the reply is a row in the parent channel, so it shares its classification")
	})
}
