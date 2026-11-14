package server

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// After a chat request times out, the next chat request on that DM gets its
// own answer, not the late answer to the one before it (ISSUE-0200).
//
// The issue's probe, through the REST route: chat 1 asks Q1 and times out;
// chat 2 asks Q2; the persona's late answer to Q1 arrives while chat 2 waits.
// Chat 2 used to return it, with no error, and the real answer to Q2 then
// found no request waiting.

// replyEcho waits for the person's message to reach the DM and returns the
// metadata an agent's reply to it carries: the id of the message it answers
// (ISSUE-0200). A chat request accepts only a reply that echoes its own
// message's id. A real agent takes that id from the event it is sent; these
// tests have no agent, so they read it from the channel.
func replyEcho(t *testing.T, store channels.ChannelStore, dmID, userID string) map[string]any {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for {
		msgs, err := store.GetHistory(context.Background(), dmID, 20, time.Time{})
		if err == nil {
			for _, m := range msgs { // newest first
				if m.SenderID == userID {
					return map[string]any{"in_reply_to": m.ID}
				}
			}
		}
		if time.Now().After(deadline) {
			t.Errorf("replyEcho: no message from %s reached %s", userID, dmID)
			return map[string]any{}
		}
		time.Sleep(2 * time.Millisecond)
	}
}

// questionsOn returns the ids of the messages `userID` has posted on the DM,
// oldest first, once there are `want` of them. It is also called from a
// goroutine, so it reports a failure and returns nil instead of stopping
// the test.
func questionsOn(t *testing.T, store channels.ChannelStore, dmID, userID string, want int) []string {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for {
		var ids []string
		msgs, err := store.GetHistory(context.Background(), dmID, 50, time.Time{})
		for i := len(msgs) - 1; i >= 0; i-- { // history is newest first
			if msgs[i].SenderID == userID {
				ids = append(ids, msgs[i].ID)
			}
		}
		if err == nil && len(ids) >= want {
			return ids
		}
		if time.Now().After(deadline) {
			t.Errorf("questionsOn: %d of %d message(s) from %s on %s (history error: %v)", len(ids), want, userID, dmID, err)
			return nil
		}
		time.Sleep(2 * time.Millisecond)
	}
}

func TestHandleChat_ALateAnswerIsNotReturnedToTheNextChat(t *testing.T) {
	tests := []struct {
		name string
		// lateMetadata is what the late answer to Q1 carries beside its echo.
		lateMetadata map[string]any
	}{
		{name: "a late answer"},
		// ISSUE-0244: a persona whose turn timed out publishes an error
		// reply, always after the request it answers has given up.
		{name: "a late error reply", lateMetadata: map[string]any{"reply_status": "error", "error_reason": "event_timeout"}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			srv, reg, router, store := chatTestServer(t)
			registerHealthyAgent(t, reg, "ember-owl", "Ember Owl")
			dm, err := store.GetOrCreateDM(context.Background(), "alice", "ember-owl")
			require.NoError(t, err)
			chat := func(message string, timeoutSeconds int32) *httptest.ResponseRecorder {
				body, _ := json.Marshal(chatRequest{Message: message, UserID: "alice", TimeoutSeconds: timeoutSeconds})
				return doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/ember-owl/chat", body)
			}
			say := func(content, inReplyTo string, extra map[string]any) {
				metadata := map[string]any{"in_reply_to": inReplyTo}
				for k, v := range extra {
					metadata[k] = v
				}
				require.NoError(t, router.Publish(context.Background(), channels.ChannelMessage{
					ID: uuid.NewString(), ChannelID: dm.ID, SenderID: "ember-owl",
					Content: content, Timestamp: time.Now().UTC(), Metadata: metadata,
				}, ""))
			}

			// Chat 1: the persona does not answer within the second it has.
			first := chat("Q1: what is the capital of France?", 1)
			require.Equal(t, http.StatusGatewayTimeout, first.Code, "body=%s", first.Body.String())

			// Chat 2 is waiting when the answer to Q1 lands, then its own.
			done := make(chan *httptest.ResponseRecorder, 1)
			go func() { done <- chat("Q2: what is 2+2?", 5) }()
			asked := questionsOn(t, store, dm.ID, "alice", 2)
			require.Len(t, asked, 2)
			say("A1: Paris", asked[0], tt.lateMetadata)
			select {
			case early := <-done:
				t.Fatalf("chat 2 returned on the late answer to chat 1: %s", early.Body.String())
			case <-time.After(100 * time.Millisecond):
			}
			say("A2: 4", asked[1], nil)

			second := <-done
			require.Equal(t, http.StatusOK, second.Code, "body=%s", second.Body.String())
			var resp chatResponse
			require.NoError(t, json.Unmarshal(second.Body.Bytes(), &resp))
			assert.Equal(t, "A2: 4", resp.Reply)
			assert.Equal(t, "ok", resp.ReplyStatus)
		})
	}
}

