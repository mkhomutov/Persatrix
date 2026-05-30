// RFC 0031 Phase 3 PR 1 — orchestrator-side session registry accessor.
//
// SessionRegistry exposes the `sessions` table (created in Phase 1
// migration v3, populated by the ISSUE-0082 SessionResolver) as a
// first-class CRUD surface so the `/api/v1/sessions` REST handlers can
// create, list, resolve, and archive operator sessions without the thin
// Rust CLI touching SQLite. These tests pin the store contract; the REST
// handlers that consume it live in internal/server.
package channels

import (
	"context"
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// mustRegistry builds a SessionRegistry over `store`, failing the test if
// the store is not the SQLite-backed implementation.
func mustRegistry(t *testing.T, store ChannelStore) *SessionRegistry {
	t.Helper()
	r, err := NewSessionRegistry(store)
	require.NoError(t, err)
	return r
}

// TestSessionRegistry_Create_MintsUUIDv7 asserts CreateSession returns a
// concrete, parseable UUIDv7 id (never empty, never the `legacy` carve-out)
// and round-trips the supplied label.
func TestSessionRegistry_Create_MintsUUIDv7(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	r := mustRegistry(t, store)
	ctx := context.Background()

	s, err := r.CreateSession(ctx, "arc-1")
	require.NoError(t, err)
	assert.Equal(t, "arc-1", s.Label)
	assert.NotEmpty(t, s.ID)
	assert.NotEqual(t, DefaultSessionID, s.ID,
		"minted id must not collide with the legacy carve-out")
	assert.False(t, s.Archived, "a freshly created session is active")

	parsed, perr := uuid.Parse(s.ID)
	require.NoError(t, perr, "minted id must be a valid UUID")
	assert.Equal(t, uuid.Version(7), parsed.Version(),
		"id must be a UUIDv7 so list order is lexicographic-by-creation")
}

// TestSessionRegistry_Create_OrdersLexicographicallyByCreation asserts two
// sequential creates sort by id in creation order — the property the default
// `list` order depends on.
func TestSessionRegistry_Create_OrdersLexicographicallyByCreation(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	r := mustRegistry(t, store)
	ctx := context.Background()

	first, err := r.CreateSession(ctx, "first")
	require.NoError(t, err)
	second, err := r.CreateSession(ctx, "second")
	require.NoError(t, err)

	assert.Less(t, first.ID, second.ID,
		"UUIDv7 ids sort lexicographically by creation time")

	got, err := r.ListSessions(ctx, false)
	require.NoError(t, err)
	require.Len(t, got, 2)
	assert.Equal(t, first.ID, got[0].ID, "list is ordered by id ascending")
	assert.Equal(t, second.ID, got[1].ID)
}

// TestSessionRegistry_Create_RejectsReservedLegacyLabel pins OQ #2a: a
// `legacy`-labelled session would silently merge into the always-visible §D
// carve-out, so create rejects it at the store boundary (the server-
// authoritative guard).
func TestSessionRegistry_Create_RejectsReservedLegacyLabel(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	r := mustRegistry(t, store)
	ctx := context.Background()

	_, err := r.CreateSession(ctx, DefaultSessionID)
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrReservedSessionID,
		"the reserved legacy sentinel must be rejected as a label")

	got, err := r.ListSessions(ctx, false)
	require.NoError(t, err)
	assert.Empty(t, got, "a rejected create must not leave a row behind")
}

// TestSessionRegistry_List_FiltersArchived asserts the default list returns
// active rows only and `includeArchived` widens it to all rows.
func TestSessionRegistry_List_FiltersArchived(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	r := mustRegistry(t, store)
	ctx := context.Background()

	active, err := r.CreateSession(ctx, "active")
	require.NoError(t, err)
	archived, err := r.CreateSession(ctx, "to-archive")
	require.NoError(t, err)
	require.NoError(t, r.ArchiveSession(ctx, archived.ID))

	activeOnly, err := r.ListSessions(ctx, false)
	require.NoError(t, err)
	require.Len(t, activeOnly, 1)
	assert.Equal(t, active.ID, activeOnly[0].ID)

	all, err := r.ListSessions(ctx, true)
	require.NoError(t, err)
	require.Len(t, all, 2)
}

// TestSessionRegistry_Archive_IsOneWayAndPreservesRow asserts archive flips
// the flag without deleting the row — the id stays resolvable so its tagged
// memory rows remain reachable via the recall path.
func TestSessionRegistry_Archive_IsOneWayAndPreservesRow(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	r := mustRegistry(t, store)
	ctx := context.Background()

	s, err := r.CreateSession(ctx, "arc")
	require.NoError(t, err)
	require.NoError(t, r.ArchiveSession(ctx, s.ID))

	got, err := r.GetSession(ctx, s.ID)
	require.NoError(t, err, "an archived session is still resolvable, not deleted")
	assert.True(t, got.Archived, "archive flips the archived flag")
	assert.Equal(t, s.Label, got.Label)
}

