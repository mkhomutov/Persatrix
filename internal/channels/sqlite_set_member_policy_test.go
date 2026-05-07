package channels

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// Store tests for [ChannelStore.SetMemberPolicy] (ISSUE-0034).

// TestSQLiteStore_SetMemberPolicy_HappyPath pins that updating an
// existing membership flips `respond_policy` and leaves `joined_at`
// unchanged. The unchanged-`joined_at` invariant matters because
// chat-as-DM calls SetMemberPolicy on every chat turn (idempotent
// normalisation); a `joined_at` rewrite would invalidate any
// "members joined since X" diagnostic.
func TestSQLiteStore_SetMemberPolicy_HappyPath(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice")

	before, err := store.GetMember(ctx, id, "alice")
	require.NoError(t, err)

	require.NoError(t, store.SetMemberPolicy(ctx, id, "alice", RespondNever))

	after, err := store.GetMember(ctx, id, "alice")
	require.NoError(t, err)
	assert.Equal(t, RespondNever, after.RespondPolicy)
	assert.True(t, before.JoinedAt.Equal(after.JoinedAt),
		"SetMemberPolicy must not rewrite joined_at (before=%v after=%v)",
		before.JoinedAt, after.JoinedAt)
}

// TestSQLiteStore_SetMemberPolicy_Idempotent pins that re-applying the
// same policy is a no-op (zero-effect UPDATE on the row, but rowcount
// is still 1 because SQLite UPDATE counts matched-rows). The chat
// handler relies on this for the "every chat turn re-normalises the
// user to RespondNever" pattern; without this contract the second
// chat turn would surface ErrNotMember for a row that already exists.
func TestSQLiteStore_SetMemberPolicy_Idempotent(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice")

	require.NoError(t, store.SetMemberPolicy(ctx, id, "alice", RespondNever))
	require.NoError(t, store.SetMemberPolicy(ctx, id, "alice", RespondNever),
		"re-applying the same policy must succeed (chat-handler idempotent normalisation)")

	got, err := store.GetMember(ctx, id, "alice")
	require.NoError(t, err)
	assert.Equal(t, RespondNever, got.RespondPolicy)
}

// TestSQLiteStore_SetMemberPolicy_404OnUnknownChannel exercises the
// channel-existence disambiguation: callers get ErrChannelNotFound
// (not ErrNotMember) when the channel itself is missing.
func TestSQLiteStore_SetMemberPolicy_404OnUnknownChannel(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	err := store.SetMemberPolicy(context.Background(), "group:nope", "alice", RespondAlways)
	assert.ErrorIs(t, err, ErrChannelNotFound)
}

// TestSQLiteStore_SetMemberPolicy_404OnUnknownMember exercises the
// second 404 path (channel exists, participant is not a member).
func TestSQLiteStore_SetMemberPolicy_404OnUnknownMember(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice")
	err := store.SetMemberPolicy(ctx, id, "ghost", RespondAlways)
	assert.ErrorIs(t, err, ErrNotMember)
}

// TestSQLiteStore_SetMemberPolicy_RejectsInvalidPolicy pins the
// vocabulary guard. Mirrors the AddMember validation so callers see
// the same sentinel regardless of which write path they take.
func TestSQLiteStore_SetMemberPolicy_RejectsInvalidPolicy(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice")
	err := store.SetMemberPolicy(ctx, id, "alice", "sometimes")
	assert.ErrorIs(t, err, ErrInvalidRespondPolicy)
}

// TestSQLiteStore_SetMemberPolicy_DMUserToRespondNever exercises the
// motivating use case: a chat-as-DM channel created via GetOrCreateDM
// (both members default to RespondAlways) is then normalised so the
// user becomes RespondNever. End-to-end shape mirrors what
// `Server.handleChat` does on every chat turn (ISSUE-0034).
func TestSQLiteStore_SetMemberPolicy_DMUserToRespondNever(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	dm, err := store.GetOrCreateDM(ctx, "alice", "agent-x")
	require.NoError(t, err)

	require.NoError(t, store.SetMemberPolicy(ctx, dm.ID, "alice", RespondNever))

	user, err := store.GetMember(ctx, dm.ID, "alice")
	require.NoError(t, err)
	assert.Equal(t, RespondNever, user.RespondPolicy)

	agent, err := store.GetMember(ctx, dm.ID, "agent-x")
	require.NoError(t, err)
	assert.Equal(t, RespondAlways, agent.RespondPolicy,
		"SetMemberPolicy on the user must not affect the agent's policy")
}
