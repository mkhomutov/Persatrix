package server

import (
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestHandleChat_LegacySessionIDJSONKeyRejected pins the v0.3.1 break:
// legacy `"session_id"` JSON key (pre-RFC-0031-OQ-8 rename) fails loud
// at 400 via `decodeJSON`'s `DisallowUnknownFields`. Operators see a
// clear error rather than a silently-rebound fresh session id.
//
// Pins three layers — HTTP status, error-message substring, AND the
// machine-readable `errorResponse.Code` envelope — so a regression that
// flips the status code while keeping the substring (or vice versa)
// still fails the test. (PR #333 review fix: finding L2.)
func TestHandleChat_LegacySessionIDJSONKeyRejected(t *testing.T) {
	srv, reg, _, _ := chatTestServer(t)
	registerHealthyAgent(t, reg, "agent-x", "Agent X")

	body := []byte(`{"message":"Hi","user_id":"alice","session_id":"legacy-sess"}`)
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body)
	assert.Equal(t, 400, rec.Code, "body=%s", rec.Body.String())
	assert.Contains(t, rec.Body.String(), "invalid or malformed JSON body")

	var env errorResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &env))
	assert.Equal(t, "BAD_REQUEST", env.Code, "error envelope code must be machine-readable BAD_REQUEST, not just a free-text message")
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
