// ISSUE-0082 PR 1 — per-request session binding store.
//
// The SessionResolver is the orchestrator-authoritative, persisted source
// of a per-request session id keyed on the (agent, channel, user) unit
// decided in RFC 0031 §B (the PR 2 amendment). These tests pin the store
// contract only — the resolver is NOT yet wired into the dispatch path
// (that activation is PR 2), so there is no behaviour change for any live
// caller until then.
package channels

import (
	"context"
	"database/sql"
	"path/filepath"
	"sync"
	"testing"

	_ "modernc.org/sqlite"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// mustResolver builds a SessionResolver over `store`, failing the test if
// the store is not the SQLite-backed implementation.
func mustResolver(t *testing.T, store ChannelStore) *SessionResolver {
	t.Helper()
	r, err := NewSessionResolver(store)
	require.NoError(t, err)
	return r
}

// TestSessionResolver_SameTriple_StableID asserts a second resolve of the
// same (agent, channel, user) triple returns the first mint — one
// conversation, one session id, every subsequent message reuses it.
func TestSessionResolver_SameTriple_StableID(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	r := mustResolver(t, store)
	ctx := context.Background()

	id1, err := r.Resolve(ctx, "agent-a", "group:planning", "user-1")
	require.NoError(t, err)
	assert.NotEmpty(t, id1, "resolver must mint a non-empty session id")
	assert.NotEqual(t, DefaultSessionID, id1,
		"minted id must not collide with the legacy carve-out")

	id2, err := r.Resolve(ctx, "agent-a", "group:planning", "user-1")
	require.NoError(t, err)
	assert.Equal(t, id1, id2,
		"same (agent, channel, user) resolves to the same session id")
}

// TestSessionResolver_DistinctTriples_DistinctIDs asserts each axis of the
// unit is load-bearing: varying the user, the channel, or the agent yields
// a distinct session id. This is the §B grain — two peers in one channel,
// or two DM threads with one agent, are distinct sessions.
func TestSessionResolver_DistinctTriples_DistinctIDs(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	r := mustResolver(t, store)
	ctx := context.Background()

	base, err := r.Resolve(ctx, "agent-a", "group:planning", "user-1")
	require.NoError(t, err)
	otherUser, err := r.Resolve(ctx, "agent-a", "group:planning", "user-2")
	require.NoError(t, err)
	otherChan, err := r.Resolve(ctx, "agent-a", "group:other", "user-1")
	require.NoError(t, err)
	otherAgent, err := r.Resolve(ctx, "agent-b", "group:planning", "user-1")
	require.NoError(t, err)

	seen := map[string]bool{}
	for _, id := range []string{base, otherUser, otherChan, otherAgent} {
		require.NotEmpty(t, id)
		assert.False(t, seen[id], "session id %q reused across distinct triples", id)
		seen[id] = true
	}
}

// TestSessionResolver_StableAcrossReopen pins the persistence property:
// the id survives a store close + reopen. This is what lets a multi-day
// dementia-test arc keep its session id across a persona-process restart —
// a derived-per-process id would not (RFC 0031 §B: authoritative +
// persisted, not derived).
func TestSessionResolver_StableAcrossReopen(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")

	store1, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	id1, err := mustResolver(t, store1).Resolve(
		context.Background(), "agent-a", "group:planning", "user-1")
	require.NoError(t, err)
	require.NoError(t, store1.Close())

	store2, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store2.Close() })
	id2, err := mustResolver(t, store2).Resolve(
		context.Background(), "agent-a", "group:planning", "user-1")
	require.NoError(t, err)

	assert.Equal(t, id1, id2, "session id must persist across a store reopen")
}

