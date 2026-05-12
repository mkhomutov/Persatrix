package server

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// REST-boundary cascade_depth tests for the publish handler.
// Counterpart router-side tests live in
// [internal/channels/router_cascade_depth_test.go]; the wire-shape
// proto-field test lives in
// [internal/channels/grpc_dispatcher_test.go].
// RFC 0011 amendment 'Cascade-depth wire propagation':
// docs/rfcs/0011-amendment-cascade-depth-wire-propagation.md

// TestChannels_PublishMessage_CascadeDepth_DefaultZero pins the
// implicit-zero contract: a publish with no `metadata.cascade_depth`
// key persists the message and the stored cascade_depth (if any) is
// zero. This is the chain-origin case — every user-initiated publish
// lands here.
func TestChannels_PublishMessage_CascadeDepth_DefaultZero(t *testing.T) {
	srv, store := channelTestServer(t)
	createBody, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}, {ID: "bob"}},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody).Code)

	pubBody, _ := json.Marshal(publishMessageRequest{
		SenderID: "alice", Content: "hi",
	})
	rec := doRequest(srv.Handler(), http.MethodPost,
		"/api/v1/channels/group:planning/messages", pubBody)
	require.Equal(t, http.StatusCreated, rec.Code, "body=%s", rec.Body.String())

	hist, err := store.GetHistory(context.Background(), "group:planning", 10, time.Time{})
	require.NoError(t, err)
	require.Len(t, hist, 1)
	// Either the key is absent (metadata stays nil) or it round-trips as 0.
	if hist[0].Metadata != nil {
		v, present := hist[0].Metadata["cascade_depth"]
		if present {
			assert.EqualValuesf(t, 0, v, "absent in body MUST persist as 0 or absent (got %v)", v)
		}
	}
}

// TestChannels_PublishMessage_CascadeDepth_NegativeRejected pins the
// REST-boundary loud-fail: a publish with a negative depth is a
// publisher bug (the canonical operational range is [0, cap]), so the
// orchestrator rejects with 400 rather than silently clamping to 0.
// The router-side clamp ([0, cap]) is defense-in-depth for programmatic
// callers that bypass the REST handler.
func TestChannels_PublishMessage_CascadeDepth_NegativeRejected(t *testing.T) {
	srv, _ := channelTestServer(t)
	createBody, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody).Code)

	pubBody, _ := json.Marshal(publishMessageRequest{
		SenderID: "alice", Content: "hi",
		Metadata: map[string]any{"cascade_depth": -1},
	})
	rec := doRequest(srv.Handler(), http.MethodPost,
		"/api/v1/channels/group:planning/messages", pubBody)
	require.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())
	assert.Contains(t, rec.Body.String(), "cascade_depth")
}

// TestChannels_PublishMessage_CascadeDepth_OverCapAccepted pins the
// clamp-don't-reject contract for over-cap values: a publisher cannot
// know the deployment's current cap, so an over-cap depth (99) is
// silently clamped at the router boundary and the publish returns
// 201. The cascade itself is suppressed — the publish succeeds, the
// fanout does not.
func TestChannels_PublishMessage_CascadeDepth_OverCapAccepted(t *testing.T) {
	srv, store := channelTestServer(t)
	createBody, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}, {ID: "bob"}},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody).Code)

	pubBody, _ := json.Marshal(publishMessageRequest{
		SenderID: "alice", Content: "hi",
		Metadata: map[string]any{"cascade_depth": 99},
	})
	rec := doRequest(srv.Handler(), http.MethodPost,
		"/api/v1/channels/group:planning/messages", pubBody)
	require.Equal(t, http.StatusCreated, rec.Code, "body=%s", rec.Body.String())

	// The stored message reflects the clamped value so a history
	// reader sees what the orchestrator actually enforced, not the
	// publisher's claim. The exact cap is owned by the channels
	// package; the test asserts only that the stored value is
	// strictly less than the input (clamped down).
	hist, err := store.GetHistory(context.Background(), "group:planning", 10, time.Time{})
	require.NoError(t, err)
	require.Len(t, hist, 1)
	require.NotNil(t, hist[0].Metadata)
	persisted, ok := hist[0].Metadata["cascade_depth"]
	require.True(t, ok, "clamped cascade_depth MUST be persisted")
	// SQLite store round-trips numeric metadata as float64.
	asFloat, isFloat := persisted.(float64)
	require.Truef(t, isFloat, "persisted cascade_depth shape: got %T", persisted)
	assert.LessOrEqualf(t, asFloat, float64(99), "sanity: clamped value cannot exceed input")
	assert.Lessf(t, asFloat, float64(99), "over-cap input MUST have been clamped down")
}

// TestChannels_PublishMessage_CascadeDepth_AtCap_Accepted pins that an
// at-cap publish succeeds at the REST boundary (201). The fanout drop
// fires in the router and is invisible to the publisher — the publish
// itself was always going to succeed.
func TestChannels_PublishMessage_CascadeDepth_AtCap_Accepted(t *testing.T) {
	srv, _ := channelTestServer(t)
	createBody, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}, {ID: "bob"}},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody).Code)

	pubBody, _ := json.Marshal(publishMessageRequest{
		SenderID: "alice", Content: "hi",
		Metadata: map[string]any{"cascade_depth": 5},
	})
	rec := doRequest(srv.Handler(), http.MethodPost,
		"/api/v1/channels/group:planning/messages", pubBody)
	require.Equal(t, http.StatusCreated, rec.Code, "body=%s", rec.Body.String())
}

// TestChannels_PublishMessage_CascadeDepth_NonInteger_Rejected pins
// that a non-integer numeric depth (e.g. 5.5) is rejected at the REST
// boundary with 400. JSON decoders cannot distinguish between
// integer-typed numerics and `float64`, so the orchestrator MUST
// enforce the whole-number invariant downstream of decode rather than
// at the codec layer.
func TestChannels_PublishMessage_CascadeDepth_NonInteger_Rejected(t *testing.T) {
	srv, _ := channelTestServer(t)
	createBody, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody).Code)

	pubBody, _ := json.Marshal(publishMessageRequest{
		SenderID: "alice", Content: "hi",
		Metadata: map[string]any{"cascade_depth": 5.5},
	})
	rec := doRequest(srv.Handler(), http.MethodPost,
		"/api/v1/channels/group:planning/messages", pubBody)
	require.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())
	assert.Contains(t, rec.Body.String(), "cascade_depth")
}
