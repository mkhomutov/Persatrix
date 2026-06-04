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

// TestChannels_CreateChannel_RouterNil_Succeeds locks the nil-router guard in
// handleCreateChannel's floor-control hook. When the channel store is wired
// without a router (the WithChannels(store, nil) degraded/test config), creating
// a group channel must still succeed: the floor-control call is correctly
// skipped because, without a router, publishes to the channel take the
// store-only fallback and never fan out — so there is no concurrent round to
// serialize and floor control is *moot*, not silently dropped (contrast the
// loud channelFallbackWarnOnce signpost on the publish path, which guards a
// fallback that DOES skip load-bearing validation + metrics). This pins the
// guard so a future refactor that removes the nil check (a panic via
// SetFloorControl on a nil router) fails loudly here.
func TestChannels_CreateChannel_RouterNil_Succeeds(t *testing.T) {
	srv, _ := channelTestServerNoRouter(t)
	body, _ := json.Marshal(createChannelRequest{
		Name: "adhoc",
		Members: []channelMemberRequest{
			{ID: "alice", Respond: "always"},
			{ID: "bob", Respond: "always"},
		},
	})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", body)
	require.Equal(t, http.StatusCreated, rec.Code,
		"create must succeed without a router wired: %s", rec.Body.String())

	// The channel is persisted...
	ch, err := srv.channelStore.GetChannel(t.Context(), "group:adhoc")
	require.NoError(t, err)
	assert.Equal(t, channels.ChannelTypeGroup, ch.Type)

	// ...and there is no router to hold floor state — floor control is
	// irrelevant on the store-only publish path, not skipped behaviour.
	assert.Nil(t, srv.channelRouter,
		"this config wires no router; the floor-control hook is a correct no-op")
}