// TestSessionRegistry_Get_ResolvesByIDOrLabel asserts GetSession accepts
// both the id and the label as the lookup key — the contract `session use`
// and `current` label rendering depend on.
func TestSessionRegistry_Get_ResolvesByIDOrLabel(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	r := mustRegistry(t, store)
	ctx := context.Background()

	s, err := r.CreateSession(ctx, "by-label")
	require.NoError(t, err)

	byID, err := r.GetSession(ctx, s.ID)
	require.NoError(t, err)
	assert.Equal(t, s.ID, byID.ID)

	byLabel, err := r.GetSession(ctx, "by-label")
	require.NoError(t, err)
	assert.Equal(t, s.ID, byLabel.ID, "label resolves to the same row as the id")
}

// TestSessionRegistry_Get_UnknownReturnsNotFound asserts an unknown id-or-
// label (including the synthetic `legacy` carve-out, which has no row)
// surfaces the typed sentinel rather than an empty zero-value session.
func TestSessionRegistry_Get_UnknownReturnsNotFound(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	r := mustRegistry(t, store)
	ctx := context.Background()

	_, err := r.GetSession(ctx, "no-such-session")
	assert.ErrorIs(t, err, ErrSessionNotFound)

	// `legacy` is a synthetic carve-out with no row in `sessions`.
	_, err = r.GetSession(ctx, DefaultSessionID)
	assert.ErrorIs(t, err, ErrSessionNotFound,
		"the legacy carve-out is not a registered row")
}

// TestSessionRegistry_Archive_UnknownReturnsNotFound asserts archiving a
// missing id is a typed not-found, not a silent no-op.
func TestSessionRegistry_Archive_UnknownReturnsNotFound(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	r := mustRegistry(t, store)
	ctx := context.Background()

	err := r.ArchiveSession(ctx, "no-such-session")
	assert.ErrorIs(t, err, ErrSessionNotFound)
}

// TestSessionRegistry_AutoMintedSession_AppearsInList asserts a session
// minted by the ISSUE-0082 dispatch-path resolver (not the operator create
// verb) is visible to `list` on day one — operator-created and auto-minted
// rows live in one registry.
func TestSessionRegistry_AutoMintedSession_AppearsInList(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	r := mustRegistry(t, store)
	resolver := mustResolver(t, store)
	ctx := context.Background()

	autoID, err := resolver.Resolve(ctx, "agent-a", "group:planning")
	require.NoError(t, err)

	got, err := r.ListSessions(ctx, false)
	require.NoError(t, err)
	require.Len(t, got, 1)
	assert.Equal(t, autoID, got[0].ID,
		"an auto-minted session appears in the operator-facing list")
	assert.Empty(t, got[0].Label,
		"the resolver registers id + created_at only; label stays empty until named")
}

// TestSessionRegistry_Get_PrefersIDMatchOverLabelMatch asserts an exact id
// match wins over a row whose *label* happens to equal that id. This pins the
// id-first resolution order the split lookup relies on (id is the primary key
// and the unambiguous form).
func TestSessionRegistry_Get_PrefersIDMatchOverLabelMatch(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	r := mustRegistry(t, store)
	ctx := context.Background()

	a, err := r.CreateSession(ctx, "session-a")
	require.NoError(t, err)
	// b's label is a's id — a pathological-but-legal collision the id-first
	// order must resolve in favour of the row whose *id* matches.
	b, err := r.CreateSession(ctx, a.ID)
	require.NoError(t, err)
	require.NotEqual(t, a.ID, b.ID)

	got, err := r.GetSession(ctx, a.ID)
	require.NoError(t, err)
	assert.Equal(t, a.ID, got.ID,
		"an exact id match wins over a row whose label equals that id")
}

// TestSessionRegistry_Get_DuplicateLabelResolvesLowestID documents the
// deliberate label contract: labels are not unique (the schema has no UNIQUE,
// and the auto-mint path writes NULL labels), so two sessions may share a
// label. GetSession resolves such a label deterministically to the earliest-
// created (lowest id) row. Whether the operator surface should reject a
// duplicate label is a PR 2 UX decision; this test pins today's behaviour so it
// stays intentional rather than accidental.
func TestSessionRegistry_Get_DuplicateLabelResolvesLowestID(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	r := mustRegistry(t, store)
	ctx := context.Background()

	first, err := r.CreateSession(ctx, "dup")
	require.NoError(t, err)
	second, err := r.CreateSession(ctx, "dup")
	require.NoError(t, err)
	require.Less(t, first.ID, second.ID)

	got, err := r.GetSession(ctx, "dup")
	require.NoError(t, err)
	assert.Equal(t, first.ID, got.ID,
		"duplicate labels resolve to the earliest-created (lowest id) row")
}
