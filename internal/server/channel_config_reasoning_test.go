// RFC 0051 Phase 3a (PR 4) — the REST surface for the nested `reasoning` knob on
// the RFC 0050 GET/PATCH config endpoint. These pin the nested merge (sub-key
// tri-state), the per-sub-knob provenance, the capability-gated 400s (deep /
// revise≥1), and the cross-field governance 400 (mode != off on an ungoverned
// channel) — the operator-facing half of the config backend.
package server

import (
	"encoding/json"
	"net/http"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// reasoningTestServer wires the config test server with one salience-gated
// open-floor member, so `reasoning.mode != off` clears the cross-field governance
// rule (the default alice/bob roster is ungoverned, used by the negative test).
func reasoningTestServer(t *testing.T, enabled bool) (*Server, string) {
	t.Helper()
	return channelConfigTestServerWithMembers(t, enabled,
		[]channels.Member{{ParticipantID: "ada", RespondPolicy: channels.RespondAlways, SalienceGated: true}})
}

// decodeReasoning pulls the nested `reasoning` block out of a config payload as a
// sub-knob → {value, source} map (the flat decodeConfig helper cannot — reasoning
// is an object of objects, not a single {value, source} cell).
func decodeReasoning(t *testing.T, raw []byte) map[string]struct {
	Value  any    `json:"value"`
	Source string `json:"source"`
} {
	t.Helper()
	var env struct {
		Reasoning map[string]struct {
			Value  any    `json:"value"`
			Source string `json:"source"`
		} `json:"reasoning"`
	}
	require.NoError(t, json.Unmarshal(raw, &env))
	return env.Reasoning
}

// TestChannelConfig_ReasoningDefaults: a never-edited channel reports the default
// rung (off / fast / shallow / 0), every sub-knob sourced from the default.
func TestChannelConfig_ReasoningDefaults(t *testing.T) {
	srv, id := reasoningTestServer(t, true)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/"+id+"/config", nil)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	r := decodeReasoning(t, rec.Body.Bytes())
	assert.Equal(t, "off", r["mode"].Value)
	assert.Equal(t, "default", r["mode"].Source)
	assert.Equal(t, "fast", r["model"].Value)
	assert.Equal(t, "shallow", r["depth"].Value)
	assert.EqualValues(t, 0, r["revise"].Value)
	for _, k := range []string{"mode", "model", "depth", "revise"} {
		assert.Equal(t, "default", r[k].Source, "sub-knob %s", k)
	}
}

// TestChannelConfig_ReasoningPatchModeBid: the happy path — PATCH reasoning.mode
// flips the rung, bumps the revision, stamps the router, and reports mode as
// channel-sourced while the untouched sub-knobs stay default-sourced (per-sub-knob
// provenance through the nested merge).
func TestChannelConfig_ReasoningPatchModeBid(t *testing.T) {
	srv, id := reasoningTestServer(t, true)

	body, _ := json.Marshal(map[string]any{"reasoning": map[string]any{"mode": "bid"}})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	revision, _ := decodeConfig(t, rec.Body.Bytes())
	assert.Equal(t, int64(1), revision)

	r := decodeReasoning(t, rec.Body.Bytes())
	assert.Equal(t, "bid", r["mode"].Value)
	assert.Equal(t, "channel", r["mode"].Source)
	assert.Equal(t, "fast", r["model"].Value)
	assert.Equal(t, "default", r["model"].Source, "an untouched sub-knob stays inherited")

	// The router took the edit live.
	assert.Equal(t, channels.ReasoningModeBid, srv.channelRouter.ReasoningFor(id).Mode)

	// GET round-trips the same state.
	rec = doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/"+id+"/config", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	r = decodeReasoning(t, rec.Body.Bytes())
	assert.Equal(t, "bid", r["mode"].Value)
	assert.Equal(t, "channel", r["mode"].Source)
}

// TestChannelConfig_ReasoningPatchPlanRoundTrips: the top rung also round-trips.
func TestChannelConfig_ReasoningPatchPlanRoundTrips(t *testing.T) {
	srv, id := reasoningTestServer(t, true)

	body, _ := json.Marshal(map[string]any{"reasoning": map[string]any{"mode": "plan"}})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
	assert.Equal(t, channels.ReasoningModePlan, srv.channelRouter.ReasoningFor(id).Mode)
}

// TestChannelConfig_ReasoningNestedMergePreservesSubKnobs: a second PATCH that sets
// a different sub-knob keeps the earlier one — the nested merge layers, it does not
// replace the whole reasoning block. (Also covers `model: quality` as accepted.)
func TestChannelConfig_ReasoningNestedMergePreservesSubKnobs(t *testing.T) {
	srv, id := reasoningTestServer(t, true)

	body, _ := json.Marshal(map[string]any{"reasoning": map[string]any{"mode": "plan"}})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	body, _ = json.Marshal(map[string]any{"reasoning": map[string]any{"model": "quality"}})
	rec = doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "1"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	r := decodeReasoning(t, rec.Body.Bytes())
	assert.Equal(t, "plan", r["mode"].Value, "earlier mode override survives the merge")
	assert.Equal(t, "channel", r["mode"].Source)
	assert.Equal(t, "quality", r["model"].Value)
	assert.Equal(t, "channel", r["model"].Source)
}

// TestChannelConfig_ReasoningPatchNullClears: a `reasoning: null` clears the whole
// block back to inherit (the block-level tri-state).
func TestChannelConfig_ReasoningPatchNullClears(t *testing.T) {
	srv, id := reasoningTestServer(t, true)

	body, _ := json.Marshal(map[string]any{"reasoning": map[string]any{"mode": "bid"}})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	body, _ = json.Marshal(map[string]any{"reasoning": nil})
	rec = doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "1"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	r := decodeReasoning(t, rec.Body.Bytes())
	assert.Equal(t, "off", r["mode"].Value, "cleared block inherits the default rung")
	assert.Equal(t, "default", r["mode"].Source)
}

// TestChannelConfig_ReasoningPatchDepthDeepRejected: `depth: deep` is a 400
// (capability-gated, Phase 4 unbuilt) — reject, not silent downgrade.
func TestChannelConfig_ReasoningPatchDepthDeepRejected(t *testing.T) {
	srv, id := reasoningTestServer(t, true)
	body, _ := json.Marshal(map[string]any{"reasoning": map[string]any{"mode": "plan", "depth": "deep"}})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	assert.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())
}

