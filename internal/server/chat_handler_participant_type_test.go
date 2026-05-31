package server

// Publish-side test for the participant-type default half of ISSUE-0068
// (the [RFC 0011 participant-type amendment]): a REST chat request that
// omits `participant_type` must default it to "user" before publish.
// Split from chat_handler_test.go to keep that file under the 500-line
// cap (`scripts/checks/file_size.py --strict`).
//
// [RFC 0011 participant-type amendment]: ../../docs/rfcs/0011-amendment-participant-type-wire-propagation.md

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// TestHandleChat_DefaultsParticipantTypeToUser pins the ISSUE-0068 fix on
// the publish side: a REST chat request that omits `participant_type`
// must still carry `participant_type=user` into the inbound
// ChannelMessage.Metadata. REST chat is always a human talking to a
// persona, so an empty field defaults to "user" — not to the proto
// boundary's downstream "agent" fallback. Without this default the
// common chat case (no explicit participant_type) records the human peer
// as `other_participant_type=agent` in the relationship tier.
func TestHandleChat_DefaultsParticipantTypeToUser(t *testing.T) {
	srv, reg, router, store := chatTestServer(t)
	registerHealthyAgent(t, reg, "agent-x", "Agent X")
	publishReplyAfter(t, router, store, "bob", "agent-x", "ok", 20*time.Millisecond)

	// No ParticipantType field set on the request.
	body, _ := json.Marshal(chatRequest{
		Message: "Hi",
		UserID:  "bob",
	})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body)
	require.Equal(t, 200, rec.Code, "body=%s", rec.Body.String())

	dm, err := store.GetOrCreateDM(context.Background(), "bob", "agent-x")
	require.NoError(t, err)
	hist, err := store.GetHistory(context.Background(), dm.ID, 10, time.Time{})
	require.NoError(t, err)

	var inbound *channels.ChannelMessage
	for i := range hist {
		if hist[i].SenderID == "bob" {
			inbound = &hist[i]
			break
		}
	}
	require.NotNil(t, inbound, "inbound message must persist")
	require.NotNil(t, inbound.Metadata, "metadata must be populated")
	assert.Equal(t, "user", inbound.Metadata["participant_type"],
		"omitted participant_type must default to \"user\" for REST chat")
}

// TestHandleChat_RejectsInvalidParticipantType pins the input-hygiene half
// of the fix: an *explicit* `participant_type` outside the
// `{"agent", "user"}` vocabulary is rejected with 400 BAD_REQUEST before
// any side effect — parity with the gRPC SendChatMessage servicer, which
// already returns INVALID_ARGUMENT for the same input
// (`agents/participant.py::validate_participant_type`).
//
// Without this, an out-of-vocabulary value (a typo like "Human", or
// "customer") rides the wire verbatim and the agent's allowlist clamp
// (`record_close.py::extract_peer_from_interaction`) silently degrades it
// to "agent" — re-introducing the exact ISSUE-0068 silent-misclassification
// this PR exists to eliminate, just triggered by a bad value instead of an
// omitted one.
func TestHandleChat_RejectsInvalidParticipantType(t *testing.T) {
	srv, reg, _, _ := chatTestServer(t)
	registerHealthyAgent(t, reg, "agent-x", "Agent X")

	// No reply is staged: validation rejects before GetOrCreateDM and the
	// reply waiter, so the handler returns immediately. `timeout_seconds=1`
	// floors any hang to ~1s (504) should the guard ever regress, instead
	// of blocking on the 30s default.
	body, _ := json.Marshal(chatRequest{
		Message:         "Hi",
		UserID:          "bob",
		ParticipantType: "robot", // outside {"agent","user"}
		TimeoutSeconds:  1,
	})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body)

	require.Equal(t, 400, rec.Code, "invalid participant_type must be rejected; body=%s", rec.Body.String())
	var env struct {
		Error string `json:"error"`
		Code  string `json:"code"`
	}
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &env))
	assert.Equal(t, "BAD_REQUEST", env.Code, "rejection must use the BAD_REQUEST envelope code")
	// The message echoes the offending value — parity with the gRPC
	// SendChatMessage guard, whose `validate_participant_type` ValueError
	// renders the rejected value (`Invalid participant_type 'robot': ...`).
	// Echoing it tells the caller *which* value was rejected, not just that
	// the field was bad.
	assert.Contains(t, env.Error, "robot",
		"rejection message must echo the offending participant_type value")
}
