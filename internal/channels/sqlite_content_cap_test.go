package channels

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// ISSUE-0050 (PR #231 review NTH, deferred to RFC 0011 PR 8 close-out):
// PublishMessage must reject messages whose Content exceeds
// MaxMessageContentBytes. Agent-side validation enforces a 4000-codepoint
// cap upstream, but the unauthenticated REST publish surface is reachable
// directly. This store-boundary cap is defense-in-depth.
//
// MaxMessageContentBytes is sized at 4× the upstream codepoint cap so a
// well-formed agent submission near the 4000-char limit (UTF-8 worst case
// 4 bytes/codepoint) still passes. Anything materially larger is rejected
// before the transaction opens — no row is inserted, the cap-prune path
// is not triggered, and the per-recipient gRPC fanout never sees the
// payload.
func TestSQLiteStore_PublishMessage_RejectsOversizedContent(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob")

	oversized := strings.Repeat("x", MaxMessageContentBytes+1)

	err := store.PublishMessage(ctx, ChannelMessage{
		ID:        uuid.NewString(),
		ChannelID: id,
		SenderID:  "alice",
		Content:   oversized,
	})
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrMessageContentTooLarge,
		"PublishMessage must surface ErrMessageContentTooLarge so the REST handler can map it to 413")

	// No row inserted — cap rejection MUST fire before the transaction opens
	// so the cap-prune path and the post-publish lookup are not reached.
	hist, err := store.GetHistory(ctx, id, 10, time.Time{})
	require.NoError(t, err)
	assert.Empty(t, hist, "rejected publish must not leave a row behind")
}

// Boundary: a publish at exactly the cap is accepted. Pins the inclusive
// vs. exclusive contract so a future off-by-one refactor (e.g. switching
// from `>` to `>=`) breaks loudly rather than silently rejecting
// well-formed agent submissions sitting at the upstream 4000-codepoint
// worst-case.
func TestSQLiteStore_PublishMessage_AcceptsContentAtCap(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice")

	atCap := strings.Repeat("x", MaxMessageContentBytes)

	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID:        uuid.NewString(),
		ChannelID: id,
		SenderID:  "alice",
		Content:   atCap,
	}))

	hist, err := store.GetHistory(ctx, id, 10, time.Time{})
	require.NoError(t, err)
	require.Len(t, hist, 1)
	assert.Equal(t, atCap, hist[0].Content)
}

// Cap is in BYTES, not codepoints — the upstream agent layer enforces the
// codepoint count, so the store-boundary cap measures the actual storage
// and wire-fanout cost. A multi-byte UTF-8 string that fits within the
// byte cap must be accepted.
func TestSQLiteStore_PublishMessage_CapMeasuresBytesNotCodepoints(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice")

	// "🦊" is 4 UTF-8 bytes; 4000 of them = 16000 bytes, which is < the cap.
	// Codepoint count is 4000 (matches the upstream char cap exactly).
	multibyte := strings.Repeat("🦊", 4000)
	require.LessOrEqual(t, len(multibyte), MaxMessageContentBytes,
		"sanity: pre-cap multibyte string must fit within the byte budget")

	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID:        uuid.NewString(),
		ChannelID: id,
		SenderID:  "alice",
		Content:   multibyte,
	}))
}
