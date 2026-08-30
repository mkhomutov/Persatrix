package channels

import (
	"context"
	"database/sql"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// ISSUE-0130 shape (b) — channel-store v11 → v12 (`messages.principal_id`).
//
// Two properties are pinned here and they are different in kind: the SCHEMA
// half (column present, backfilled, stamped atomically, idempotent) and the
// WRITE half — that the value comes from the publishing context and from
// nowhere else. The second is the one that matters: the column is the seed PR
// B2 attributes replayed derivation from, so a caller-settable principal
// would be a cross-tenant read primitive, not a cosmetic defect.

// migrateThroughV11 hand-builds a v11-shaped database at `path`: the v1
// baseline plus every migration step through v10→v11, each stamping its own
// user_version, so the file reads back as a genuine v11 store whose next
// open advances exactly one step. Mirrors the v10→v11 test's own ladder.
func migrateThroughV11(t *testing.T, path string) *sql.DB {
	t.Helper()
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
	require.NoError(t, migrateV7ToV8(db))
	require.NoError(t, migrateV8ToV9(db))
	require.NoError(t, migrateV9ToV10(db))
	require.NoError(t, migrateV10ToV11(db))
	return db
}

// messagePrincipal reads the raw column, bypassing the store API — the
// assertions about what was PERSISTED must not run through the same scan
// path they are checking.
func messagePrincipal(t *testing.T, db *sql.DB, messageID string) string {
	t.Helper()
	var got string
	require.NoError(t, db.QueryRow(
		`SELECT principal_id FROM messages WHERE id = ?`, messageID).Scan(&got))
	return got
}

// TestSQLiteStore_SchemaV12_FreshDB_MessagesHasPrincipalColumn — a fresh
// database lands with the column present.
func TestSQLiteStore_SchemaV12_FreshDB_MessagesHasPrincipalColumn(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		assert.Contains(t, tableColumns(t, db, "messages"), "principal_id",
			"messages.principal_id missing")
	})
}

// TestSQLiteStore_SchemaV12_Migration_Idempotent pins the literal latest
// version (the newest migration's test owns the literal pin, per the v5/v6
// test-header convention) and that a reopen is a no-op.
func TestSQLiteStore_SchemaV12_Migration_Idempotent(t *testing.T) {
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
		assert.Equal(t, 12, version,
			"channelStoreSchemaVersion is v12 as of ISSUE-0130 shape (b); reopen is a no-op")
		assert.Equal(t, channelStoreSchemaVersion, version,
			"the literal pin and the const must agree")
	})
}

// TestSQLiteStore_Migration_V11ToV12_BackfillsLocal pins the data migration on
// a POPULATED v11 store: existing message rows are carried forward unchanged
// and every one backfills to `local` — "no verified tenant", which for a row
// written before the column existed is the truth rather than a downgrade.
func TestSQLiteStore_Migration_V11ToV12_BackfillsLocal(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")

	db := migrateThroughV11(t, path)
	_, err := db.Exec(
		`INSERT INTO channels (id, name, channel_type, description, created_at, session_id, epoch_id, classification)
		   VALUES ('group:planning', 'planning', 'group', '', '2026-01-01T00:00:00Z', 'legacy', 'live', 'internal')`)
	require.NoError(t, err)
	_, err = db.Exec(
		`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at)
		   VALUES ('group:planning', 'alice-person', 'always', '2026-01-01T00:00:00Z')`)
	require.NoError(t, err)
	_, err = db.Exec(
		`INSERT INTO messages (id, channel_id, sender_id, content, timestamp, mentions, metadata, session_id, epoch_id)
		   VALUES ('msg-pre-v12', 'group:planning', 'alice-person', 'said before the column existed',
		           '2026-01-01T00:00:00Z', '[]', '{}', 'legacy', 'live')`)
	require.NoError(t, err)
	require.NoError(t, db.Close())

	// Reopen — the v11→v12 migration runs.
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	withDB(t, path, func(db *sql.DB) {
		var version int
		require.NoError(t, db.QueryRow(`PRAGMA user_version`).Scan(&version))
		assert.Equal(t, channelStoreSchemaVersion, version, "the v11→v12 migration ran")

		assert.Equal(t, DefaultPrincipalID, messagePrincipal(t, db, "msg-pre-v12"),
			"a pre-v12 row backfills to local")
	})

	// No data loss, and the store API surfaces the backfilled value.
	msg, err := store.GetMessage(context.Background(), "msg-pre-v12")
	require.NoError(t, err)
	assert.Equal(t, "said before the column existed", msg.Content)
	assert.Equal(t, DefaultPrincipalID, msg.PrincipalID)
}

