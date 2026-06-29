// RFC 0052 Phase 1a (PR 1) — the REST surface for the nested `autonomous` knob on
// the RFC 0050 GET/PATCH config endpoint. These pin the nested merge (sub-key
// tri-state), the per-sub-knob provenance, the mandatory-cost-cap 400, the OQ #1
// convener 400s (non-member / chair-collision), the unknown-sub-knob 400, the
// null-clears block tri-state, and the ISSUE-0103 first-edit freeze — the
// operator-facing half of the dark config backend.
package server

import (
	"encoding/json"
	"net/http"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// autonomousTestServer wires the config test server with a two-member roster
// (nova + ada) so an autonomous channel has a valid convener (nova) distinct from
// any chair.
func autonomousTestServer(t *testing.T) (*Server, string) {
	t.Helper()
	return channelConfigTestServerWithMembers(t, true,
		[]channels.Member{{ParticipantID: "nova"}, {ParticipantID: "ada"}})
}

// decodeAutonomous pulls the nested `autonomous` block out of a config payload as a
// sub-knob → {value, source} map.
func decodeAutonomous(t *testing.T, raw []byte) map[string]struct {
	Value  any    `json:"value"`
	Source string `json:"source"`
} {
	t.Helper()
	var env struct {
		Autonomous map[string]struct {
			Value  any    `json:"value"`
			Source string `json:"source"`
		} `json:"autonomous"`
	}
	require.NoError(t, json.Unmarshal(raw, &env))
	return env.Autonomous
}

// armBody is a PATCH body that arms a channel: a positive cap + an autonomous
// block naming `nova` as convener. The shared happy-path edit.
func armBody(extra map[string]any) []byte {
	auto := map[string]any{"enabled": true, "convener": "nova"}
	for k, v := range extra {
		auto[k] = v
	}
	body, _ := json.Marshal(map[string]any{
		"interaction_budget_tokens": 200000,
		"autonomous":                auto,
	})
	return body
}

// TestChannelConfig_AutonomousDefaults: a never-edited channel reports the disabled
// default rung, every sub-knob sourced from the default.
func TestChannelConfig_AutonomousDefaults(t *testing.T) {
	srv, id := autonomousTestServer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/"+id+"/config", nil)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	a := decodeAutonomous(t, rec.Body.Bytes())
	assert.Equal(t, false, a["enabled"].Value, "autonomy is off by default")
	assert.Equal(t, "default", a["enabled"].Source)
	assert.EqualValues(t, channels.DefaultAutonomousMaxRounds, a["max_rounds"].Value)
}

// TestChannelConfig_AutonomousArmRoundTrips: the happy path — PATCH arms the
// channel with a cap, bumps the revision, stamps the router, and reports the armed
// sub-knobs channel-sourced while untouched ones stay default-sourced.
func TestChannelConfig_AutonomousArmRoundTrips(t *testing.T) {
	srv, id := autonomousTestServer(t)

	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		armBody(map[string]any{"topic": "monorepo?"}), map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	revision, _ := decodeConfig(t, rec.Body.Bytes())
	assert.Equal(t, int64(1), revision)

	a := decodeAutonomous(t, rec.Body.Bytes())
	assert.Equal(t, true, a["enabled"].Value)
	assert.Equal(t, "channel", a["enabled"].Source)
	assert.Equal(t, "nova", a["convener"].Value)
	assert.Equal(t, "monorepo?", a["topic"].Value)
	assert.Equal(t, "channel", a["topic"].Source)

	// The router took the edit live.
	assert.True(t, srv.channelRouter.AutonomousFor(id).Enabled)
	assert.Equal(t, "nova", srv.channelRouter.AutonomousFor(id).Convener)

	// GET round-trips the same state.
	rec = doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/"+id+"/config", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	a = decodeAutonomous(t, rec.Body.Bytes())
	assert.Equal(t, true, a["enabled"].Value)
	assert.Equal(t, "channel", a["enabled"].Source)
}

// TestChannelConfig_AutonomousCapRequired: arming without a positive cap is a 400 —
// the mandatory-cost-cap safety gate. Uncapped autonomy is un-creatable.
func TestChannelConfig_AutonomousCapRequired(t *testing.T) {
	srv, id := autonomousTestServer(t)
	body, _ := json.Marshal(map[string]any{"autonomous": map[string]any{"enabled": true, "convener": "nova"}})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	assert.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())
}

// TestChannelConfig_AutonomousConvenerMustBeMember: a non-member convener is a 400.
func TestChannelConfig_AutonomousConvenerMustBeMember(t *testing.T) {
	srv, id := autonomousTestServer(t)
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		armBody(map[string]any{"convener": "ghost"}), map[string]string{"If-Match": "0"})
	assert.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())
}

// TestChannelConfig_AutonomousConvenerDistinctFromChair: a convener that collides
// with the escalation chair is a 400 (OQ #1 — distinct roles).
func TestChannelConfig_AutonomousConvenerDistinctFromChair(t *testing.T) {
	srv, id := autonomousTestServer(t)
	body, _ := json.Marshal(map[string]any{
		"interaction_budget_tokens": 200000,
		"escalation_chair_id":       "nova",
		"autonomous":                map[string]any{"enabled": true, "convener": "nova"},
	})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	assert.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())
}

