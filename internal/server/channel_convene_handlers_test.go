package server

// channel_convene_handlers_test.go — RFC 0052 §B PR 3: the POST …/convene
// endpoint. Pins the operator-action contract: gated behind the
// config_edit_enabled toggle, 202 + convener ack on an armed channel, 409 on an
// unarmed one, 403 when the toggle is off.

import (
	"encoding/json"
	"net/http"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// armConvene marks the seeded channel autonomous with `convener` as the opener.
func armConvene(t *testing.T, srv *Server, id, convener string) {
	t.Helper()
	srv.channelRouter.SetAutonomous(id, channels.AutonomousConfig{
		Enabled:  true,
		Convener: convener,
		Topic:    "Should we adopt a monorepo?",
		Goal:     "A synthesized recommendation.",
	})
}

func TestConveneHandler_ArmedChannel_Accepted(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)
	armConvene(t, srv, id, "alice")

	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/"+id+"/convene", nil)
	require.Equal(t, http.StatusAccepted, rec.Code, "body=%s", rec.Body.String())

	var resp struct {
		ChannelID string `json:"channel_id"`
		Convener  string `json:"convener"`
		Status    string `json:"status"`
	}
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, id, resp.ChannelID)
	assert.Equal(t, "alice", resp.Convener)
	assert.Equal(t, "convening", resp.Status)
}

func TestConveneHandler_UnarmedChannel_Conflict(t *testing.T) {
	srv, id := channelConfigTestServer(t, true) // never armed

	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/"+id+"/convene", nil)
	assert.Equal(t, http.StatusConflict, rec.Code, "body=%s", rec.Body.String())
}

func TestConveneHandler_ToggleOff_Forbidden(t *testing.T) {
	srv, id := channelConfigTestServer(t, false) // config_edit_enabled OFF
	armConvene(t, srv, id, "alice")

	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/"+id+"/convene", nil)
	assert.Equal(t, http.StatusForbidden, rec.Code, "body=%s", rec.Body.String())
}

func TestConveneHandler_DriftedConvener_BadRequest(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)
	armConvene(t, srv, id, "ghost") // not a member of the alice/bob roster

	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/"+id+"/convene", nil)
	assert.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())
}

// TestConveneHandler_MissingChannel_NotFound — convening a channel id that does
// not exist reports 404, consistent with GET/PATCH …/config (the deep-review
// fix: it previously fell through AutonomousFor's disabled default and 409'd as
// "not armed", masking a fat-fingered/deleted id).
func TestConveneHandler_MissingChannel_NotFound(t *testing.T) {
	srv, _ := channelConfigTestServer(t, true) // toggle on; convene a different id

	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/group%3Aabsent/convene", nil)
	assert.Equal(t, http.StatusNotFound, rec.Code, "body=%s", rec.Body.String())
}