// TestMigrateV11ToV12_StampsUserVersionInTransaction drives the single
// migration step directly (the PR #335 review L3 property, pinned per-step
// like the v1→v2 / v2→v3 originals): after the step, user_version reads 12 —
// the stamp committed atomically with the ALTER.
func TestMigrateV11ToV12_StampsUserVersionInTransaction(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	db := migrateThroughV11(t, path)
	t.Cleanup(func() { _ = db.Close() })

	require.NoError(t, migrateV11ToV12(db))

	var version int
	require.NoError(t, db.QueryRow(`PRAGMA user_version`).Scan(&version))
	assert.Equal(t, 12, version, "v11→v12 stamps user_version inside its own tx")
}

// TestPublishMessage_StampsPrincipalFromContext is the write half: the value
// persisted is the one on the publishing context, and an unauthenticated
// publish — no principal on the ctx, which is every agent turn and every
// caller under `auth.mode: disabled` — persists `local`.
func TestPublishMessage_StampsPrincipalFromContext(t *testing.T) {
	for _, tc := range []struct {
		name string
		ctx  func() context.Context
		want string
	}{
		{
			name: "authenticated publish persists the verified principal",
			ctx:  func() context.Context { return WithPrincipal(context.Background(), "alice-person") },
			want: "alice-person",
		},
		{
			name: "unauthenticated publish persists local",
			ctx:  context.Background,
			want: DefaultPrincipalID,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "channels.db")
			store, err := NewSQLiteStore(path, SQLiteOptions{})
			require.NoError(t, err)
			t.Cleanup(func() { _ = store.Close() })
			id := mustCreateGroup(t, store, "planning", "alice-person")

			require.NoError(t, store.PublishMessage(tc.ctx(), ChannelMessage{
				ID: "m1", ChannelID: id, SenderID: "alice-person", Content: "hi",
			}))

			withDB(t, path, func(db *sql.DB) {
				assert.Equal(t, tc.want, messagePrincipal(t, db, "m1"))
			})
		})
	}
}

// TestPublishMessage_DiscardsCallerSuppliedPrincipal is the security pin: the
// store OVERWRITES [ChannelMessage.PrincipalID] from the context rather than
// defaulting it when empty. Without this, an agent — which reaches the publish
// seam unauthenticated by design — could name a tenant, and B2 would attribute
// replayed derivation to it. The REST body has no field to carry one either
// (pinned server-side); this is the store-level backstop for every
// programmatic caller.
func TestPublishMessage_DiscardsCallerSuppliedPrincipal(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	id := mustCreateGroup(t, store, "planning", "ember-owl")

	// An unauthenticated publish claiming someone else's tenant...
	require.NoError(t, store.PublishMessage(context.Background(), ChannelMessage{
		ID: "claimed", ChannelID: id, SenderID: "ember-owl", Content: "I am Alice",
		PrincipalID: "alice-person",
	}))
	// ...and an authenticated one claiming a THIRD tenant, so the test
	// distinguishes "ignored" from "used only as a fallback".
	require.NoError(t, store.PublishMessage(
		WithPrincipal(context.Background(), "bob-person"),
		ChannelMessage{
			ID: "overridden", ChannelID: id, SenderID: "ember-owl", Content: "I am Alice",
			PrincipalID: "alice-person",
		}))

	withDB(t, path, func(db *sql.DB) {
		assert.Equal(t, DefaultPrincipalID, messagePrincipal(t, db, "claimed"),
			"a caller-supplied principal is discarded, not used as a default")
		assert.Equal(t, "bob-person", messagePrincipal(t, db, "overridden"),
			"the context wins over the caller's claim")
	})
}

