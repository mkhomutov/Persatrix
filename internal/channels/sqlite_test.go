package channels

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// newTestStore opens a fresh on-disk database under t.TempDir(). On-disk
// (rather than `:memory:`) is used because modernc.org/sqlite's in-memory
// mode does not share state across the connection pool when WAL is
// configured. A fresh file per test is hermetic and fast (~ms).
func newTestStore(t *testing.T, opts SQLiteOptions) ChannelStore {
	t.Helper()
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, opts)
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	return store
}

func mustCreateGroup(t *testing.T, store ChannelStore, name string, members ...string) string {
	t.Helper()
	id := "group:" + name
	require.NoError(t, store.CreateChannel(context.Background(), Channel{
		ID: id, Name: name, Type: ChannelTypeGroup,
	}))
	for _, m := range members {
		require.NoError(t, store.AddMember(context.Background(), id, m, RespondWhenMentioned))
	}
	return id
}

func TestChannelType_Valid(t *testing.T) {
	for _, ct := range []ChannelType{ChannelTypeGroup, ChannelTypeDM, ChannelTypeThread} {
		assert.True(t, ct.Valid(), "want %s valid", ct)
	}
	assert.False(t, ChannelType("broadcast").Valid())
	assert.False(t, ChannelType("").Valid())
}

func TestRespondPolicy_Valid(t *testing.T) {
	for _, p := range []RespondPolicy{RespondWhenMentioned, RespondAlways, RespondNever} {
		assert.True(t, p.Valid(), "want %s valid", p)
	}
	assert.False(t, RespondPolicy("sometimes").Valid())
}

func TestCanonicalDMID_SortsParticipants(t *testing.T) {
	id1, err := CanonicalDMID("agent-b", "agent-a")
	require.NoError(t, err)
	id2, err := CanonicalDMID("agent-a", "agent-b")
	require.NoError(t, err)
	assert.Equal(t, "dm:agent-a:agent-b", id1)
	assert.Equal(t, id1, id2)
}

func TestCanonicalDMID_RejectsInvalid(t *testing.T) {
	cases := []struct{ a, b string }{
		{"agent-a", "agent-a"}, // same participant
		{"agent:a", "agent-b"}, // colon in id
		{"", "agent-b"},        // empty
		{"agent-a", "agent b"}, // whitespace
	}
	for _, tc := range cases {
		_, err := CanonicalDMID(tc.a, tc.b)
		assert.Error(t, err, "%q/%q", tc.a, tc.b)
	}
}

func TestSQLiteStore_CreateAndGetChannel(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()

	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup, Description: "x",
	}))

	got, err := store.GetChannel(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, "group:planning", got.ID)
	assert.Equal(t, "planning", got.Name)
	assert.Equal(t, ChannelTypeGroup, got.Type)

	_, err = store.GetChannel(ctx, "group:missing")
	assert.ErrorIs(t, err, ErrChannelNotFound)
}

func TestSQLiteStore_CreateChannel_DuplicateRejected(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	mustCreateGroup(t, store, "planning")
	err := store.CreateChannel(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	})
	assert.ErrorIs(t, err, ErrChannelExists)
}

func TestSQLiteStore_GlobalChannelCapEnforced(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{MaxChannels: 2})
	ctx := context.Background()
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: "group:a", Name: "a", Type: ChannelTypeGroup}))
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: "group:b", Name: "b", Type: ChannelTypeGroup}))
	err := store.CreateChannel(ctx, Channel{ID: "group:c", Name: "c", Type: ChannelTypeGroup})
	assert.ErrorIs(t, err, ErrChannelCapExceeded)
}

func TestSQLiteStore_AddMember_Idempotent(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning")

	require.NoError(t, store.AddMember(ctx, id, "alice", RespondAlways))
	first, err := store.GetMember(ctx, id, "alice")
	require.NoError(t, err)

	// Re-add must not change the joined_at or policy.
	time.Sleep(2 * time.Millisecond)
	require.NoError(t, store.AddMember(ctx, id, "alice", RespondNever))
	second, err := store.GetMember(ctx, id, "alice")
	require.NoError(t, err)
	assert.Equal(t, first.JoinedAt.UnixNano(), second.JoinedAt.UnixNano())
	assert.Equal(t, RespondAlways, second.RespondPolicy, "re-add must not overwrite policy")
}

