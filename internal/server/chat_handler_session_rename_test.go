package server

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// TestHandleChat_SessionIDIsOperatorNamespace pins the RFC 0031 Phase 3
// (PR 4) fulfilment of the key the v0.3.1 rename reserved: `session_id` on the
// chat body is now the operator-namespace session override, NOT the RFC 0016
// chat token (which lives on `chat_session_id`). It is accepted — no longer
// rejected by `decodeJSON`'s `DisallowUnknownFields` — and stamps the
// persisted inbound row, so a `persatrix chat --session run-arc-3` re-binds the
// conversation to that session (RFC 0031 OQ #1 resolution 1a). This supersedes
// the v0.3.1-era `TestHandleChat_LegacySessionIDJSONKeyRejected`, which guarded
// the interim window before this operator field existed.
func TestHandleChat_SessionIDIsOperatorNamespace(t *testing.T) {
	srv, reg, router, store := chatTestServer(t)
	registerHealthyAgent(t, reg, "agent-x", "Agent X")
	publishReplyAfter(t, router, store, "alice", "agent-x", "ok", 20*time.Millisecond)

	body := []byte(`{"message":"Hi","user_id":"alice","session_id":"run-arc-3"}`)
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body)
	require.Equal(t, 200, rec.Code, "session_id is now an accepted operator field; body=%s", rec.Body.String())

	dm, err := store.GetOrCreateDM(context.Background(), "alice", "agent-x")
	require.NoError(t, err)
	hist, err := store.GetHistory(context.Background(), dm.ID, 10, time.Time{})
	require.NoError(t, err)

	var inbound *channels.ChannelMessage
	for i := range hist {
		if hist[i].SenderID == "alice" {
			inbound = &hist[i]
			break
		}
	}
	require.NotNil(t, inbound, "inbound message must persist")
	assert.Equal(t, "run-arc-3", inbound.SessionID,
		"the operator-namespace session_id override must stamp the inbound row")
}

// TestHandleChat_InvalidSessionOverride_Rejected pins the fail-loud boundary
// check (PR #469 deep-review finding 1): an operator `session_id` carrying a
// control / non-ASCII byte cannot ride the gRPC `persatrix-session` metadata
// header and would otherwise silently fail the dispatch. The handler rejects it
// with a 400 before publishing, consistent with the publish path
// (TestPublish_InvalidSessionOverride_Rejected).
//
// It also rejects *before any side effect*: the agent is healthy, so the
// pre-fix handler would have already opened the DM channel (and demoted the
// user's membership) by the time the override was validated. The override check
// is hoisted above `GetOrCreateDM`, so a rejected chat leaves no DM behind —
// parity with `TestPublish_InvalidSessionOverride_Rejected`, which asserts no
// row is persisted (PR #469 deep-review finding 1, follow-up).
func TestHandleChat_InvalidSessionOverride_Rejected(t *testing.T) {
	srv, reg, _, store := chatTestServer(t)
	registerHealthyAgent(t, reg, "agent-x", "Agent X")

	body, _ := json.Marshal(chatRequest{Message: "Hi", UserID: "alice", SessionID: "bad\nid"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body)
	require.Equal(t, 400, rec.Code, "a malformed session_id must fail loud; body=%s", rec.Body.String())

	var env errorResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &env))
	assert.Equal(t, "BAD_REQUEST", env.Code,
		"error envelope code must be machine-readable BAD_REQUEST")

	// No DM channel was created: the override is validated before the
	// GetOrCreateDM side effect, mirroring the publish path's reject-before-
	// persist contract.
	chans, err := store.ListChannels(context.Background(), 100, "")
	require.NoError(t, err)
	assert.Empty(t, chans,
		"a rejected chat must not create a DM channel as a side effect (reject before GetOrCreateDM)")
}

// TestHandleChat_ChatSessionIDJSONTagOnResponse pins the wire-name on
// the response side: a v0.3.1 client decoding the chat response sees
// `chat_session_id`, not `session_id`. Guards against accidental
// reversion of the struct-tag rename in `internal/server/types.go`.
func TestHandleChat_ChatSessionIDJSONTagOnResponse(t *testing.T) {
	resp := chatResponse{
		Reply:         "hi",
		ChatSessionID: "abc-123",
		AgentID:       "agent-x",
	}
	raw, err := json.Marshal(resp)
	require.NoError(t, err)

	var asMap map[string]any
	require.NoError(t, json.Unmarshal(raw, &asMap))
	assert.Equal(t, "abc-123", asMap["chat_session_id"])
	_, hasLegacy := asMap["session_id"]
	assert.False(t, hasLegacy, "legacy `session_id` JSON key must not appear on the wire (RFC 0031 OQ #8)")
}