// TestMessageReadPaths_CarryPrincipal pins that every `messages` projection
// surfaces the column — the four unaliased reads through [messageColumns] and
// the m-aliased scoped read through [recallMessageColumns]. A projection that
// forgets it does not fail loudly (the scanner would error on arity, but only
// on the endpoint the test suite happens to exercise), so each door is opened
// once here.
func TestMessageReadPaths_CarryPrincipal(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	id := mustCreateGroup(t, store, "planning", "alice-person")

	ctx := WithPrincipal(context.Background(), "alice-person")
	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID: "root", ChannelID: id, SenderID: "alice-person", Content: "root message",
	}))
	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID: "reply", ChannelID: id, SenderID: "alice-person", Content: "a reply",
		ThreadID: "root",
	}))

	bg := context.Background()

	got, err := store.GetMessage(bg, "root")
	require.NoError(t, err)
	assert.Equal(t, "alice-person", got.PrincipalID, "GetMessage")

	history, err := store.GetHistory(bg, id, 10, time.Time{})
	require.NoError(t, err)
	require.NotEmpty(t, history)
	assert.Equal(t, "alice-person", history[0].PrincipalID, "GetHistory")

	paged, err := store.GetHistory(bg, id, 10, time.Now().UTC().Add(time.Hour))
	require.NoError(t, err)
	require.NotEmpty(t, paged)
	assert.Equal(t, "alice-person", paged[0].PrincipalID, "GetHistory (before)")

	thread, err := store.GetThread(bg, "root", 10)
	require.NoError(t, err)
	require.NotEmpty(t, thread)
	assert.Equal(t, "alice-person", thread[0].PrincipalID, "GetThread")

	scoped, err := store.GetHistoryScoped(bg, id, "alice-person", 10, time.Time{})
	require.NoError(t, err)
	require.NotEmpty(t, scoped)
	assert.Equal(t, "alice-person", scoped[0].PrincipalID, "GetHistoryScoped")
}

// TestMessageColumns_MatchRecallProjection pins the two projections identical
// modulo the `m.` alias. They feed one scanner ([scanMessage]) from two files,
// so a column added to one and not the other is a runtime arity error on
// whichever endpoint uses the stale list — the exact drift v12 would have
// caused across the four inline copies [messageColumns] replaced.
func TestMessageColumns_MatchRecallProjection(t *testing.T) {
	assert.Equal(t, messageColumns, strings.ReplaceAll(recallMessageColumns, "m.", ""),
		"messageColumns and recallMessageColumns must name the same columns in the same order")
}

// TestPublishMessage_PersistsTheRestampedCausalPrincipal composes v12 with
// ISSUE-0124 R-2 (merged as PR A2), and is the reason `publishCommit` applies
// the re-stamp AHEAD of the store commit rather than merely ahead of fanout.
//
// A persona's reply re-enters as a fresh UNAUTHENTICATED publish. Persisted
// naively that row would read `local` — and B2, seeding replay from it, would
// re-derive Alice's content into the shared tenant on every restart, which is
// the whole of ISSUE-0130. The re-stamped context makes the stored row carry
// the person who caused the reply.
//
// Reversing the two would still pass every R-2 test (the wire is unaffected)
// and every schema test here, so this is the pin that holds the ordering.
func TestPublishMessage_PersistsTheRestampedCausalPrincipal(t *testing.T) {
	router, _, _, _, id := newRestampRouter(t)
	ctx := context.Background()

	// Alice, authenticated, speaks: her own turn's dispatches carry her tenant
	// and the attribution table records what caused each persona to speak.
	publishUnder(t, router, WithPrincipal(ctx, "alice-person"), id, "alice")

	// Iron-fox replies through the REST hop that holds no credential.
	relayed := uuid.NewString()
	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: relayed, ChannelID: id, SenderID: "iron-fox", Content: "relaying what Alice said",
	}, ""))

	stored, err := router.store.GetMessage(ctx, relayed)
	require.NoError(t, err)
	assert.Equal(t, "alice-person", stored.PrincipalID,
		"the relayed reply must PERSIST the causal tenant; a `local` row here is what replay re-derives")
}
