package server

import (
	"encoding/json"
	"net/http"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// TestChannels_CreateChannel_EnablesFloorControl pins the runtime-create half
// of RFC 0030 Layer 2.5 (the floor-control follow-up to the config-startup
// wiring). A group channel created at runtime via POST /api/v1/channels — the
// path the RFC 0048 web console "New channel" form drives — must resolve floor
// control ON by default, exactly like a channel declared in config/channels.yaml.
// Otherwise a tester who creates a channel from the console and watches two
// personas reply sees the concurrent, mutually-blind "shout" floor control was
// added to fix. The default per-turn timeout is the canonical 45s (D2).
func TestChannels_CreateChannel_EnablesFloorControl(t *testing.T) {
	srv, _ := channelTestServer(t)
	body, _ := json.Marshal(createChannelRequest{
		Name: "adhoc",
		Members: []channelMemberRequest{
			{ID: "alice", Respond: "always"},
			{ID: "bob", Respond: "always"},
		},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", body).Code)

	enabled, turnTimeout, set := srv.channelRouter.FloorControlFor("group:adhoc")
	require.True(t, set,
		"a runtime-created group channel must have floor control resolved (the console-create gap)")
	assert.True(t, enabled,
		"floor control is ON by default for a runtime-created group channel")
	assert.Equal(t, time.Duration(channels.DefaultFloorTurnTimeoutSeconds)*time.Second, turnTimeout,
		"the per-turn timeout defaults to the canonical 45s (amendment D2)")
}
