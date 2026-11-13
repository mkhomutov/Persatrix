package channels

import (
	"context"
	"fmt"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// A reply belongs to the channel of the message it replies to (ISSUE-0203).
//
// `messages.thread_id` is a foreign key to `messages.id`. It says the parent
// exists, not where. So a message could be posted in one channel as a reply
// to a message in another, and from then on deleting or pruning the parent's
// channel deleted a message in the reply's channel (`ON DELETE CASCADE`).
// The store now refuses such a publish, for every caller.

// publishTo posts one message and returns its id.
func publishTo(t *testing.T, store ChannelStore, channelID, sender, threadID string) (string, error) {
	t.Helper()
	id := uuid.NewString()
	err := store.PublishMessage(context.Background(), ChannelMessage{
		ID: id, ChannelID: channelID, SenderID: sender,
		Content: "text of " + id, ThreadID: threadID,
	})
	return id, err
}

func historyIDs(t *testing.T, store ChannelStore, channelID string) []string {
	t.Helper()
	msgs, err := store.GetHistory(context.Background(), channelID, 100, time.Time{})
	require.NoError(t, err)
	ids := make([]string, 0, len(msgs))
	for _, m := range msgs {
		ids = append(ids, m.ID)
	}
	return ids
}

func TestSQLiteStore_PublishMessage_AThreadParentMustBeInTheSameChannel(t *testing.T) {
	const unknown = "msg-does-not-exist"
	tests := []struct {
		name string
		// parent says which message the new one names as its thread parent:
		// "" for none, or one of the seeded messages below.
		parent  string
		refused bool
	}{
		{name: "no thread parent", parent: ""},
		{name: "a parent in the same channel", parent: "lobby"},
		{name: "a reply in the same channel as parent", parent: "lobby-reply"},
		{name: "a parent in another channel", parent: "board", refused: true},
		{name: "a reply in another channel as parent", parent: "board-reply", refused: true},
		{name: "a parent that does not exist", parent: unknown, refused: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			store := newTestStore(t, SQLiteOptions{})
			// Both channels have the same members and the same (default)
			// classification: the rule is about the channel, nothing else.
			lobby := mustCreateGroup(t, store, "lobby", "alice", "bob")
			board := mustCreateGroup(t, store, "board", "alice", "bob")
			seeded := map[string]string{"": "", unknown: unknown}
			var err error
			seeded["lobby"], err = publishTo(t, store, lobby, "alice", "")
			require.NoError(t, err)
			seeded["lobby-reply"], err = publishTo(t, store, lobby, "bob", seeded["lobby"])
			require.NoError(t, err)
			seeded["board"], err = publishTo(t, store, board, "alice", "")
			require.NoError(t, err)
			seeded["board-reply"], err = publishTo(t, store, board, "bob", seeded["board"])
			require.NoError(t, err)
			lobbyBefore := historyIDs(t, store, lobby)
			boardBefore := historyIDs(t, store, board)

			id, err := publishTo(t, store, lobby, "alice", seeded[tt.parent])

			if !tt.refused {
				require.NoError(t, err)
				assert.Contains(t, historyIDs(t, store, lobby), id)
				return
			}
			require.ErrorIs(t, err, ErrThreadParentNotInChannel)
			assert.Contains(t, err.Error(), "invalid thread_id "+seeded[tt.parent],
				"the refusal names the thread id the caller sent")
			assert.Equal(t, lobbyBefore, historyIDs(t, store, lobby), "nothing was stored in the lobby")
			assert.Equal(t, boardBefore, historyIDs(t, store, board), "nor in the board")
			_, getErr := store.GetMessage(context.Background(), id)
			assert.ErrorIs(t, getErr, ErrMessageNotFound)
		})
	}
}