// TestChannelConfig_AutonomousNestedMergePreservesSubKnobs: a second PATCH setting a
// different sub-knob keeps the earlier ones — the nested merge layers, not replaces.
func TestChannelConfig_AutonomousNestedMergePreservesSubKnobs(t *testing.T) {
	srv, id := autonomousTestServer(t)

	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		armBody(nil), map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	body, _ := json.Marshal(map[string]any{"autonomous": map[string]any{"goal": "a recommendation"}})
	rec = doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "1"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	a := decodeAutonomous(t, rec.Body.Bytes())
	assert.Equal(t, true, a["enabled"].Value, "earlier enabled survives the merge")
	assert.Equal(t, "nova", a["convener"].Value, "earlier convener survives the merge")
	assert.Equal(t, "a recommendation", a["goal"].Value)
}

// TestChannelConfig_AutonomousAgendaRoundTrips: the array sub-knob round-trips.
func TestChannelConfig_AutonomousAgendaRoundTrips(t *testing.T) {
	srv, id := autonomousTestServer(t)
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		armBody(map[string]any{"agenda": []string{"cost", "coupling"}}), map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
	assert.Equal(t, []string{"cost", "coupling"}, srv.channelRouter.AutonomousFor(id).Agenda)
}

// TestChannelConfig_AutonomousPatchNullClears: `autonomous: null` clears the whole
// block back to inherit (the block-level tri-state). It also clears the cap so the
// channel is no longer armed.
func TestChannelConfig_AutonomousPatchNullClears(t *testing.T) {
	srv, id := autonomousTestServer(t)

	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		armBody(nil), map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	body, _ := json.Marshal(map[string]any{"autonomous": nil})
	rec = doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "1"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	a := decodeAutonomous(t, rec.Body.Bytes())
	assert.Equal(t, false, a["enabled"].Value, "cleared block inherits the disabled default")
	assert.Equal(t, "default", a["enabled"].Source)
}

// TestChannelConfig_AutonomousUnknownSubKnobRejected: an unrecognised sub-knob is a
// 400 (the closed-sub-knob-set gate).
func TestChannelConfig_AutonomousUnknownSubKnobRejected(t *testing.T) {
	srv, id := autonomousTestServer(t)
	body, _ := json.Marshal(map[string]any{"autonomous": map[string]any{"vibe": true}})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	assert.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())
}

// TestChannelConfig_AutonomousFirstEditFreezesArmed: a sparse first edit of an
// UNRELATED knob must not disarm a channel armed on the router (e.g. by a YAML
// block) — the ISSUE-0103 baseline freeze covers autonomous too.
func TestChannelConfig_AutonomousFirstEditFreezesArmed(t *testing.T) {
	srv, id := autonomousTestServer(t)
	// Pre-seed an armed rung on the router + a cap, standing in for a YAML-resolved
	// autonomous channel at revision 0.
	srv.channelRouter.SetAutonomous(id, channels.AutonomousConfig{Enabled: true, Convener: "nova"})
	srv.channelRouter.SetInteractionBudgetTokens(id, 200000)

	body, _ := json.Marshal(map[string]any{"floor_control": false})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	assert.True(t, srv.channelRouter.AutonomousFor(id).Enabled,
		"the first edit must freeze the armed rung, not disarm it")
	a := decodeAutonomous(t, rec.Body.Bytes())
	assert.Equal(t, true, a["enabled"].Value)
	assert.Equal(t, "channel", a["enabled"].Source, "the frozen armed rung is channel-sourced")
}

// TestChannelConfig_AutonomousFirstEditDropsDriftedConvener: the freeze's
// governance-drift branch (mirroring the escalation chair). An armed rung whose
// convener has drifted OUT of the channel's membership is already un-convenable at
// dispatch, so an unrelated first edit must DROP the inert block rather than freeze
// it — freezing would make the convener-membership cross-field rule REJECT (400) an
// edit naming a knob the operator never touched. The edit proceeds and the channel
// reads back inherit/disabled.
func TestChannelConfig_AutonomousFirstEditDropsDriftedConvener(t *testing.T) {
	srv, id := autonomousTestServer(t)
	// Arm the router with a convener that is NOT a member (drifted out), standing in
	// for a YAML-armed channel whose convener has since left the roster. A cap is
	// present so only the drift — not a missing cap — is under test.
	srv.channelRouter.SetAutonomous(id, channels.AutonomousConfig{Enabled: true, Convener: "ghost"})
	srv.channelRouter.SetInteractionBudgetTokens(id, 200000)

	body, _ := json.Marshal(map[string]any{"floor_control": false})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code,
		"a drifted convener must not block an unrelated first edit; body=%s", rec.Body.String())

	a := decodeAutonomous(t, rec.Body.Bytes())
	assert.Equal(t, false, a["enabled"].Value, "the inert armed block is dropped, not frozen")
	assert.Equal(t, "default", a["enabled"].Source)
	assert.False(t, srv.channelRouter.AutonomousFor(id).Enabled,
		"the dropped block leaves the channel disabled on the router")
}

// TestChannelConfig_AutonomousFirstEditOnDefaultStaysInherit: the conditional
// freeze's other branch — a channel at the default (disabled) rung stays inherit
// through an unrelated first edit.
func TestChannelConfig_AutonomousFirstEditOnDefaultStaysInherit(t *testing.T) {
	srv, id := autonomousTestServer(t)

	body, _ := json.Marshal(map[string]any{"floor_control": false})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	a := decodeAutonomous(t, rec.Body.Bytes())
	assert.Equal(t, false, a["enabled"].Value)
	assert.Equal(t, "default", a["enabled"].Source,
		"a default autonomous rung stays inherit through an unrelated first edit")
}
