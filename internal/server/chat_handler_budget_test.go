package server

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// ISSUE-0065 — chat handler must honour the agent-side reply_status
// discriminator on the channel-receive path so wallet budget denials
// surface as HTTP 200 + reply_status="error" instead of HTTP 504
// DEADLINE_EXCEEDED. Companion tests for the Python-side fix in
// agents/server_servicers.py::_dispatch_channel_event live in
// agents/tests/test_chat_path_budget_denial.py.

// TestHandleChat_ReplyMetadataReplyStatusErrorSurfacedAs200 pins ISSUE-0065:
// when the agent's reply on the DM carries `Metadata["reply_status"] = "error"`
// (the channel-receive path's structured-error envelope under wallet budget
// denial), the chat handler MUST render HTTP 200 + `reply_status="error"` +
// the denial message in `reply`, NOT HTTP 504 `DEADLINE_EXCEEDED`.
//
// Per MT-COST-003 Step 2:
//
//	HTTP status of the denied turn is 200 (not 500, not 503); reply_status
//	equals "error"; reply text references budget or lease.
//
// Surfacing budget denials as 5xx would conflate them with chat-server
// failures and break dashboard incident routing.
func TestHandleChat_ReplyMetadataReplyStatusErrorSurfacedAs200(t *testing.T) {
	srv, reg, router, store := chatTestServer(t)
	registerHealthyAgent(t, reg, "ember-owl", "Ember Owl")

	denialMsg := "per_agent budget exceeded: spent=0.017259, limit=0.100000, estimated=0.084555"

	// Simulate the agent's error-envelope reply on the DM (what
	// `_dispatch_channel_event` publishes after catching BudgetExceededError).
	go func() {
		time.Sleep(20 * time.Millisecond)
		dm, err := store.GetOrCreateDM(context.Background(), "alice", "ember-owl")
		if err != nil {
			t.Errorf("publishErrorReply: GetOrCreateDM failed: %v", err)
			return
		}
		if err := router.Publish(context.Background(), channels.ChannelMessage{
			ID:        uuid.NewString(),
			ChannelID: dm.ID,
			SenderID:  "ember-owl",
			Content:   denialMsg,
			Timestamp: time.Now().UTC(),
			Metadata: map[string]any{
				"reply_status": "error",
				"error_reason": "budget_exceeded",
			},
		}, ""); err != nil {
			t.Errorf("publishErrorReply: Publish failed: %v", err)
		}
	}()

	body, _ := json.Marshal(chatRequest{Message: "How are you?", UserID: "alice"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/ember-owl/chat", body)
	require.Equal(t, 200, rec.Code,
		"ISSUE-0065: wallet-denied chat MUST return 200, not 504 — body=%s",
		rec.Body.String())

	var resp chatResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, "error", resp.ReplyStatus,
		"ISSUE-0065: reply.Metadata[reply_status]=error must surface in JSON envelope")
	assert.Equal(t, denialMsg, resp.Reply,
		"ISSUE-0065: the wallet's LeaseDenied.message must reach the caller")
	assert.Contains(t, resp.Reply, "budget",
		"MT-COST-003: reply text must reference budget or lease, not a generic 'Internal error'")
}

// TestHandleChat_ReplyMetadataUnsetDefaultsToReplyStatusOK pins that the
// new metadata-driven branch does not regress the happy path: a reply
// without metadata (or with metadata that does not carry `reply_status`)
// still renders as `reply_status="ok"`.
func TestHandleChat_ReplyMetadataUnsetDefaultsToReplyStatusOK(t *testing.T) {
	srv, reg, router, store := chatTestServer(t)
	registerHealthyAgent(t, reg, "ember-owl", "Ember Owl")

	go func() {
		time.Sleep(20 * time.Millisecond)
		dm, err := store.GetOrCreateDM(context.Background(), "alice", "ember-owl")
		if err != nil {
			t.Errorf("GetOrCreateDM failed: %v", err)
			return
		}
		_ = router.Publish(context.Background(), channels.ChannelMessage{
			ID:        uuid.NewString(),
			ChannelID: dm.ID,
			SenderID:  "ember-owl",
			Content:   "Hello",
			Timestamp: time.Now().UTC(),
			Metadata:  map[string]any{"some_other_key": "value"},
		}, "")
	}()

	body, _ := json.Marshal(chatRequest{Message: "Hi", UserID: "alice"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/ember-owl/chat", body)
	require.Equal(t, 200, rec.Code)

	var resp chatResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, "ok", resp.ReplyStatus,
		"unrelated metadata must not flip reply_status to error")
}
