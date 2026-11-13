package channels

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
	"go.uber.org/zap/zaptest/observer"
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
		name string
		// request is the id the wait is registered under; "q2" when empty.
		request string
		reply   ChannelMessage
		matches bool
	}{
		{name: "a reply to this request", reply: replyTo(testDM, "ember-owl", "q2", "A2"), matches: true},
		{name: "a late reply to the request before it", reply: replyTo(testDM, "ember-owl", "q1", "A1")},
		{name: "a message that replies to nothing", reply: replyTo(testDM, "ember-owl", "", "by the way")},
		{name: "a reply to this request from someone else", reply: replyTo(testDM, "iron-fox", "q2", "A2")},
		{name: "a reply to this request in another channel", reply: replyTo("dm:bob:ember-owl", "ember-owl", "q2", "A2")},
		{
			// The request's id is "2", so an echo read as its text would match.
			name: "an echo that is a number, not text", request: "2",
			reply: ChannelMessage{
				ID: "r", ChannelID: testDM, SenderID: "ember-owl",
				Metadata: map[string]any{"in_reply_to": 2},
			},
		},
		{
			name: "an echo that is a number with a fraction, not text", request: "2",
			reply: ChannelMessage{
				ID: "r", ChannelID: testDM, SenderID: "ember-owl",
				Metadata: map[string]any{"in_reply_to": float64(2)},
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
			request := tt.request
			if request == "" {
				request = "q2"
			}
			w := newReplyWaiter()
			ch, cancel, err := w.Register(testDM, "ember-owl", request)
			require.NoError(t, err)
			defer cancel()

			// Fatal on a mismatch: a reply that was not handed over would
			// leave the receive below waiting for ever.
			require.Equal(t, tt.matches, w.Notify(tt.reply))

			if tt.matches {
				assert.Equal(t, tt.reply.ID, (<-ch).ID)
				return
			}
			assert.Empty(t, ch, "nothing was handed to the request")
			// The request is still waiting: its own reply is accepted after
			// the one that was not for it.
			own := replyTo(testDM, "ember-owl", request, "A2")
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
				// Once question 2 is stored its wait is registered (the
				// wait comes first), so the order below does not depend on
				// how long anything takes.
				deadline := time.Now().Add(5 * time.Second)
				for time.Now().Before(deadline) {
					if _, err := store.GetMessage(ctx, q2.ID); err == nil {
						break
					}
					time.Sleep(time.Millisecond)
				}
				_ = router.Publish(ctx, late, "")
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

// Why a message was not handed to a waiting request (the review of
// ISSUE-0200). A reply that names no message looks, from outside, exactly
// like a slow agent: the request times out and nothing says why.
func TestReplyWaiter_SaysWhatItDidWithAMessage(t *testing.T) {
	tests := []struct {
		name  string
		reply ChannelMessage
		want  notifyOutcome
	}{
		{"a reply to the waiting request", replyTo(testDM, "ember-owl", "q2", "A2"), notifyMatched},
		{"a message from the awaited sender that names no message", replyTo(testDM, "ember-owl", "", "A2"), notifyNoEcho},
		{"a reply from the awaited sender to another message", replyTo(testDM, "ember-owl", "q1", "A1"), notifyOtherRequest},
		{"a message from someone nobody waits for", replyTo(testDM, "iron-fox", "", "hello"), notifyNoWaiter},
		{"a message on a channel nobody waits on", replyTo("dm:bob:ember-owl", "ember-owl", "q2", "A2"), notifyNoWaiter},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			w := newReplyWaiter()
			_, cancel, err := w.Register(testDM, "ember-owl", "q2")
			require.NoError(t, err)
			defer cancel()

			assert.Equal(t, tt.want, w.notify(tt.reply))
		})
	}
}

// The router says so in its log. An agent older than the orchestrator
// sends no `in_reply_to`: every chat request to it would time out, and the
// WARN below is the only thing that tells that apart from a slow agent.
func TestPublishAndAwait_LogsAMessageItCouldNotHandToTheWaitingRequest(t *testing.T) {
	const noEcho = "channels: a message from a sender a request is waiting for names no message it answers; it was not handed to that request"
	const otherEcho = "channels: a message from a sender a request is waiting for answers another message; the request stays open"
	tests := []struct {
		name      string
		inReplyTo func(q ChannelMessage) string
		wantWarn  int
		wantDebug int
	}{
		{"a reply that names no message", func(ChannelMessage) string { return "" }, 1, 0},
		{"a reply to another message", func(ChannelMessage) string { return "some-earlier-question" }, 0, 1},
		{"a reply to the request", func(q ChannelMessage) string { return q.ID }, 0, 0},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			core, logs := observer.New(zapcore.DebugLevel)
			store := newTestStore(t, SQLiteOptions{})
			router := NewChannelRouter(store, &recordingDispatcher{}, zap.New(core), nil)
			ctx := context.Background()
			dm, err := store.GetOrCreateDM(ctx, "alice", "ember-owl")
			require.NoError(t, err)
			q := ChannelMessage{ID: uuid.NewString(), ChannelID: dm.ID, SenderID: "alice", Content: "Q"}
			reply := replyTo(dm.ID, "ember-owl", tt.inReplyTo(q), "A")
			go func() {
				deadline := time.Now().Add(5 * time.Second)
				for time.Now().Before(deadline) {
					if _, err := store.GetMessage(ctx, q.ID); err == nil {
						break
					}
					time.Sleep(time.Millisecond)
				}
				_ = router.Publish(ctx, reply, "")
			}()

			_, err = router.PublishAndAwait(ctx, q, "ember-owl", 300*time.Millisecond)

			matched := tt.wantWarn == 0 && tt.wantDebug == 0
			if matched {
				require.NoError(t, err)
			} else {
				require.ErrorIs(t, err, ErrChatTimeout)
			}
			warns := logs.FilterMessage(noEcho).All()
			require.Len(t, warns, tt.wantWarn)
			assert.Len(t, logs.FilterMessage(otherEcho).All(), tt.wantDebug)
			for _, entry := range warns {
				assert.Equal(t, zapcore.WarnLevel, entry.Level)
				fields := entry.ContextMap()
				assert.Equal(t, dm.ID, fields["channel_id"])
				assert.Equal(t, "ember-owl", fields["sender_id"])
				assert.Equal(t, reply.ID, fields["message_id"])
			}
			// The person's own question is from a sender nobody waits for:
			// it is never what the log is about.
			for _, entry := range logs.All() {
				assert.NotEqual(t, q.ID, entry.ContextMap()["message_id"])
			}
		})
	}
}