// TestSessionResolver_MintRegistersSession asserts the minted id is
// registered as a row in the `sessions` registry (not only in the binding
// map), so the Phase 3 CLI's `persatrix session list` surfaces it.
func TestSessionResolver_MintRegistersSession(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	id, err := mustResolver(t, store).Resolve(
		context.Background(), "agent-a", "group:planning", "user-1")
	require.NoError(t, err)

	withDB(t, path, func(db *sql.DB) {
		var n int
		require.NoError(t, db.QueryRow(
			`SELECT COUNT(1) FROM sessions WHERE id = ?`, id).Scan(&n))
		assert.Equal(t, 1, n,
			"minted session must be registered in the sessions table")
	})
}

// TestSessionResolver_ConcurrentFirstSight_SingleID pins the race contract:
// N goroutines racing the very first resolve of one triple all agree on a
// single id, and the store ends up with exactly one binding row and one
// session row — no double-mint, no orphan session. SQLite's single-writer
// model narrows the window but the ON CONFLICT DO NOTHING + re-read guard
// is what closes it.
func TestSessionResolver_ConcurrentFirstSight_SingleID(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	r := mustResolver(t, store)

	const n = 16
	ids := make([]string, n)
	errs := make([]error, n)
	start := make(chan struct{})
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			ids[i], errs[i] = r.Resolve(
				context.Background(), "agent-a", "group:planning", "user-1")
		}(i)
	}
	close(start)
	wg.Wait()

	for i := 0; i < n; i++ {
		require.NoErrorf(t, errs[i], "goroutine %d", i)
		assert.Equalf(t, ids[0], ids[i],
			"all concurrent resolves of one triple must agree (goroutine %d)", i)
	}

	withDB(t, path, func(db *sql.DB) {
		var bindings, sessions int
		require.NoError(t, db.QueryRow(`SELECT COUNT(1) FROM session_bindings`).Scan(&bindings))
		require.NoError(t, db.QueryRow(`SELECT COUNT(1) FROM sessions`).Scan(&sessions))
		assert.Equal(t, 1, bindings, "exactly one binding row for one triple")
		assert.Equal(t, 1, sessions, "exactly one session row — no double-mint orphan")
	})
}

// TestSessionResolver_EmptyAxis_Rejected pins the input contract: the
// (agent, channel, user) unit is load-bearing on every axis (RFC 0031 §B —
// the same grain TestSessionResolver_DistinctTriples_DistinctIDs proves), so
// an empty component is never a valid unit. The resolver fails loud with
// ErrEmptySessionAxis rather than minting a session keyed on an empty axis —
// such a junk row would pollute the `sessions` registry the Phase 3 CLI
// surfaces and mis-group recall, the very isolation property ISSUE-0082
// exists to protect. The rejection writes neither a binding nor a session row.
func TestSessionResolver_EmptyAxis_Rejected(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	r := mustResolver(t, store)
	ctx := context.Background()

	cases := []struct {
		name                       string
		agentID, channelID, userID string
	}{
		{"empty agent", "", "group:planning", "user-1"},
		{"empty channel", "agent-a", "", "user-1"},
		{"empty user", "agent-a", "group:planning", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			id, err := r.Resolve(ctx, tc.agentID, tc.channelID, tc.userID)
			require.Error(t, err, "an empty axis must be rejected")
			assert.ErrorIs(t, err, ErrEmptySessionAxis,
				"rejection must surface the ErrEmptySessionAxis sentinel")
			assert.Empty(t, id, "no session id is returned on a rejected triple")
		})
	}

	// The rejected triples must have left the store untouched — fail-loud,
	// not fail-and-still-write.
	withDB(t, path, func(db *sql.DB) {
		var bindings, sessions int
		require.NoError(t, db.QueryRow(`SELECT COUNT(1) FROM session_bindings`).Scan(&bindings))
		require.NoError(t, db.QueryRow(`SELECT COUNT(1) FROM sessions`).Scan(&sessions))
		assert.Equal(t, 0, bindings, "rejected triples must not write a binding row")
		assert.Equal(t, 0, sessions, "rejected triples must not write a session row")
	})
}