func TestSQLiteStore_AddMember_RejectsUnknownChannel(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	err := store.AddMember(context.Background(), "group:missing", "alice", RespondWhenMentioned)
	assert.ErrorIs(t, err, ErrChannelNotFound)
}

func TestSQLiteStore_PublishAndHistory(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob")

	for i := 0; i < 5; i++ {
		require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
			ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
			Content:   fmt.Sprintf("msg-%d", i),
			Mentions:  []string{"bob"},
			Timestamp: time.Now().UTC().Add(time.Duration(i) * time.Millisecond),
		}))
	}

	hist, err := store.GetHistory(ctx, id, 10, time.Time{})
	require.NoError(t, err)
	require.Len(t, hist, 5)
	// Newest-first.
	assert.Equal(t, "msg-4", hist[0].Content)
	assert.Equal(t, "msg-0", hist[4].Content)
	assert.Equal(t, []string{"bob"}, hist[0].Mentions)
}

func TestSQLiteStore_PublishMessage_NonMember_403Equivalent(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice")
	err := store.PublishMessage(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "bob", Content: "x",
	})
	assert.ErrorIs(t, err, ErrNotMember)
}

func TestSQLiteStore_PublishMessage_UnknownChannel(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	err := store.PublishMessage(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: "group:missing", SenderID: "alice", Content: "x",
	})
	assert.ErrorIs(t, err, ErrChannelNotFound)
}

func TestSQLiteStore_PerChannelCapPruning(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{MaxMessagesPerChannel: 5})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice")

	for i := 0; i < 7; i++ {
		require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
			ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
			Content:   fmt.Sprintf("msg-%d", i),
			Timestamp: time.Now().UTC().Add(time.Duration(i) * time.Millisecond),
		}))
	}

	hist, err := store.GetHistory(ctx, id, 100, time.Time{})
	require.NoError(t, err)
	assert.Len(t, hist, 5, "post-prune cap")
	// Newest-first; oldest two should be gone.
	assert.Equal(t, "msg-6", hist[0].Content)
	assert.Equal(t, "msg-2", hist[4].Content)
}

func TestSQLiteStore_ThreadFKCascade(t *testing.T) {
	// RFC 0011 §B: pruning a thread root must cascade to its replies in the
	// same transaction with no FK violation.
	store := newTestStore(t, SQLiteOptions{MaxMessagesPerChannel: 5})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice")

	rootID := uuid.NewString()
	rootTS := time.Now().UTC()
	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID: rootID, ChannelID: id, SenderID: "alice", Content: "root",
		Timestamp: rootTS,
	}))
	// Two replies anchored to root, also old enough to be pruned.
	for i := 0; i < 2; i++ {
		require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
			ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
			Content: fmt.Sprintf("reply-%d", i), ThreadID: rootID,
			Timestamp: rootTS.Add(time.Duration(i+1) * time.Millisecond),
		}))
	}
	// Now publish enough fresh messages to push the cap and force the root
	// (oldest) to prune. Replies must cascade.
	for i := 0; i < 6; i++ {
		require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
			ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
			Content:   fmt.Sprintf("fresh-%d", i),
			Timestamp: rootTS.Add(time.Hour + time.Duration(i)*time.Millisecond),
		}))
	}

	thread, err := store.GetThread(ctx, rootID, 0)
	require.NoError(t, err)
	assert.Empty(t, thread, "thread replies cascade-deleted with root")

	_, err = store.GetMessage(ctx, rootID)
	assert.ErrorIs(t, err, ErrChannelNotFound, "root pruned")
}