// What the refusal protects: an action on one channel leaves the messages of
// another alone. Both ways a parent is deleted are covered, a channel delete
// and the per-channel message cap.
func TestSQLiteStore_AMessageOutlivesAnotherChannelsParent(t *testing.T) {
	tests := []struct {
		name string
		// removeBoardParent deletes the board message the lobby tried to
		// thread under.
		removeBoardParent func(t *testing.T, store ChannelStore, board string)
	}{
		{
			name: "the other channel is deleted",
			removeBoardParent: func(t *testing.T, store ChannelStore, board string) {
				require.NoError(t, store.DeleteChannel(context.Background(), board))
			},
		},
		{
			name: "the other channel prunes its oldest message at the cap",
			removeBoardParent: func(t *testing.T, store ChannelStore, board string) {
				for range 3 { // cap is 3: the first board message is pruned
					_, err := publishTo(t, store, board, "alice", "")
					require.NoError(t, err)
				}
			},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			store := newTestStore(t, SQLiteOptions{MaxMessagesPerChannel: 3})
			lobby := mustCreateGroup(t, store, "lobby", "alice", "bob")
			board := mustCreateGroup(t, store, "board", "alice", "bob")
			boardParent, err := publishTo(t, store, board, "alice", "")
			require.NoError(t, err)
			_, err = publishTo(t, store, lobby, "alice", "")
			require.NoError(t, err)
			// Refused now; on a store that accepted it this was the message
			// the board's delete or prune took with it.
			_, _ = publishTo(t, store, lobby, "bob", boardParent)
			lobbyBefore := historyIDs(t, store, lobby)

			tt.removeBoardParent(t, store, board)

			_, getErr := store.GetMessage(context.Background(), boardParent)
			require.ErrorIs(t, getErr, ErrMessageNotFound, "the board's parent is gone")
			assert.Equal(t, lobbyBefore, historyIDs(t, store, lobby),
				"every lobby message is still there")
		})
	}
}

// A store written to before the publish rule can hold a reply whose parent is
// in another channel. A thread read leaves such a row out: a thread holds the
// replies in its parent's channel, so the channel's label on the response is
// true for every message in it.
func TestSQLiteStore_GetThread_LeavesOutARowInAnotherChannel(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	lobby := mustCreateGroup(t, store, "lobby", "alice", "bob")
	board := mustCreateGroup(t, store, "board", "alice", "bob")
	parent, err := publishTo(t, store, board, "alice", "")
	require.NoError(t, err)
	inBoard, err := publishTo(t, store, board, "bob", parent)
	require.NoError(t, err)
	// The row an older version accepted: posted in the lobby, threaded under
	// the board's message. Written with SQL because the store refuses it.
	sqlStore, ok := store.(*sqliteStore)
	require.True(t, ok)
	_, err = sqlStore.db.ExecContext(context.Background(),
		`INSERT INTO messages (id, channel_id, sender_id, content, timestamp, thread_id, mentions, metadata, session_id, principal_id)
		 VALUES ('old-cross-reply', ?, 'bob', 'posted in the lobby', ?, ?, '[]', '{}', ?, ?)`,
		lobby, time.Now().UTC(), parent, DefaultSessionID, DefaultPrincipalID)
	require.NoError(t, err)

	tests := []struct {
		name  string
		limit int
	}{
		{name: "with a limit", limit: 10},
		{name: "with no limit", limit: 0},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			msgs, err := store.GetThread(context.Background(), parent, tt.limit)
			require.NoError(t, err)
			ids := make([]string, 0, len(msgs))
			for _, m := range msgs {
				ids = append(ids, m.ID)
				assert.Equal(t, board, m.ChannelID)
			}
			assert.Equal(t, []string{inBoard}, ids)
		})
	}
}

// The thread read's channel condition must not change how the thread is
// found: by `idx_messages_thread`, which reads only the thread's rows. The
// channel index would read the whole channel.
func TestThreadQuery_ReadsOnlyTheThread(t *testing.T) {
	for _, ps := range planStores() {
		for _, limit := range []int{0, 50} {
			t.Run(fmt.Sprintf("%s, limit %d", ps.name, limit), func(t *testing.T) {
				db := ps.open(t)
				q, args := threadQuery("group:ch-00-msg-0000", limit)
				plan := queryPlan(t, db, q, args...)
				assert.Contains(t, plan, "SEARCH messages USING INDEX idx_messages_thread (thread_id=?)", plan)
				assert.NotContains(t, plan, "idx_messages_channel", plan)
				assert.NotContains(t, plan, "SCAN messages", plan)
			})
		}
	}
}