// TestChannelConfig_ReasoningPatchReviseRejected: `revise >= 1` is a 400 (Phase 5
// not deployed).
func TestChannelConfig_ReasoningPatchReviseRejected(t *testing.T) {
	srv, id := reasoningTestServer(t, true)
	body, _ := json.Marshal(map[string]any{"reasoning": map[string]any{"mode": "plan", "revise": 1}})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	assert.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())
}

// TestChannelConfig_ReasoningPatchUngovernedRejected: `mode != off` on a channel
// with no salience-gated member is a 400 — the knob does not by itself arm the
// gate. Uses the default (ungoverned) alice/bob roster.
func TestChannelConfig_ReasoningPatchUngovernedRejected(t *testing.T) {
	srv, id := channelConfigTestServer(t, true) // alice/bob, not salience-gated
	body, _ := json.Marshal(map[string]any{"reasoning": map[string]any{"mode": "bid"}})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	assert.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())
}

// TestChannelConfig_ReasoningPatchUnknownSubKnobRejected: an unrecognised reasoning
// sub-knob is a 400 (the closed-sub-knob-set gate), mirroring the top-level
// unknown-knob rejection.
func TestChannelConfig_ReasoningPatchUnknownSubKnobRejected(t *testing.T) {
	srv, id := reasoningTestServer(t, true)
	body, _ := json.Marshal(map[string]any{"reasoning": map[string]any{"ponder": true}})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	assert.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())
}

// TestChannelConfig_ReasoningFirstEditFreezesRung: a sparse first edit of an
// UNRELATED knob must not reset the channel's (default) reasoning rung — the
// ISSUE-0103 baseline freeze covers reasoning too.
func TestChannelConfig_ReasoningFirstEditFreezesRung(t *testing.T) {
	srv, id := reasoningTestServer(t, true)
	// Pre-seed a non-default rung on the router as a stand-in for a YAML-resolved
	// value, then edit a different knob.
	srv.channelRouter.SetReasoning(id, channels.ReasoningConfig{Mode: channels.ReasoningModeBid})

	body, _ := json.Marshal(map[string]any{"floor_control": false})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	assert.Equal(t, channels.ReasoningModeBid, srv.channelRouter.ReasoningFor(id).Mode,
		"the first edit must freeze the resolved reasoning rung, not reset it to off")
	r := decodeReasoning(t, rec.Body.Bytes())
	assert.Equal(t, "bid", r["mode"].Value)
}

