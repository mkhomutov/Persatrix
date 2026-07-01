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

// armBody is a PATCH body that arms a channel: a positive cap, an escalation chair
// (`ada`, the role that authors the synthesis turn on close — RFC 0052 §D / PR 4),
// and an autonomous block naming `nova` as convener (distinct from the chair). The
// shared happy-path edit.
func armBody(extra map[string]any) []byte {
	auto := map[string]any{"enabled": true, "convener": "nova"}
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

// TestChannelConfig_AutonomousConvenerMustNotBeObserver: an `observer` (respond:
// never) convener is a 400 — it can never author the opening turn, the same
// guaranteed-futile case the escalation chair already rejects.
func TestChannelConfig_AutonomousConvenerMustNotBeObserver(t *testing.T) {
	srv, id := channelConfigTestServerWithMembers(t, true,
		[]channels.Member{{ParticipantID: "ghost", RespondPolicy: channels.RespondNever}, {ParticipantID: "ada"}})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		armBody(map[string]any{"convener": "ghost"}), map[string]string{"If-Match": "0"})
	assert.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())
}

// TestChannelConfig_AutonomousArmsViaInheritedCap: an operator who caps the fleet
// may arm autonomy WITHOUT setting a per-channel `interaction_budget_tokens` — the
// documented invariant on the REST path. The first-edit baseline freezes the
// channel's resolved budget into the blob, so the merged patch carries a positive
// cap even though the arming PATCH never named one. Pre-seeding the router budget
// stands in for a positive resolved (fleet-default) value.
func TestChannelConfig_AutonomousArmsViaInheritedCap(t *testing.T) {
	srv, id := autonomousTestServer(t)
	srv.channelRouter.SetInteractionBudgetTokens(id, 200000) // resolved (e.g. fleet-default) cap

	body, _ := json.Marshal(map[string]any{
		"escalation_chair_id": "ada",
		"autonomous":          map[string]any{"enabled": true, "convener": "nova"},
	})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code,
		"a positive resolved cap should let an arming PATCH omit the per-channel budget; body=%s", rec.Body.String())

	a := decodeAutonomous(t, rec.Body.Bytes())
	assert.Equal(t, true, a["enabled"].Value)
	assert.True(t, srv.channelRouter.AutonomousFor(id).Enabled)
}

// TestChannelConfig_AutonomousUncapRejectedOnArmed: lowering an armed channel's cap
// to 0 (uncapped) is a 400 — the mandatory cost cap holds across edits, not just at
// arming time. Uncapped autonomy is un-creatable AND un-reachable.
func TestChannelConfig_AutonomousUncapRejectedOnArmed(t *testing.T) {
	srv, id := autonomousTestServer(t)
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		armBody(nil), map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	body, _ := json.Marshal(map[string]any{"interaction_budget_tokens": 0})
	rec = doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "1"})
	assert.Equal(t, http.StatusBadRequest, rec.Code,
		"uncapping an armed channel must be rejected; body=%s", rec.Body.String())
}

// TestChannelConfig_AutonomousClearCapRejectedOnArmed: clearing an armed channel's
// per-channel cap (`interaction_budget_tokens: null`) is a 400 on the REST path. The
// apply-path gate cannot see the fleet default the cleared value would inherit, so
// it conservatively rejects rather than risk arming on an unverifiable cap — the
// documented load-vs-apply asymmetry, pinned in the safe direction.
func TestChannelConfig_AutonomousClearCapRejectedOnArmed(t *testing.T) {
	srv, id := autonomousTestServer(t)
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		armBody(nil), map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	body, _ := json.Marshal(map[string]any{"interaction_budget_tokens": nil})
	rec = doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "1"})
	assert.Equal(t, http.StatusBadRequest, rec.Code,
		"clearing the cap on an armed channel must be rejected; body=%s", rec.Body.String())
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
	// Pre-seed an armed rung on the router + a cap + a chair (PR 4 made the chair
	// mandatory and the un-closeable drop keys on it), standing in for a YAML-resolved
	// autonomous channel at revision 0. The unrelated edit is a chair-neutral knob
	// (salience_max_channel_members) — `floor_control:false` would now be rejected by
	// the chair's floor-control rule, so it is no longer a valid "unrelated" edit on a
	// chaired channel.
	srv.channelRouter.SetAutonomous(id, channels.AutonomousConfig{Enabled: true, Convener: "nova"})
	srv.channelRouter.SetEscalationChair(id, "ada")
	srv.channelRouter.SetInteractionBudgetTokens(id, 200000)

	body, _ := json.Marshal(map[string]any{"salience_max_channel_members": 8})
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

