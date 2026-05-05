package channels

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// PR #231 deep review SF-3 (RFC 0011 PR 4a-ii-α): every mention id in
// `ChannelMessage.Mentions` must round-trip through `validateParticipantID`
// before INSERT. Without this check, the wire boundary's mentions cap is
// the only filter and junk values (whitespace, ":", non-ASCII) would
// reach the response gate in PR 4b. The contract is the same regex used
// for sender/member ids (`^[A-Za-z0-9][A-Za-z0-9_-]*$`), so the new
// check is purely additive.
func TestSQLiteStore_PublishMessage_RejectsInvalidMention(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob")

	cases := []struct {
		name    string
		mention string
	}{
		{"empty", ""},
		{"contains_colon", "alice:bob"},
		{"whitespace", "alice bob"},
		{"non_ascii", "alicé"},
		{"leading_dash", "-alice"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := store.PublishMessage(ctx, ChannelMessage{
				ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
				Content:  "hi",
				Mentions: []string{"bob", tc.mention},
			})
			require.Error(t, err)
			assert.ErrorIs(t, err, ErrInvalidParticipantID)
			assert.Contains(t, err.Error(), "mentions[1]",
				"error must identify the offending index")
		})
	}
}

// PR #231 review SF-3 (RFC 0011 PR 4a-ii-α): valid mentions must round-trip
// unchanged after the validation loop is added. Guards against the obvious
// regression where an over-eager check rejects legitimate ids.
func TestSQLiteStore_PublishMessage_AcceptsValidMentions(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob", "User_1")

	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
		Content:  "hi",
		Mentions: []string{"bob", "User_1"},
	}))

	hist, err := store.GetHistory(ctx, id, 10, time.Time{})
	require.NoError(t, err)
	require.Len(t, hist, 1)
	assert.Equal(t, []string{"bob", "User_1"}, hist[0].Mentions)
}
