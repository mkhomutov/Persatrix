package server

import (
	"encoding/json"
	"net/http"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// TestChannels_GetChannel_SurfacesSalienceSignal pins the v0.3.8 contract that
// GET /api/v1/channels/{id} carries the per-member salience signal, not just the
// normalized legacy `respond` token. The store collapses the disposition
// vocabulary to the legacy triple before persisting (chair/participant → always,
// observer → never), so `respond` alone cannot tell a salience-gated participant
// from a legacy always-replier — the `salience_gated`/`threshold` fields are the
// only thing that survives the store boundary (see [channels.Member.SalienceGated]).
// Without them an operator cannot read back the disposition they just set.
//
// Lives in its own file (not channel_handlers_test.go) to keep that file under
// the 500-line review cap.
func TestChannels_GetChannel_SurfacesSalienceSignal(t *testing.T) {
	srv, _ := channelTestServer(t)
	createBody, _ := json.Marshal(createChannelRequest{
		Name: "planning",
		Members: []channelMemberRequest{
			{ID: "alice", Respond: "chair"},
			{ID: "bob", Respond: "participant"},
			{ID: "carol", Respond: "observer"},
			{ID: "dave"}, // bare → when_mentioned
		},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody).Code)

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/group:planning", nil)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	// Decode into a raw shape so the assertions key off the wire JSON, not the
	// Go struct (red before the memberResponse fields exist).
	var resp struct {
		Members []struct {
			ID            string   `json:"id"`
			Respond       string   `json:"respond"`
			SalienceGated bool     `json:"salience_gated"`
			Threshold     *float64 `json:"threshold"`
		} `json:"members"`
	}
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	byID := map[string]struct {
		respond string
		gated   bool
		thr     *float64
	}{}
	for _, m := range resp.Members {
		byID[m.ID] = struct {
			respond string
			gated   bool
			thr     *float64
		}{m.Respond, m.SalienceGated, m.Threshold}
	}

	// chair: normalized to `always` on the wire, but gated with the low default
	// threshold — the only signal that distinguishes it from a legacy `always`.
	assert.Equal(t, "always", byID["alice"].respond)
	assert.True(t, byID["alice"].gated, "chair must be salience-gated")
	require.NotNil(t, byID["alice"].thr, "chair carries the default threshold")
	assert.InDelta(t, channels.DefaultChairThreshold, *byID["alice"].thr, 1e-9)

	// participant: gated, no explicit threshold (bias-to-silence → nil).
	assert.Equal(t, "always", byID["bob"].respond)
	assert.True(t, byID["bob"].gated, "participant must be salience-gated")
	assert.Nil(t, byID["bob"].thr, "bare participant has no threshold")

	// observer / when_mentioned: not gated.
	assert.Equal(t, "never", byID["carol"].respond)
	assert.False(t, byID["carol"].gated)
	assert.Equal(t, "when_mentioned", byID["dave"].respond)
	assert.False(t, byID["dave"].gated)
}
