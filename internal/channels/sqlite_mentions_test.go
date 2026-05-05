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

// PR #249 deep-review Nice-to-Have #3: pin the loop short-circuit
// behavior. The implementation must reject on the FIRST invalid mention
// and report its index, not scan the whole slice. Without this assertion
// a future refactor that collects all errors into a multi-error would
// silently change the surfaced index without breaking any other test.
func TestSQLiteStore_PublishMessage_RejectsFirstInvalidMention(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob")

	err := store.PublishMessage(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
		Content: "hi",
		// Both entries are invalid; the first (index 0) is what the
		// loop must report. The second invalid entry is a sentinel to
		// catch any implementation that aggregates errors across all
		// entries instead of returning on first failure.
		Mentions: []string{"", "also:bad"},
	})
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidParticipantID)
	assert.Contains(t, err.Error(), "mentions[0]",
		"loop must short-circuit on the first invalid mention")
	assert.NotContains(t, err.Error(), "mentions[1]",
		"second invalid mention must not be scanned (short-circuit guarantee)")
}

// PR #249 deep-review Nice-to-Have #2 (regression pin): the per-mention
// error must surface the offending value alongside the index so a 422
// returned to a REST caller pinpoints which input the operator must
// correct. The value is contributed by `validateParticipantID` itself
// (via “%q“) and bubbles up through the loop's “mentions[%d]: %w“
// wrap. This test exists so a future refactor of either layer that
// drops the value (e.g. switching the inner error to a bare sentinel,
// or rewriting the outer wrap to omit “%w“) fails immediately rather
// than silently degrading the operator-facing error.
func TestSQLiteStore_PublishMessage_MentionErrorIncludesOffendingValue(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob")

	err := store.PublishMessage(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
		Content:  "hi",
		Mentions: []string{"bob", "alice:bob"},
	})
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidParticipantID)
	// %q-quoted form is what the implementation contracts; substring
	// match is robust to surrounding wrapping changes.
	assert.Contains(t, err.Error(), `"alice:bob"`,
		"error must echo the offending mention value (quoted)")
	assert.Contains(t, err.Error(), "mentions[1]",
		"index must remain present alongside the value")
}