// TestChannelConfig_AutonomousFirstEditDropsObserverConvener: the freeze's
// drift branch must treat a convener that is a member but an OBSERVER (respond:
// never) the same as a non-member one — both are un-convenable at dispatch, so an
// unrelated first edit must DROP the inert armed block rather than freeze it.
// Freezing it would make the apply-path observer rule REJECT (400) an edit naming a
// knob the operator never touched (the bug that would surface if the baseline drop
// only checked membership and not enforceability).
func TestChannelConfig_AutonomousFirstEditDropsObserverConvener(t *testing.T) {
	srv, id := channelConfigTestServerWithMembers(t, true,
		[]channels.Member{{ParticipantID: "ghost", RespondPolicy: channels.RespondNever}, {ParticipantID: "ada"}})
	// Arm the router with a convener that is a member but an observer — un-convenable.
	srv.channelRouter.SetAutonomous(id, channels.AutonomousConfig{Enabled: true, Convener: "ghost"})
	srv.channelRouter.SetInteractionBudgetTokens(id, 200000)

	body, _ := json.Marshal(map[string]any{"floor_control": false})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code,
		"an observer convener must not block an unrelated first edit; body=%s", rec.Body.String())

	a := decodeAutonomous(t, rec.Body.Bytes())
	assert.Equal(t, false, a["enabled"].Value, "the inert armed block is dropped, not frozen")
	assert.Equal(t, "default", a["enabled"].Source)
	assert.False(t, srv.channelRouter.AutonomousFor(id).Enabled)
}

// TestChannelConfig_AutonomousFirstEditDropsArmedWithoutConvener: the freeze's
// drift branch must also drop an armed rung with NO convener at all — a degenerate
// state validation blocks at every write path, reachable here only via a direct
// router stamp. Freezing such a rung would make the apply-path convener rule REJECT
// (400) an unrelated first edit on the empty-convener case, exactly as a drifted
// convener would. The drop guard therefore keys on "armed AND un-convenable", which
// includes an absent convener, not only a drifted one.
func TestChannelConfig_AutonomousFirstEditDropsArmedWithoutConvener(t *testing.T) {
	srv, id := autonomousTestServer(t)
	// Arm the router with NO convener (and a cap, so only the missing convener — not
	// the cap — is under test).
	srv.channelRouter.SetAutonomous(id, channels.AutonomousConfig{Enabled: true})
	srv.channelRouter.SetInteractionBudgetTokens(id, 200000)

	body, _ := json.Marshal(map[string]any{"floor_control": false})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code,
		"an armed-but-convenerless rung must not block an unrelated first edit; body=%s", rec.Body.String())

	a := decodeAutonomous(t, rec.Body.Bytes())
	assert.Equal(t, false, a["enabled"].Value, "the inert armed block is dropped, not frozen")
	assert.Equal(t, "default", a["enabled"].Source)
	assert.False(t, srv.channelRouter.AutonomousFor(id).Enabled)
}

// TestChannelConfig_AutonomousConvenerDriftLocksThenRecovers pins the deliberate,
// escalation-chair-symmetric contract for a STORE-CANONICAL (revision > 0) armed
// channel whose convener later leaves the roster. Unlike the revision-0 first edit
// — where the baseline DROPS the drifted block — a revision > 0 edit merges over the
// stored blob, which still carries the convener, so the apply-path convener rule
// rejects EVERY subsequent edit, even an unrelated one. That lockout is the safety
// contract surfacing a broken armed channel loudly (the same way the escalation
// chair behaves), and it is always RECOVERABLE: disarming (or re-pointing/clearing
// the convener) clears the gate. Member removal deliberately does not clear the
// config reference — see [ChannelRouter.validateAutonomousConvener].
func TestChannelConfig_AutonomousConvenerDriftLocksThenRecovers(t *testing.T) {
	srv, id := autonomousTestServer(t) // members: nova + ada

	// Arm via REST → the channel becomes store-canonical at revision 1.
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		armBody(nil), map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	// The convener leaves the roster. RemoveMember does not touch the config blob,
	// so the stored autonomous rung still names nova.
	require.NoError(t, srv.channelStore.RemoveMember(t.Context(), id, "nova"))

	// An UNRELATED edit is now locked: the merged patch (stored blob + an unrelated,
	// chair-neutral knob) still carries the drifted convener, which the apply-path rule
	// rejects. (The knob is salience_max_channel_members, not floor_control:false —
	// the latter would now be rejected by the chair's floor-control rule and mask the
	// convener-drift lockout this test pins.)
	body, _ := json.Marshal(map[string]any{"salience_max_channel_members": 8})
	rec = doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "1"})
	assert.Equal(t, http.StatusBadRequest, rec.Code,
		"a drifted convener locks unrelated edits on a store-canonical channel; body=%s", rec.Body.String())

	// Recovery: disarming the channel clears the gate (the revision is unchanged by
	// the rejected edit, so If-Match is still 1).
	body, _ = json.Marshal(map[string]any{"autonomous": map[string]any{"enabled": false}})
	rec = doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "1"})
	require.Equal(t, http.StatusOK, rec.Code,
		"disarming the channel must recover from the drift lockout; body=%s", rec.Body.String())
	assert.False(t, srv.channelRouter.AutonomousFor(id).Enabled)
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
