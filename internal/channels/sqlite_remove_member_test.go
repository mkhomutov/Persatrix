package channels

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// PR 4b store tests for [ChannelStore.RemoveMember]. Split from
// `sqlite_test.go` to keep that file under the 500-line review-friendly
// cap.

// TestSQLiteStore_RemoveMember pins the RFC 0011 PR 4b contract:
// removing a member deletes the membership row but the participant's
// prior messages are preserved (`messages.sender_id` retains the
// historical value per §C endpoint table).
func TestSQLiteStore_RemoveMember(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob")

	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "bob", Content: "hi",
	}))

	require.NoError(t, store.RemoveMember(ctx, id, "bob"))

	members, err := store.GetMembers(ctx, id)
	require.NoError(t, err)
	require.Len(t, members, 1)
	assert.Equal(t, "alice", members[0].ParticipantID)

	hist, err := store.GetHistory(ctx, id, 10, time.Time{})
	require.NoError(t, err)
	require.Len(t, hist, 1)
	assert.Equal(t, "bob", hist[0].SenderID,
		"messages.sender_id retains the historical value after RemoveMember")
}

// TestSQLiteStore_RemoveMember_404OnUnknownChannel exercises the channel-
// existence pre-check so REST callers get the right 404 cause.
func TestSQLiteStore_RemoveMember_404OnUnknownChannel(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	err := store.RemoveMember(context.Background(), "group:nope", "alice")
	assert.ErrorIs(t, err, ErrChannelNotFound)
}

// TestSQLiteStore_RemoveMember_404OnUnknownMember exercises the second
// 404 path (channel exists, participant is not a member).
func TestSQLiteStore_RemoveMember_404OnUnknownMember(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice")
	err := store.RemoveMember(ctx, id, "ghost")
	assert.ErrorIs(t, err, ErrNotMember)
}