// An error reply that arrives in time still answers its own request: the
// person gets the error text with HTTP 200, as before.
func TestHandleChat_AnErrorReplyInTimeAnswersItsOwnChat(t *testing.T) {
	srv, reg, router, store := chatTestServer(t)
	registerHealthyAgent(t, reg, "ember-owl", "Ember Owl")
	dm, err := store.GetOrCreateDM(context.Background(), "alice", "ember-owl")
	require.NoError(t, err)
	go func() {
		asked := questionsOn(t, store, dm.ID, "alice", 1)
		if len(asked) == 0 {
			return
		}
		_ = router.Publish(context.Background(), channels.ChannelMessage{
			ID: uuid.NewString(), ChannelID: dm.ID, SenderID: "ember-owl",
			Content: "LLM provider error", Timestamp: time.Now().UTC(),
			Metadata: map[string]any{
				"in_reply_to": asked[0], "reply_status": "error", "error_reason": "provider_error",
			},
		}, "")
	}()

	body, _ := json.Marshal(chatRequest{Message: "Hi", UserID: "alice", TimeoutSeconds: 5})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/ember-owl/chat", body)

	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
	var resp chatResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, "LLM provider error", resp.Reply)
	assert.Equal(t, "error", resp.ReplyStatus)
}

// TestHandleChat_MultiMessageReplyReturnsFirst pins the single-shot
// semantics of `replyWaiter`: when an agent publishes multiple
// `SEND_CHANNEL_MESSAGE`s in response to a single chat turn (e.g. a
// `tool_call → tool_result → final_answer` plugin pattern), only the
// first message satisfies the waiter and is returned to the caller.
// Subsequent messages are persisted to the DM history but not
// delivered to the chat response.
func TestHandleChat_MultiMessageReplyReturnsFirst(t *testing.T) {
	srv, reg, router, store := chatTestServer(t)
	registerHealthyAgent(t, reg, "agent-x", "Agent X")

	// Publish two replies in quick succession.
	go func() {
		time.Sleep(15 * time.Millisecond)
		dm, err := store.GetOrCreateDM(context.Background(), "alice", "agent-x")
		if err != nil {
			t.Errorf("GetOrCreateDM failed: %v", err)
			return
		}
		_ = router.Publish(context.Background(), channels.ChannelMessage{
			ID:        uuid.NewString(),
			ChannelID: dm.ID,
			SenderID:  "agent-x",
			Content:   "first",
			Timestamp: time.Now().UTC(),
			Metadata:  replyEcho(t, store, dm.ID, "alice"),
		}, "")
		_ = router.Publish(context.Background(), channels.ChannelMessage{
			ID:        uuid.NewString(),
			ChannelID: dm.ID,
			SenderID:  "agent-x",
			Content:   "second",
			Timestamp: time.Now().UTC(),
			Metadata:  replyEcho(t, store, dm.ID, "alice"),
		}, "")
	}()

	body, _ := json.Marshal(chatRequest{Message: "Hi", UserID: "alice"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body)
	require.Equal(t, 200, rec.Code, "body=%s", rec.Body.String())

	var resp chatResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, "first", resp.Reply, "single-shot waiter must surface the first reply")

	// Allow the second publish to complete before snapshotting.
	require.Eventually(t, func() bool {
		dm, err := store.GetOrCreateDM(context.Background(), "alice", "agent-x")
		if err != nil {
			return false
		}
		hist, err := store.GetHistory(context.Background(), dm.ID, 10, time.Time{})
		if err != nil {
			return false
		}
		// inbound + first + second = 3
		return len(hist) == 3
	}, 2*time.Second, 20*time.Millisecond, "all three messages must persist to the DM")
}
