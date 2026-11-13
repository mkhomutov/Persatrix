package channels

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// A reply answers the request it echoes, and no other (ISSUE-0200).
//
// A chat request publishes the person's message into the DM and waits for
// the persona's reply. The wait used to be matched by channel and sender
// only. So when one request timed out, the persona's late answer to it was
// returned to the next request on that DM, and every answer after it was one
// question behind. The reply now carries the id of the message it answers
// (`in_reply_to`, which the agent copies from the event it was sent), and a
// wait accepts only a reply that carries the id of its own request.

const testDM = "dm:alice:ember-owl"

// replyTo is a message from `sender` that echoes `inReplyTo` the way an
// agent's publish does; an empty `inReplyTo` echoes nothing.
func replyTo(channelID, sender, inReplyTo, content string) ChannelMessage {
	msg := ChannelMessage{
		ID: uuid.NewString(), ChannelID: channelID, SenderID: sender, Content: content,
	}
	if inReplyTo != "" {
		msg.Metadata = map[string]any{"in_reply_to": inReplyTo}
	}
	return msg
}

func TestReplyWaiter_AcceptsOnlyAReplyToItsOwnRequest(t *testing.T) {
	tests := []struct {
		name    string
		reply   ChannelMessage
		matches bool
	}{
		{name: "a reply to this request", reply: replyTo(testDM, "ember-owl", "q2", "A2"), matches: true},
		{name: "a late reply to the request before it", reply: replyTo(testDM, "ember-owl", "q1", "A1")},
		{name: "a message that replies to nothing", reply: replyTo(testDM, "ember-owl", "", "by the way")},
		{name: "a reply to this request from someone else", reply: replyTo(testDM, "iron-fox", "q2", "A2")},
		{name: "a reply to this request in another channel", reply: replyTo("dm:bob:ember-owl", "ember-owl", "q2", "A2")},
		{
			name: "an echo that is not text",
			reply: ChannelMessage{
				ID: "r", ChannelID: testDM, SenderID: "ember-owl",
				Metadata: map[string]any{"in_reply_to": 2},
			},
		},
		{
			name: "an empty echo",
			reply: ChannelMessage{
				ID: "r", ChannelID: testDM, SenderID: "ember-owl",
				Metadata: map[string]any{"in_reply_to": ""},
			},
		},
		{
			name:  "an echo that only starts with this request's id",
			reply: replyTo(testDM, "ember-owl", "q2-and-more", "A2"),
		},
		{name: "an echo in another letter case", reply: replyTo(testDM, "ember-owl", "Q2", "A2")},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			w := newReplyWaiter()
			ch, cancel, err := w.Register(testDM, "ember-owl", "q2")
			require.NoError(t, err)
			defer cancel()

			assert.Equal(t, tt.matches, w.Notify(tt.reply))

			if tt.matches {
				assert.Equal(t, tt.reply.ID, (<-ch).ID)
				return
			}
			assert.Empty(t, ch, "nothing was handed to the request")
			// The request is still waiting: its own reply is accepted after
			// the one that was not for it.
			own := replyTo(testDM, "ember-owl", "q2", "A2")
			require.True(t, w.Notify(own), "the waiter must have stayed registered")
			assert.Equal(t, own.ID, (<-ch).ID)
		})
	}
}

// A request with no id can be answered by nothing: not by a reply that
// echoes nothing either. Both callers always have an id.
func TestReplyWaiter_ARequestWithNoIDMatchesNothing(t *testing.T) {
	w := newReplyWaiter()
	ch, cancel, err := w.Register(testDM, "ember-owl", "")
	require.NoError(t, err)
	defer cancel()

	assert.False(t, w.Notify(replyTo(testDM, "ember-owl", "", "A")))
	assert.False(t, w.Notify(ChannelMessage{
		ChannelID: testDM, SenderID: "ember-owl", Metadata: map[string]any{"in_reply_to": ""},
	}))
	assert.Empty(t, ch)
}

// A request's cancel removes its own wait and no other. Once its reply has
// been delivered and the next request has registered under the same channel
// and sender, the first request's deferred cancel must leave that wait alone.
func TestReplyWaiter_CancelLeavesTheNextRequestsWaitAlone(t *testing.T) {
	w := newReplyWaiter()
	_, cancelFirst, err := w.Register(testDM, "ember-owl", "q1")
	require.NoError(t, err)
	require.True(t, w.Notify(replyTo(testDM, "ember-owl", "q1", "A1")))

	second, cancelSecond, err := w.Register(testDM, "ember-owl", "q2")
	require.NoError(t, err)
	defer cancelSecond()
	cancelFirst()

	own := replyTo(testDM, "ember-owl", "q2", "A2")
	require.True(t, w.Notify(own), "the first request's cancel removed the second request's wait")
	assert.Equal(t, own.ID, (<-second).ID)
}

