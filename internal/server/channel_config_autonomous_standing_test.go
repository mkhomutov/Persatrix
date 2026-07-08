// RFC 0052 Phase 3a (PR 7) — the REST surface for the STANDING/scheduled
// sub-knobs on the RFC 0050 GET/PATCH config endpoint:
// `autonomous.{schedule_interval_seconds,max_convenings,standing_budget_tokens}`.
// These pin the nested merge of the new sub-knobs, their provenance, and the
// mandatory aggregate-bound 400 — the operator-facing half of the standing
// config backend. Ships dark (the timer wiring + convening counter are PR 7b);
// the one live effect is the aggregate-bound gate.
package server

import (
	"encoding/json"
	"net/http"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// standingArmBody is armBody plus a schedule interval — the shared standing edit.
// `extra` overlays the autonomous sub-knobs (e.g. an aggregate bound).
func standingArmBody(interval int, extra map[string]any) []byte {
	auto := map[string]any{"enabled": true, "convener": "nova", "schedule_interval_seconds": interval}
	for k, v := range extra {
		auto[k] = v
	}
	body, _ := json.Marshal(map[string]any{
		"interaction_budget_tokens": 200000,
		"escalation_chair_id":       "ada",
		"autonomous":                auto,
	})
	return body
}

// TestChannelConfig_AutonomousStandingRoundTrips: the happy path — a PATCH arming
// a standing channel with a schedule + an aggregate bound round-trips through the
// REST layer, stamps the router, and reports the new sub-knobs channel-sourced.
func TestChannelConfig_AutonomousStandingRoundTrips(t *testing.T) {
	srv, id := autonomousTestServer(t)

	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		standingArmBody(3600, map[string]any{"max_convenings": 10, "standing_budget_tokens": 5000000}),
		map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	a := decodeAutonomous(t, rec.Body.Bytes())
	assert.EqualValues(t, 3600, a["schedule_interval_seconds"].Value)
	assert.Equal(t, "channel", a["schedule_interval_seconds"].Source)
	assert.EqualValues(t, 10, a["max_convenings"].Value)
	assert.EqualValues(t, 5000000, a["standing_budget_tokens"].Value)

	// The router took the schedule live.
	assert.Equal(t, 3600, srv.channelRouter.AutonomousFor(id).ScheduleIntervalSeconds)
	assert.Equal(t, 10, srv.channelRouter.AutonomousFor(id).MaxConvenings)

	// GET round-trips the same state.
	rec = doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/"+id+"/config", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	a = decodeAutonomous(t, rec.Body.Bytes())
	assert.EqualValues(t, 3600, a["schedule_interval_seconds"].Value)
	assert.Equal(t, "channel", a["schedule_interval_seconds"].Source)
}

// TestChannelConfig_AutonomousStandingBoundRequired: arming a STANDING channel (a
// schedule interval) with no aggregate bound is a 400 — the §E safety gate. A
// standing channel is un-creatable without a `max_convenings`/`standing_budget_tokens`.
func TestChannelConfig_AutonomousStandingBoundRequired(t *testing.T) {
	srv, id := autonomousTestServer(t)
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		standingArmBody(3600, nil), map[string]string{"If-Match": "0"})
	assert.Equal(t, http.StatusBadRequest, rec.Code,
		"a standing channel without an aggregate bound must be rejected; body=%s", rec.Body.String())

	// The rejected edit never wrote — the channel is still at revision 0 / disabled.
	rec = doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/"+id+"/config", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	a := decodeAutonomous(t, rec.Body.Bytes())
	assert.Equal(t, false, a["enabled"].Value, "the rejected standing arm never wrote")
}

// TestChannelConfig_AutonomousStandingBoundViaBudgetAccepted: the aggregate cost
// budget alone (no count) satisfies the gate — either bound is sufficient.
func TestChannelConfig_AutonomousStandingBoundViaBudgetAccepted(t *testing.T) {
	srv, id := autonomousTestServer(t)
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		standingArmBody(3600, map[string]any{"standing_budget_tokens": 5000000}),
		map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code,
		"a standing cost budget alone satisfies the aggregate bound; body=%s", rec.Body.String())
	assert.True(t, srv.channelRouter.AutonomousFor(id).Enabled)
}

// TestChannelConfig_AutonomousStandingDefaultsUnset: a never-edited channel
// reports the schedule/aggregate-bound sub-knobs as unset (0), default-sourced —
// never a spurious standing channel.
func TestChannelConfig_AutonomousStandingDefaultsUnset(t *testing.T) {
	srv, id := autonomousTestServer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/"+id+"/config", nil)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	a := decodeAutonomous(t, rec.Body.Bytes())
	assert.EqualValues(t, 0, a["schedule_interval_seconds"].Value)
	assert.Equal(t, "default", a["schedule_interval_seconds"].Source)
	assert.EqualValues(t, 0, a["max_convenings"].Value)
	assert.EqualValues(t, 0, a["standing_budget_tokens"].Value)
}
