package server

import (
	"encoding/json"
	"net/http"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// seedPlanningWithAlice creates group:planning with alice and returns the server
// + store, the shared setup for the member-config PATCH tests.
func seedPlanningWithAlice(t *testing.T) *Server {
	t.Helper()
	srv, _ := channelTestServer(t)
	body, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", body).Code)
	return srv
}

// TestChannels_UpdateMember_NoContent pins the RFC 0050 member-config PATCH happy
// path: a disposition + threshold edit returns 204 and is reflected in the
// channel's member list.
func TestChannels_UpdateMember_NoContent(t *testing.T) {
	srv := seedPlanningWithAlice(t)

	th := 0.6
	body, _ := json.Marshal(updateMemberRequest{Respond: "participant", Threshold: &th})
	rec := doRequest(srv.Handler(), http.MethodPatch, "/api/v1/channels/group:planning/members/alice", body)
	require.Equal(t, http.StatusNoContent, rec.Code, "body=%s", rec.Body.String())

	rec = doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/group:planning", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	var resp channelResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.Len(t, resp.Members, 1)
	assert.True(t, resp.Members[0].SalienceGated)
	require.NotNil(t, resp.Members[0].Threshold)
	assert.InDelta(t, 0.6, *resp.Members[0].Threshold, 1e-9)
}

// TestChannels_UpdateMember_NotFound: PATCHing a participant who is not a member
// is a 404 (distinct from the 403 a non-member publish returns).
func TestChannels_UpdateMember_NotFound(t *testing.T) {
	srv := seedPlanningWithAlice(t)

	body, _ := json.Marshal(updateMemberRequest{Respond: "participant"})
	rec := doRequest(srv.Handler(), http.MethodPatch, "/api/v1/channels/group:planning/members/ghost", body)
	assert.Equal(t, http.StatusNotFound, rec.Code, "body=%s", rec.Body.String())
}

// TestChannels_UpdateMember_BadThreshold: an out-of-range or wrong-disposition
// threshold is a 400, enforcing the same rule as config load over REST.
func TestChannels_UpdateMember_BadThreshold(t *testing.T) {
	srv := seedPlanningWithAlice(t)

	over := 1.5
	body, _ := json.Marshal(updateMemberRequest{Respond: "participant", Threshold: &over})
	rec := doRequest(srv.Handler(), http.MethodPatch, "/api/v1/channels/group:planning/members/alice", body)
	assert.Equal(t, http.StatusBadRequest, rec.Code, "out-of-range threshold is a 400; body=%s", rec.Body.String())

	th := 0.3
	body, _ = json.Marshal(updateMemberRequest{Respond: "observer", Threshold: &th})
	rec = doRequest(srv.Handler(), http.MethodPatch, "/api/v1/channels/group:planning/members/alice", body)
	assert.Equal(t, http.StatusBadRequest, rec.Code, "threshold on a non-open-floor disposition is a 400; body=%s", rec.Body.String())
}

// TestChannels_UpdateMember_RespondRequired pins that the PATCH is a full replace
// of the member's editable config and MUST name a disposition: an absent
// `respond` is a 400, not a silent default to `when_mentioned`. This is not a
// style choice — `salience_gated` is derived from the *declared* disposition
// (participant/chair/bare-always), which collapses to the legacy `always` wire
// value and is unrecoverable from persisted state, so an edit that omits
// `respond` cannot re-derive the bid correctly. The regression it guards: a
// body carrying only a new threshold must NOT quietly demote a salience-gated
// participant to `when_mentioned` (bias-to-silence with the bid off).
func TestChannels_UpdateMember_RespondRequired(t *testing.T) {
	srv := seedPlanningWithAlice(t)

	// Establish a known good state: alice is an open-floor participant with a bar.
	th := 0.6
	body, _ := json.Marshal(updateMemberRequest{Respond: "participant", Threshold: &th})
	require.Equal(t, http.StatusNoContent,
		doRequest(srv.Handler(), http.MethodPatch, "/api/v1/channels/group:planning/members/alice", body).Code)

	// A body that omits `respond` (here, "just change the threshold") is rejected.
	rec := doRequest(srv.Handler(), http.MethodPatch, "/api/v1/channels/group:planning/members/alice", []byte(`{"threshold":0.9}`))
	require.Equal(t, http.StatusBadRequest, rec.Code, "respond is required; body=%s", rec.Body.String())
	assert.Contains(t, rec.Body.String(), "respond", "the 400 names the missing field")

	// An empty body is likewise a 400, not a 204 that wipes the disposition.
	rec = doRequest(srv.Handler(), http.MethodPatch, "/api/v1/channels/group:planning/members/alice", []byte(`{}`))
	assert.Equal(t, http.StatusBadRequest, rec.Code, "empty body is rejected, not a silent downgrade; body=%s", rec.Body.String())

	// The rejected edits left alice's prior config untouched (no silent downgrade).
	rec = doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/group:planning", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	var resp channelResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.Len(t, resp.Members, 1)
	assert.True(t, resp.Members[0].SalienceGated, "still an open-floor participant")
	require.NotNil(t, resp.Members[0].Threshold)
	assert.InDelta(t, 0.6, *resp.Members[0].Threshold, 1e-9, "threshold unchanged by the rejected edits")
}
