package channels

import (
	"context"
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// PR 4b router tests — pin the pre-resolution of `thread_parent_sender_id`
// during [ChannelRouter.Publish] and the per-recipient propagation
// through [DispatchEnvelope]. Split from `router_test.go` to keep that
// file under the 500-line review-friendly cap.

// TestChannelRouter_Publish_PreResolvesThreadParentSenderID pins the
// RFC 0011 PR 4b contract: when a message carries a `thread_id`, the
// router resolves the parent's `sender_id` once per publish and passes
// it through the [DispatchEnvelope] to every recipient. This amortises
// the lookup across fanout and lets the receiver's response gate decide
// thread-reply-to-self without an extra REST call.
func TestChannelRouter_Publish_PreResolvesThreadParentSenderID(t *testing.T) {
	router, disp, store := newRouterTest(t)
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob", "carol")

	parentID := uuid.NewString()
	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID: parentID, ChannelID: id, SenderID: "carol", Content: "parent",
	}))

	// alice replies inside carol's thread; bob and carol receive the dispatch.
	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
		Content: "reply", ThreadID: parentID,
	}, ""))

	calls := disp.snapshot()
	require.Len(t, calls, 2, "fanout to bob+carol")
	for _, c := range calls {
		assert.Equal(t, "carol", c.threadParentSenderID,
			"every recipient sees the same pre-resolved thread_parent_sender_id (recipient=%s)",
			c.participantID)
	}
}

// TestChannelRouter_Publish_NonThreadEventLeavesParentEmpty pins the
// negative case: a non-thread publish must not pay the GetMessage
// lookup nor surface any non-empty thread_parent_sender_id on the wire.
func TestChannelRouter_Publish_NonThreadEventLeavesParentEmpty(t *testing.T) {
	router, disp, store := newRouterTest(t)
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob")

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "x",
	}, ""))

	calls := disp.snapshot()
	require.Len(t, calls, 1)
	assert.Empty(t, calls[0].threadParentSenderID,
		"non-thread events MUST leave thread_parent_sender_id empty")
}