// The issue's probe, at the router: chat 1 times out, chat 2 is asked, and
// the late answer to chat 1 arrives while chat 2 waits.
func TestPublishAndAwait_ALateAnswerIsNotReturnedToTheNextRequest(t *testing.T) {
	tests := []struct {
		name string
		// lateMetadata is added to the late answer to question 1.
		lateMetadata map[string]any
	}{
		{name: "a late answer"},
		// ISSUE-0244: an error reply is a message like any other to the
		// waiter, so a late one used to answer the next request too.
		{name: "a late error reply", lateMetadata: map[string]any{"reply_status": "error"}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			store := newTestStore(t, SQLiteOptions{})
			router := NewChannelRouter(store, &recordingDispatcher{}, zap.NewNop(), nil)
			ctx := context.Background()
			dm, err := store.GetOrCreateDM(ctx, "alice", "ember-owl")
			require.NoError(t, err)
			ask := func(text string) ChannelMessage {
				return ChannelMessage{ID: uuid.NewString(), ChannelID: dm.ID, SenderID: "alice", Content: text}
			}

			// Chat 1: nobody answers in time.
			q1 := ask("Q1: what is the capital of France?")
			_, err = router.PublishAndAwait(ctx, q1, "ember-owl", 20*time.Millisecond)
			require.ErrorIs(t, err, ErrChatTimeout)

			// Chat 2 is waiting when the answer to chat 1 lands, and then
			// its own.
			q2 := ask("Q2: what is 2+2?")
			late := replyTo(dm.ID, "ember-owl", q1.ID, "A1: Paris")
			for k, v := range tt.lateMetadata {
				late.Metadata[k] = v
			}
			own := replyTo(dm.ID, "ember-owl", q2.ID, "A2: 4")
			go func() {
				time.Sleep(20 * time.Millisecond)
				_ = router.Publish(ctx, late, "")
				time.Sleep(20 * time.Millisecond)
				_ = router.Publish(ctx, own, "")
			}()

			got, err := router.PublishAndAwait(ctx, q2, "ember-owl", 2*time.Second)

			require.NoError(t, err)
			assert.Equal(t, "A2: 4", got.Content)
			assert.Equal(t, own.ID, got.ID)
			// The channel's own record keeps all four, in the order they
			// were said.
			history, err := store.GetHistory(ctx, dm.ID, 10, time.Time{})
			require.NoError(t, err)
			var said []string
			for i := len(history) - 1; i >= 0; i-- {
				said = append(said, history[i].Content)
			}
			assert.Equal(t, []string{q1.Content, q2.Content, "A1: Paris", "A2: 4"}, said)
		})
	}
}

// A reply that answers this request is returned whatever else it carries:
// an error reply that arrives in time still answers its own request.
func TestPublishAndAwait_ReturnsTheReplyToThisRequest(t *testing.T) {
	tests := []struct {
		name     string
		metadata map[string]any
	}{
		{name: "an answer"},
		{name: "an error reply", metadata: map[string]any{"reply_status": "error", "error_reason": "event_timeout"}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			store := newTestStore(t, SQLiteOptions{})
			router := NewChannelRouter(store, &recordingDispatcher{}, zap.NewNop(), nil)
			ctx := context.Background()
			dm, err := store.GetOrCreateDM(ctx, "alice", "ember-owl")
			require.NoError(t, err)
			question := ChannelMessage{ID: uuid.NewString(), ChannelID: dm.ID, SenderID: "alice", Content: "Q"}
			reply := replyTo(dm.ID, "ember-owl", question.ID, "A")
			for k, v := range tt.metadata {
				reply.Metadata[k] = v
			}
			go func() {
				time.Sleep(10 * time.Millisecond)
				_ = router.Publish(ctx, reply, "")
			}()

			got, err := router.PublishAndAwait(ctx, question, "ember-owl", 2*time.Second)

			require.NoError(t, err)
			assert.Equal(t, reply.ID, got.ID)
			for k, v := range tt.metadata {
				assert.Equal(t, v, got.Metadata[k])
			}
		})
	}
}
