// RFC 0052 PR 4a — the escalation-chair leg of the autonomous config REST tests,
// split out of channel_config_autonomous_test.go (at the review-friendly size cap,
// scripts/checks/file_size.py --strict). Mirrors the repo's established split
// convention (channel_config_autonomous.go itself was split out of
// channel_config_handlers.go for the same reason). Pins the mandatory-chair 400
// (`ErrAutonomousChairRequired`), the chair leg of the first-edit baseline drop
// (chairless / drifted / observer chair), and the store-canonical chair-drift
// lockout + its recovery — the chair counterpart to the convener tests that remain
// in channel_config_autonomous_test.go. Shares that file's helpers
// (autonomousTestServer, decodeAutonomous, armBody) via the same package.
package server

import (
	"encoding/json"
	"net/http"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// TestChannelConfig_AutonomousChairRequired: arming without an `escalation_chair_id`
// is a 400 — RFC 0052 §D / PR 4 makes the chair (the role that synthesizes on close)
// mandatory on an armed channel. The body carries a cap + a convener but no chair.
func TestChannelConfig_AutonomousChairRequired(t *testing.T) {
	srv, id := autonomousTestServer(t)
	body, _ := json.Marshal(map[string]any{
		"interaction_budget_tokens": 200000,
		"autonomous":                map[string]any{"enabled": true, "convener": "nova"},
	})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	assert.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())
}

// TestChannelConfig_AutonomousFirstEditDropsChairlessArmed: the chair leg of the
// freeze's un-closeable drop (PR 4). An armed rung with NO `escalation_chair_id` is
// un-closeable (no role to author the synthesis turn), so an unrelated first edit must
// DROP it rather than freeze it — freezing would make the apply-path chair-required
// rule REJECT (400) an edit naming a knob the operator never touched, the chair
// mirror of the convenerless drop. The edit proceeds and the channel reads back
// inherit/disabled.
func TestChannelConfig_AutonomousFirstEditDropsChairlessArmed(t *testing.T) {
	srv, id := autonomousTestServer(t)
	// Arm the router with a valid convener but NO chair (and a cap, so only the missing
	// chair — not the cap or convener — is under test).
	srv.channelRouter.SetAutonomous(id, channels.AutonomousConfig{Enabled: true, Convener: "nova"})
	srv.channelRouter.SetInteractionBudgetTokens(id, 200000)

	body, _ := json.Marshal(map[string]any{"salience_max_channel_members": 8})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code,
		"a chairless armed rung must not block an unrelated first edit; body=%s", rec.Body.String())

	a := decodeAutonomous(t, rec.Body.Bytes())
	assert.Equal(t, false, a["enabled"].Value, "the un-closeable armed block is dropped, not frozen")
	assert.Equal(t, "default", a["enabled"].Source)
	assert.False(t, srv.channelRouter.AutonomousFor(id).Enabled)
}

// TestChannelConfig_AutonomousFirstEditDropsDriftedChair: the drift variant of the
// chair leg — an armed rung whose chair has drifted OUT of membership (here a
// non-member chair) is un-closeable, so an unrelated first edit drops it. Without the
// chair-enforceability check threaded into [Server.autonomousBaseline], the apply-path
// chair rule ([ChannelRouter.validateEscalationChair]) would 400 the unrelated edit.
func TestChannelConfig_AutonomousFirstEditDropsDriftedChair(t *testing.T) {
	srv, id := autonomousTestServer(t) // members: nova + ada
	// Arm with a valid convener (nova) but a chair that is NOT a member (drifted out).
	srv.channelRouter.SetAutonomous(id, channels.AutonomousConfig{Enabled: true, Convener: "nova"})
	srv.channelRouter.SetEscalationChair(id, "ghost")
	srv.channelRouter.SetInteractionBudgetTokens(id, 200000)

	body, _ := json.Marshal(map[string]any{"salience_max_channel_members": 8})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code,
		"a drifted chair must not block an unrelated first edit; body=%s", rec.Body.String())

	a := decodeAutonomous(t, rec.Body.Bytes())
	assert.Equal(t, false, a["enabled"].Value, "the un-closeable armed block is dropped, not frozen")
	assert.Equal(t, "default", a["enabled"].Source)
	assert.False(t, srv.channelRouter.AutonomousFor(id).Enabled)
}