// TestChannelConfig_ReasoningFirstEditOnDefaultStaysInherit is the F3 coverage
// gap: the conditional freeze's OTHER branch. A first edit of an unrelated knob on
// a DEFAULT-off channel must NOT freeze reasoning — it stays inherit ("default"
// source) so the channel keeps tracking the package default and remains responsive
// to the RFC 0051 PR 6 default flip (off→bid). The non-default branch is pinned by
// TestChannelConfig_ReasoningFirstEditFreezesRung; this pins the nil branch.
func TestChannelConfig_ReasoningFirstEditOnDefaultStaysInherit(t *testing.T) {
	srv, id := reasoningTestServer(t, true) // default-off rung, governed roster

	body, _ := json.Marshal(map[string]any{"floor_control": false})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	r := decodeReasoning(t, rec.Body.Bytes())
	for _, k := range []string{"mode", "model", "depth", "revise"} {
		assert.Equalf(t, "default", r[k].Source,
			"a default reasoning rung must stay inherit through an unrelated first edit (sub-knob %s)", k)
	}
	assert.Equal(t, "off", r["mode"].Value)
}

// TestChannelConfig_ReasoningOffModeStaysResponsiveDespiteNonDefaultModel is the
// F4 regression: the freeze must be PER-SUB-KNOB, not whole-rung. A channel whose
// rung is non-default ONLY because of model=quality (mode still off) must keep
// `mode` inherited — freezing an explicit mode=off would silently opt it out of the
// PR 6 default flip, a flip the operator (who only touched model) never declined.
// The non-default model still freezes.
func TestChannelConfig_ReasoningOffModeStaysResponsiveDespiteNonDefaultModel(t *testing.T) {
	srv, id := channelConfigTestServer(t, true) // ungoverned roster (mode stays off → no governance needed)
	// A YAML-resolved rung: mode off, model quality (the discouraged-but-accepted
	// economics value). Non-default rung, but mode is still the responsive default.
	srv.channelRouter.SetReasoning(id, channels.ReasoningConfig{
		Mode: channels.ReasoningModeOff, Model: channels.ReasoningModelQuality})

	body, _ := json.Marshal(map[string]any{"interaction_idle_timeout_seconds": 60})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	r := decodeReasoning(t, rec.Body.Bytes())
	assert.Equal(t, "default", r["mode"].Source,
		"mode=off must stay inherit (responsive to the PR 6 flip) even when model is non-default")
	assert.Equal(t, "channel", r["model"].Source, "the non-default model still freezes")
	assert.Equal(t, "quality", r["model"].Value)
}

// TestChannelConfig_ReasoningFirstEditWithDriftedGovernanceDoesNotBlock is the F2
// regression — the reasoning analogue of the escalation chair's drifted-member
// footgun (TestChannelConfig_FirstEditWithDriftedChairDoesNotBlockUnrelatedEdit).
// A router-resolved non-off rung whose salience-gated member has since left is
// inert at dispatch; freezing it into the first-edit baseline would let the
// cross-field governance rule reject an UNRELATED edit naming a knob the operator
// never touched. The baseline must drop the inert mode, so the edit succeeds and
// reasoning falls back to off — the same outcome dispatch already produces.
func TestChannelConfig_ReasoningFirstEditWithDriftedGovernanceDoesNotBlock(t *testing.T) {
	srv, id := channelConfigTestServer(t, true) // alice/bob — NOT salience-gated
	// A non-off rung the boot path seated on the router for a channel whose
	// salience-gated member has since drifted away: governance drift the rest of
	// the system absorbs silently (dispatch treats the rung as inert).
	srv.channelRouter.SetReasoning(id, channels.ReasoningConfig{Mode: channels.ReasoningModeBid})

	body, _ := json.Marshal(map[string]any{"interaction_idle_timeout_seconds": 60})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code,
		"an unrelated edit must not be blocked by a drifted, inert reasoning rung, body=%s", rec.Body.String())

	r := decodeReasoning(t, rec.Body.Bytes())
	assert.Equal(t, "off", r["mode"].Value, "the inert rung drops to off (matching dispatch), not frozen")
	assert.Equal(t, "default", r["mode"].Source)
}