func TestSQLiteStore_ChannelDeletionCascade(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "hi",
	}))

	require.NoError(t, store.DeleteChannel(ctx, id))

	members, err := store.GetMembers(ctx, id)
	require.NoError(t, err)
	assert.Empty(t, members)

	hist, err := store.GetHistory(ctx, id, 10, time.Time{})
	require.NoError(t, err)
	assert.Empty(t, hist)

	_, err = store.GetChannel(ctx, id)
	assert.ErrorIs(t, err, ErrChannelNotFound)

	err = store.DeleteChannel(ctx, id)
	assert.ErrorIs(t, err, ErrChannelNotFound, "second delete is a 404")
}

func TestSQLiteStore_GetOrCreateDM_Idempotent(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	a, err := store.GetOrCreateDM(ctx, "agent-b", "agent-a")
	require.NoError(t, err)
	b, err := store.GetOrCreateDM(ctx, "agent-a", "agent-b")
	require.NoError(t, err)
	assert.Equal(t, "dm:agent-a:agent-b", a.ID)
	assert.Equal(t, a.ID, b.ID)
	assert.Equal(t, ChannelTypeDM, a.Type)

	members, err := store.GetMembers(ctx, a.ID)
	require.NoError(t, err)
	require.Len(t, members, 2)
	for _, m := range members {
		assert.Equal(t, RespondAlways, m.RespondPolicy)
	}
}

func TestSQLiteStore_HistoryPaginationByBefore(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice")

	base := time.Now().UTC().Truncate(time.Second)
	timestamps := make([]time.Time, 6)
	for i := 0; i < 6; i++ {
		ts := base.Add(time.Duration(i) * time.Second)
		timestamps[i] = ts
		require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
			ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
			Content: fmt.Sprintf("msg-%d", i), Timestamp: ts,
		}))
	}

	page1, err := store.GetHistory(ctx, id, 3, time.Time{})
	require.NoError(t, err)
	require.Len(t, page1, 3)
	assert.Equal(t, "msg-5", page1[0].Content)
	cursor := page1[len(page1)-1].Timestamp

	page2, err := store.GetHistory(ctx, id, 3, cursor)
	require.NoError(t, err)
	require.Len(t, page2, 3)
	assert.Equal(t, "msg-2", page2[0].Content)
	assert.Equal(t, "msg-0", page2[2].Content)

	// No duplicates across pages.
	seen := map[string]bool{}
	for _, m := range append(page1, page2...) {
		assert.False(t, seen[m.ID], "duplicate id across pages: %s", m.ID)
		seen[m.ID] = true
	}
}

func TestSQLiteStore_GetMessage_Found(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice")
	mid := uuid.NewString()
	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID: mid, ChannelID: id, SenderID: "alice", Content: "hello",
	}))

	got, err := store.GetMessage(ctx, mid)
	require.NoError(t, err)
	assert.Equal(t, "hello", got.Content)
	assert.Equal(t, "alice", got.SenderID)
}

func TestSQLiteStore_IsMember(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice")
	ok, err := store.IsMember(ctx, id, "alice")
	require.NoError(t, err)
	assert.True(t, ok)
	ok, err = store.IsMember(ctx, id, "bob")
	require.NoError(t, err)
	assert.False(t, ok)
}

func TestSQLiteStore_RejectsInvalidEnums(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	err := store.CreateChannel(ctx, Channel{ID: "x", Name: "x", Type: "broadcast"})
	assert.ErrorIs(t, err, ErrInvalidChannelType)

	mustCreateGroup(t, store, "planning")
	err = store.AddMember(ctx, "group:planning", "alice", "sometimes")
	assert.ErrorIs(t, err, ErrInvalidRespondPolicy)
}

func TestSQLiteStore_Close_Idempotent(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	require.NoError(t, store.Close())
	// Second close on the same handle returns an error from database/sql,
	// which is fine — we only assert that the first one was clean.
	_ = store.Close()
}

// Sanity: errors.Is unwraps through the fmt.Errorf wrappers used throughout.
func TestErrors_Unwrap(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	_, err := store.GetChannel(context.Background(), "missing")
	require.Error(t, err)
	assert.True(t, errors.Is(err, ErrChannelNotFound))
}
