package channels

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// Channel-activity tracking (RFC 0048 console presence Tier 1): the router marks
// the members a publish is expected to draw a reply from — the responders, NOT
// the ingestion-only recipients — and clears each when its reply re-enters or
// when the TTL backstop fires. `GET /channels/{id}/activity` reads this so the
// web console shows an accurate "… is thinking" for every trigger, not only the
// turns it fired itself (Tier 0). These tests pin the router half end to end:
// Publish drives fanout synchronously, so the mark is visible on return.

func TestChannelActivity_MarksMentionedRespondersOnPublish(t *testing.T) {
	router, _, store := newRouterTest(t)
	ctx := context.Background()
	// All three agents are when_mentioned (mustCreateGroup's policy). alice
	// addresses two of them; the unmentioned one ingests the message but will
	// not reply, so it must NOT be marked as thinking.
	id := mustCreateGroup(t, store, "planning", "alice", "ember-owl", "crimson-fox", "quiet-mouse")

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
		Content:  "@ember-owl @crimson-fox your reads?",
		Mentions: []string{"ember-owl", "crimson-fox"},
	}, ""))

	assert.Equal(t, []string{"crimson-fox", "ember-owl"}, router.ChannelActivity(id),
		"only the two addressed responders are thinking; the unmentioned member is ingestion-only")
}

func TestChannelActivity_MarksAlwaysRespondersOnBroadcast(t *testing.T) {
	router, _, store := newRouterTest(t)
	ctx := context.Background()
	id := "group:standup"
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: id, Name: "standup", Type: ChannelTypeGroup}))
	require.NoError(t, store.AddMember(ctx, id, "alice", RespondNever))
	require.NoError(t, store.AddMember(ctx, id, "ember-owl", RespondAlways))
	require.NoError(t, store.AddMember(ctx, id, "crimson-fox", RespondAlways))

	// An undirected broadcast (no mentions) draws every always-responder.
	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "standup time",
	}, ""))

	assert.Equal(t, []string{"crimson-fox", "ember-owl"}, router.ChannelActivity(id))
}

func TestChannelActivity_ClearsResponderOnReply(t *testing.T) {
	router, _, store := newRouterTest(t)
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "ember-owl", "crimson-fox")

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
		Content:  "@ember-owl @crimson-fox your reads?",
		Mentions: []string{"ember-owl", "crimson-fox"},
	}, ""))
	require.Equal(t, []string{"crimson-fox", "ember-owl"}, router.ChannelActivity(id))

	// ember-owl's reply re-enters via Publish (sender = the agent) — it clears
	// from the thinking set; crimson-fox is still pending.
	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "ember-owl", Content: "my read is…",
	}, ""))

	assert.Equal(t, []string{"crimson-fox"}, router.ChannelActivity(id))
}

func TestChannelActivity_TTLExpiryClearsAStrandedResponder(t *testing.T) {
	router, _, store := newRouterTest(t)
	ctx := context.Background()
	now := time.Now()
	router.activityNow = func() time.Time { return now } // deterministic clock

	id := mustCreateGroup(t, store, "planning", "alice", "ember-owl")
	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
		Content: "@ember-owl ?", Mentions: []string{"ember-owl"},
	}, ""))
	require.Equal(t, []string{"ember-owl"}, router.ChannelActivity(id))

	// The agent never replies; once the entry ages past the TTL the read prunes
	// it, so a declined/silent turn can't strand the indicator (the
	// fire-and-forget fanout has no server-side await to clear it).
	now = now.Add(activityTTL + time.Second)
	assert.Empty(t, router.ChannelActivity(id))
}

func TestChannelActivity_EmptyForUntouchedChannel(t *testing.T) {
	router, _, _ := newRouterTest(t)
	assert.Empty(t, router.ChannelActivity("group:never-used"))
}