// TestChannelConfig_AutonomousFirstEditDropsObserverChair: the OBSERVER variant of
// the chair leg, distinct from TestChannelConfig_AutonomousFirstEditDropsDriftedChair
// (which covers a non-member chair). [Server.chairIsEnforceableMember] treats a chair
// who IS a declared member but is an `observer` (respond: never) the same as a
// non-member — the receiver gate suppresses it before any LLM, so it could never
// author the synthesis turn either — but that branch of the helper was previously
// only exercised for the (pre-existing, non-autonomous) escalation-chair baseline,
// never for the autonomous chair-required drop this PR adds. Pinned separately so a
// future change that special-cased "member-but-observer" would be caught here.
func TestChannelConfig_AutonomousFirstEditDropsObserverChair(t *testing.T) {
	srv, id := channelConfigTestServerWithMembers(t, true,
		[]channels.Member{{ParticipantID: "nova"}, {ParticipantID: "ghost", RespondPolicy: channels.RespondNever}})
	// Arm with a valid convener (nova) but a chair that IS a member, just an observer.
	srv.channelRouter.SetAutonomous(id, channels.AutonomousConfig{Enabled: true, Convener: "nova"})
	srv.channelRouter.SetEscalationChair(id, "ghost")
	srv.channelRouter.SetInteractionBudgetTokens(id, 200000)

	body, _ := json.Marshal(map[string]any{"salience_max_channel_members": 8})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code,
		"an observer chair must not block an unrelated first edit; body=%s", rec.Body.String())

	a := decodeAutonomous(t, rec.Body.Bytes())
	assert.Equal(t, false, a["enabled"].Value, "the un-closeable armed block is dropped, not frozen")
	assert.Equal(t, "default", a["enabled"].Source)
	assert.False(t, srv.channelRouter.AutonomousFor(id).Enabled)
}

// TestChannelConfig_AutonomousChairDriftAtRevisionLocksThenRecovers is the chair
// analogue of TestChannelConfig_AutonomousConvenerDriftLocksThenRecovers. Distinct
// from the revision-0 TestChannelConfig_AutonomousFirstEditDropsDriftedChair (which
// covers the baseline-freeze DROP): here the channel is already STORE-CANONICAL
// (revision > 0), so the merge base is the stored blob, which keeps naming the chair
// even after that member leaves the roster (RemoveMember does not touch the config
// blob) — [ChannelRouter.validateEscalationChair] then rejects EVERY subsequent
// edit, even an unrelated one, exactly the lockout the convener test pins.
//
// Recovery differs from the convener case in one load-bearing way:
// [ChannelRouter.validateAutonomousConvener] short-circuits on `!effectiveEnabled()`,
// so disarming ALONE recovers a drifted convener. [ChannelRouter.validateEscalationChair]
// has no such short-circuit — a channel's escalation chair is a general RFC 0050
// knob, independent of `autonomous.enabled` — so it keeps validating the drifted
// chair even after the autonomous block is disarmed. Recovery therefore requires
// clearing (or re-pointing) `escalation_chair_id` explicitly, not just disarming.
func TestChannelConfig_AutonomousChairDriftAtRevisionLocksThenRecovers(t *testing.T) {
	srv, id := autonomousTestServer(t) // members: nova + ada

	// Arm via REST → the channel becomes store-canonical at revision 1 (chair = ada).
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		armBody(nil), map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	// The chair leaves the roster. RemoveMember does not touch the config blob, so
	// the stored escalation_chair_id still names ada.
	require.NoError(t, srv.channelStore.RemoveMember(t.Context(), id, "ada"))

	// An UNRELATED edit is now locked: the merged patch (stored blob + an unrelated,
	// chair-neutral knob) still carries the drifted chair, which validateEscalationChair
	// rejects regardless of autonomous.enabled.
	body, _ := json.Marshal(map[string]any{"salience_max_channel_members": 8})
	rec = doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "1"})
	assert.Equal(t, http.StatusBadRequest, rec.Code,
		"a drifted chair locks unrelated edits on a store-canonical channel; body=%s", rec.Body.String())

	// Disarming ALONE does not recover: escalation_chair_id has no enabled-gated
	// short-circuit, so the still-drifted chair keeps failing validateEscalationChair.
	body, _ = json.Marshal(map[string]any{"autonomous": map[string]any{"enabled": false}})
	rec = doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "1"})
	assert.Equal(t, http.StatusBadRequest, rec.Code,
		"disarming alone must not recover a drifted chair (no enabled-gated short-circuit on the chair rule); body=%s", rec.Body.String())

	// Recovery: disarm AND clear the drifted chair in the same PATCH. Clearing the
	// chair alone (while still armed) would instead trip the chair-required gate
	// this PR added, so both must move together.
	body, _ = json.Marshal(map[string]any{
		"autonomous":          map[string]any{"enabled": false},
		"escalation_chair_id": nil,
	})
	rec = doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "1"})
	require.Equal(t, http.StatusOK, rec.Code,
		"disarming + clearing the drifted chair together must recover from the lockout; body=%s", rec.Body.String())
	assert.False(t, srv.channelRouter.AutonomousFor(id).Enabled)
}
