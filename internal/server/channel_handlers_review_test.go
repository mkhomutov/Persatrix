package server

import (
	"encoding/json"
	"net/http"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// PR #245 review fix-ups — handler-layer hardening.
//
// These tests pin three review findings against the channel REST surface:
//
//  1. respond-policy boundary validation (Should-Fix #4): an unknown
//     `respond:` value in CreateChannel / AddMember should be rejected
//     at the handler with a clean 400, not delegated to the store CHECK
//     constraint (which surfaces as 500 INTERNAL).
//  2. parseLimit strictness (Low): a malformed `?limit=` query parameter
//     should surface as 400, not silently fall back to the default —
//     malformed values are bugs we want clients to fix, not paper over.
//  3. atomic create-then-add-members (High): a partial-failure mid-loop
//     in handleCreateChannel must NOT leave an orphan channel that
//     poisons the client's retry with 409 CONFLICT. The new
//     `CreateChannelWithMembers` store path makes the create+add atomic;
//     this test pins the externally observable behaviour.

// TestChannels_CreateChannel_RejectsUnknownRespondPolicy pins finding
// "validate respond at REST boundary": invalid `respond` strings should
// return 400 with a typed error, not 500 from the store CHECK constraint.
func TestChannels_CreateChannel_RejectsUnknownRespondPolicy(t *testing.T) {
	srv, _ := channelTestServer(t)
	body, _ := json.Marshal(createChannelRequest{
		Name: "planning",
		Members: []channelMemberRequest{
			{ID: "alice", Respond: "always"},
			{ID: "bob", Respond: "occasionally"}, // invalid
		},
	})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", body)
	assert.Equal(t, http.StatusBadRequest, rec.Code,
		"unknown respond policy must surface as 400 from the boundary, not 500 from the store")
}

// TestChannels_AddMember_RejectsUnknownRespondPolicy pins the same
// contract on the add-member endpoint.
func TestChannels_AddMember_RejectsUnknownRespondPolicy(t *testing.T) {
	srv, _ := channelTestServer(t)
	createBody, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody).Code)

	addBody, _ := json.Marshal(addMemberRequest{ID: "carol", Respond: "sometimes"})
	rec := doRequest(srv.Handler(), http.MethodPost,
		"/api/v1/channels/group:planning/members", addBody)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

// TestChannels_ListChannels_MalformedLimit_400 pins finding "tighten
// parseLimit": a non-empty malformed `?limit=` value should reject with
// 400, not silently fall back to the default.
func TestChannels_ListChannels_MalformedLimit_400(t *testing.T) {
	srv, _ := channelTestServer(t)

	cases := []struct {
		name string
		raw  string
	}{
		{"non-numeric", "?limit=abc"},
		{"negative", "?limit=-5"},
		{"zero", "?limit=0"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels"+c.raw, nil)
			assert.Equal(t, http.StatusBadRequest, rec.Code,
				"malformed limit %q must surface as 400", c.raw)
		})
	}

	// Sanity: an absent limit still works (falls back to the default).
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels", nil)
	assert.Equal(t, http.StatusOK, rec.Code)
}

// TestChannels_CreateChannel_AtomicOnInvalidMember pins finding
// "non-atomic create-then-add-members": an invalid member id mid-list
// must NOT leave an orphan channel that poisons the client's retry.
//
// Pre-fix behaviour: handleCreateChannel called CreateChannel, then
// looped per-member calling AddMember; an invalid id in the second slot
// returned 400/500 with the channel already created. A client retry
// then hit ErrChannelExists → 409, and the second member was never
// added. Post-fix: the handler routes through the atomic
// CreateChannelWithMembers store helper, which rolls back the channel
// row when any membership insert fails, so the retry succeeds cleanly.
func TestChannels_CreateChannel_AtomicOnInvalidMember(t *testing.T) {
	srv, _ := channelTestServer(t)
	body, _ := json.Marshal(createChannelRequest{
		Name: "planning",
		Members: []channelMemberRequest{
			{ID: "alice"},
			{ID: ""}, // invalid → triggers rollback in the store
		},
	})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", body)
	require.NotEqual(t, http.StatusCreated, rec.Code,
		"invalid member must reject the entire create")

	// Channel must NOT exist. If the rollback worked, GET returns 404.
	getRec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/group:planning", nil)
	assert.Equal(t, http.StatusNotFound, getRec.Code,
		"failed create must not leak an orphan channel row")

	// Retry with a clean member list succeeds — no ErrChannelExists from
	// a leaked row.
	cleanBody, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}},
	})
	retryRec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", cleanBody)
	assert.Equal(t, http.StatusCreated, retryRec.Code,
		"retry after rolled-back create must succeed (no 409)")
}
