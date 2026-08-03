package server

import (
	"encoding/json"
	"net/http"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// channel_config_cascade_test.go — the ISSUE-0114 (v0.3.13) per-channel
// `max_cascade_depth` knob on the RFC 0050 REST surface. Split from
// channel_config_handlers_test.go (near the 500-line review cap); the
// harness (channelConfigTestServer / decodeConfig) lives there.

// TestChannelConfigCascadeDepth_PatchSetThenGet: the knob's happy path — a
// PATCH resolves the override on the live router (the publish path reads the
// same map), the response reports channel provenance, and null unsets back to
// fleet inheritance.
func TestChannelConfigCascadeDepth_PatchSetThenGet(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)

	body, _ := json.Marshal(map[string]any{"max_cascade_depth": 3})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	_, fields := decodeConfig(t, rec.Body.Bytes())
	assert.EqualValues(t, 3, fields["max_cascade_depth"].Value)
	assert.Equal(t, "channel", fields["max_cascade_depth"].Source)

	depth, set := srv.channelRouter.MaxCascadeDepthFor(id)
	assert.Equal(t, 3, depth, "PATCH must stamp the router, not just the store")
	assert.True(t, set)

	// null unsets → back to the fleet cap, sourced from the default.
	body, _ = json.Marshal(map[string]any{"max_cascade_depth": nil})
	rec = doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "1"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
	_, fields = decodeConfig(t, rec.Body.Bytes())
	assert.Equal(t, "default", fields["max_cascade_depth"].Source, "null must clear the override")
	_, set = srv.channelRouter.MaxCascadeDepthFor(id)
	assert.False(t, set, "the router entry must be deleted — fleet inheritance restored")
}

// TestChannelConfigCascadeDepth_RejectsNonPositive: an explicit 0 (or a
// negative) 400s — the setter treats non-positive as the inherit sentinel, so
// persisting it would store a knob that lies about being an override (the
// salience-cap posture). Nothing is written on rejection.
func TestChannelConfigCascadeDepth_RejectsNonPositive(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)

	for _, v := range []int{0, -2} {
		body, _ := json.Marshal(map[string]any{"max_cascade_depth": v})
		rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
			body, map[string]string{"If-Match": "0"})
		assert.Equalf(t, http.StatusBadRequest, rec.Code,
			"max_cascade_depth=%d must be rejected, body=%s", v, rec.Body.String())
	}

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/"+id+"/config", nil)
	revision, _ := decodeConfig(t, rec.Body.Bytes())
	assert.Equal(t, int64(0), revision, "a rejected apply must not bump the revision")
}

// TestChannelConfigCascadeDepth_AboveFleetAppliesWithWarn pins the option (c)
// live-edit posture end to end: an above-fleet PATCH is ACCEPTED (200, the
// override applies) rather than rejected — the fleet cap is startup-only, so
// a reject would force a restart into a live edit loop; the loud warning
// rides the router setter (pinned in the channels package). Config-as-code is
// the strict side: the YAML loader rejects the same value.
func TestChannelConfigCascadeDepth_AboveFleetAppliesWithWarn(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)
	fleet := srv.channelRouter.MaxCascadeDepth()

	body, _ := json.Marshal(map[string]any{"max_cascade_depth": fleet + 2})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code,
		"an above-fleet live edit is warned, never rejected; body=%s", rec.Body.String())

	depth, set := srv.channelRouter.MaxCascadeDepthFor(id)
	assert.Equal(t, fleet+2, depth)
	assert.True(t, set)
}

// TestChannelConfigCascadeDepth_FirstEditPreservesYAMLSeededCap is the
// ISSUE-0103 shape for the new knob: a YAML-declared per-channel cap lives
// only on the router at revision 0; the FIRST edit of an unrelated knob must
// freeze it into the baseline (conditional capture keys on the router's
// explicit-set flag), not silently reset the channel to the fleet cap.
func TestChannelConfigCascadeDepth_FirstEditPreservesYAMLSeededCap(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)

	// Seed the way the boot path does (ResolveChannelCascadeCaps → setter):
	// router-held only, absent from the store.
	srv.channelRouter.SetChannelMaxCascadeDepth(id, 3)

	body, _ := json.Marshal(map[string]any{"interaction_idle_timeout_seconds": 60})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	_, fields := decodeConfig(t, rec.Body.Bytes())
	assert.EqualValues(t, 3, fields["max_cascade_depth"].Value,
		"the un-edited YAML-seeded cap must survive the first edit")
	assert.Equal(t, "channel", fields["max_cascade_depth"].Source,
		"the surviving cap is frozen as an explicit override")
	depth, set := srv.channelRouter.MaxCascadeDepthFor(id)
	assert.Equal(t, 3, depth, "the router must still hold the cap after the apply")
	assert.True(t, set)
}
