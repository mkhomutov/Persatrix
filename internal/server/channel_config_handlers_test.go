package server

import (
	"encoding/json"
	"net/http"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// channelConfigTestServer wires a real SQLite channel store + router onto a
// test Server with the RFC 0050 `config_edit_enabled` toggle ON (and a seeded
// group channel `group:planning` with members alice + bob). Pass enabled=false
// to exercise the toggle-off gate. Returns the server and the canonical channel
// id.
func channelConfigTestServer(t *testing.T, enabled bool) (*Server, string) {
	t.Helper()
	return channelConfigTestServerWithMembers(t, enabled,
		[]channels.Member{{ParticipantID: "alice"}, {ParticipantID: "bob"}})
}

// channelConfigTestServerWithMembers is channelConfigTestServer with the seeded
// channel's membership roster injected by the caller — used by tests that need a
// specific member shape (e.g. an observer chair) the default alice/bob roster
// cannot express.
func channelConfigTestServerWithMembers(t *testing.T, enabled bool, members []channels.Member) (*Server, string) {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "channels.db")
	store, err := channels.NewSQLiteStore(dbPath, channels.SQLiteOptions{
		MaxChannels: 50,
		Logger:      zap.NewNop(),
	})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	router := channels.NewChannelRouter(store, channels.NoopDispatcher{}, zap.NewNop(), nil)

	logger := zap.NewNop()
	cfg := DefaultUIConfig()
	cfg.Panels["channel_timeline"] = PanelToggle{Enabled: true, CreateEnabled: true, ConfigEditEnabled: enabled}
	srv, err := New("127.0.0.1:0", t.TempDir(),
		state.NewInMemoryStore(logger),
		registry.NewInMemoryRegistry(logger),
		planner.NewYAMLPlanner(logger),
		logger,
		WithChannels(store, router),
		WithUIConfig(cfg),
	)
	require.NoError(t, err)

	const id = "group:planning"
	require.NoError(t, store.CreateChannelWithMembers(t.Context(),
		channels.Channel{ID: id, Name: "planning", Type: channels.ChannelTypeGroup},
		members,
	))
	// Match the runtime-create governance seeding so the router has live entries
	// for the channel (floor on, default salience + reply budget, and the
	// governance-aware RFC 0051 reasoning rung). A salience-gated roster resolves
	// to the PR 6 `bid` default, exactly like production's create handler / boot
	// ResolveReasoning (governance is read off the store members just written).
	srv.applyRuntimeGroupGovernance(t.Context(), id)
	return srv, id
}

// decodeConfig unmarshals a GET/PATCH config response into the revision plus a
// flat map of knob → {value, source}, so a test can assert on a single knob
// without mirroring the whole struct.
func decodeConfig(t *testing.T, raw []byte) (int64, map[string]struct {
	Value  any    `json:"value"`
	Source string `json:"source"`
}) {
	t.Helper()
	var envelope struct {
		Revision int64 `json:"revision"`
	}
	require.NoError(t, json.Unmarshal(raw, &envelope))

	var rawFields map[string]json.RawMessage
	require.NoError(t, json.Unmarshal(raw, &rawFields))
	delete(rawFields, "revision")

	fields := make(map[string]struct {
		Value  any    `json:"value"`
		Source string `json:"source"`
	}, len(rawFields))
	for name, blob := range rawFields {
		var f struct {
			Value  any    `json:"value"`
			Source string `json:"source"`
		}
		require.NoError(t, json.Unmarshal(blob, &f), "knob %s", name)
		fields[name] = f
	}
	return envelope.Revision, fields
}

// TestChannelConfig_GetDefaults: a never-edited channel reports revision 0 and
// every knob sourced from the default.
func TestChannelConfig_GetDefaults(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/"+id+"/config", nil)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	revision, fields := decodeConfig(t, rec.Body.Bytes())
	assert.Equal(t, int64(0), revision)
	assert.Equal(t, "default", fields["floor_control"].Source)
	assert.Equal(t, true, fields["floor_control"].Value, "group floor control defaults ON")
	assert.Equal(t, "default", fields["end_vote_threshold"].Source)
}

// TestChannelConfig_PatchSetThenGet: the load-bearing happy path — a PATCH with
// the current revision as If-Match flips a router-held knob, bumps the revision,
// and a follow-up GET reflects the new value sourced from the channel.
func TestChannelConfig_PatchSetThenGet(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)

	body, _ := json.Marshal(map[string]any{"floor_control": false})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	revision, fields := decodeConfig(t, rec.Body.Bytes())
	assert.Equal(t, int64(1), revision, "an apply bumps the revision")
	assert.Equal(t, false, fields["floor_control"].Value)
	assert.Equal(t, "channel", fields["floor_control"].Source)

	// The router took the edit live.
	enabled, _, _ := srv.channelRouter.FloorControlFor(id)
	assert.False(t, enabled, "PATCH must stamp the router, not just the store")

	// GET round-trips the same state.
	rec = doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/"+id+"/config", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	revision, fields = decodeConfig(t, rec.Body.Bytes())
	assert.Equal(t, int64(1), revision)
	assert.Equal(t, false, fields["floor_control"].Value)
	assert.Equal(t, "channel", fields["floor_control"].Source)
}

// TestChannelConfig_PatchNullUnsets: a `null` value clears the override back to
// inherit (RFC 0050 tri-state), distinct from omitting the key.
func TestChannelConfig_PatchNullUnsets(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)

	// Set floor_control:false at revision 0.
	body, _ := json.Marshal(map[string]any{"floor_control": false})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	// Unset it with an explicit null at revision 1.
	body, _ = json.Marshal(map[string]any{"floor_control": nil})
	rec = doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "1"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	_, fields := decodeConfig(t, rec.Body.Bytes())
	assert.Equal(t, "default", fields["floor_control"].Source, "null must clear the override")
	assert.Equal(t, true, fields["floor_control"].Value, "cleared knob inherits the group default ON")
}

// TestChannelConfig_PatchMergePreservesOtherKnobs: a sparse PATCH that omits a
// previously-set knob leaves it in place (REST-layer merge, not wholesale
// replace).
func TestChannelConfig_PatchMergePreservesOtherKnobs(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)

	body, _ := json.Marshal(map[string]any{"floor_control": false})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code)

	// A second PATCH touching a different knob must NOT resurrect floor_control's
	// default — the merge keeps the prior override.
	body, _ = json.Marshal(map[string]any{"max_replies_per_participant_per_interaction": 2})
	rec = doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "1"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	_, fields := decodeConfig(t, rec.Body.Bytes())
	assert.Equal(t, "channel", fields["floor_control"].Source, "merge must preserve the earlier override")
	assert.Equal(t, false, fields["floor_control"].Value)
	assert.Equal(t, "channel", fields["max_replies_per_participant_per_interaction"].Source)
	assert.EqualValues(t, 2, fields["max_replies_per_participant_per_interaction"].Value)
}

// TestChannelConfig_FirstEditPreservesYAMLSeededChair is the ISSUE-0103
// regression: on a YAML-seeded channel (a router-held escalation chair that was
// never written to the store — revision 0), the FIRST sparse edit of an
// unrelated knob must NOT silently detach the chair. The REST layer seeds the
// merge base from the channel's resolved governance before layering the patch,
// so editing only the idle timeout leaves the chair (and every other resolved
// knob) intact while flipping the channel to store-canonical.
func TestChannelConfig_FirstEditPreservesYAMLSeededChair(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)

	// Seed a chair the way the YAML boot path does (ResolveEscalationChairs →
	// SetEscalationChair): router-held only, absent from the store. "alice" is a
	// declared member of the seeded channel, so the cross-field rule accepts it.
	srv.channelRouter.SetEscalationChair(id, "alice")
	chair, _ := srv.channelRouter.EscalationChairFor(id)
	require.Equal(t, "alice", chair, "precondition: chair is live on the router but unstored")

	// First edit: an unrelated knob, the exact shape of the issue's repro.
	body, _ := json.Marshal(map[string]any{"interaction_idle_timeout_seconds": 60})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	revision, fields := decodeConfig(t, rec.Body.Bytes())
	assert.Equal(t, int64(1), revision, "the edit committed")
	assert.EqualValues(t, 60, fields["interaction_idle_timeout_seconds"].Value, "the edited knob took effect")
	// The footgun the issue describes: this assertion FAILS before the fix.
	assert.Equal(t, "alice", fields["escalation_chair_id"].Value,
		"the un-edited YAML-seeded chair must survive the first edit")
	chairAfter, _ := srv.channelRouter.EscalationChairFor(id)
	assert.Equal(t, "alice", chairAfter, "the router must still hold the chair after the apply")
}

// TestChannelConfig_FirstEditFreezesDefaultsAsChannel documents the deliberate
// consequence of the ISSUE-0103 fix: a first edit makes the channel
// store-canonical, so its previously-inherited knobs are snapshotted as explicit
// overrides and their provenance flips from "default" to "channel" — the same
// freeze the YAML adopt path makes. This is the accepted interim cost (true
// sparse-layering over the YAML baseline is RFC 0050 Phase 3); pin it so the flip
// is an intended, tested property rather than a surprise.
//
// It also pins the matched-pair invariant between [Server.resolvedConfigBaseline]
// (which builds the freeze set) and [Server.buildChannelConfigResponse] (which
// reports provenance): EVERY router-held knob the response surfaces must be in
// the seeded baseline, so all of them flip to "channel" on a first edit. If a
// future change adds a router-held knob to one method but not the other, the new
// knob stays "default" after a first edit and an assertion below goes red — the
// drift the two methods' cross-references warn against, caught in CI.
func TestChannelConfig_FirstEditFreezesDefaultsAsChannel(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)

	body, _ := json.Marshal(map[string]any{"interaction_idle_timeout_seconds": 60})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	_, fields := decodeConfig(t, rec.Body.Bytes())
	// Every UNCONDITIONALLY-seeded router-held knob freezes to "channel" — the
	// edited one (interaction_idle_timeout_seconds) and every un-edited one alike.
	for _, knob := range []string{
		"floor_control",
		"salience_max_channel_members",
		"interaction_budget_tokens",
		"max_replies_per_participant_per_interaction",
		"end_vote_threshold",
		"end_vote_window",
		"interaction_idle_timeout_seconds",
	} {
		assert.Equalf(t, "channel", fields[knob].Source,
			"%s must freeze to an explicit override once the channel is canonical", knob)
	}
	// The escalation chair is seeded CONDITIONALLY (only when set + enforceable);
	// none is seeded here, so it stays inherited rather than freezing.
	assert.Equal(t, "default", fields["escalation_chair_id"].Source,
		"no chair was seeded, so the conditional capture leaves it inherited")
}

// TestChannelConfig_FirstEditFloorOffWithYAMLChairRejected is the beneficial
// flip-side of the ISSUE-0103 fix: because a first edit now seeds the merge base
// from the channel's resolved governance, a YAML-seeded escalation chair is part
// of the set the cross-field validator sees. So a lone `floor_control:false` on a
// chaired channel — silently accepted (and chair-detaching) before the fix — is
// now rejected with the chair/floor-control conflict, and nothing persists. This
// is the issue's "make the validator consult effective state" guard, delivered
// for free by the baseline seeding.
func TestChannelConfig_FirstEditFloorOffWithYAMLChairRejected(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)
	srv.channelRouter.SetEscalationChair(id, "alice") // YAML-seeded, unstored

	body, _ := json.Marshal(map[string]any{"floor_control": false})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	assert.Equal(t, http.StatusBadRequest, rec.Code,
		"lone floor_control:false on a chaired channel must be rejected, body=%s", rec.Body.String())

	// Nothing was written: the channel is still at revision 0 and the chair stands.
	rec = doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/"+id+"/config", nil)
	revision, fields := decodeConfig(t, rec.Body.Bytes())
	assert.Equal(t, int64(0), revision, "a rejected apply must not bump the revision")
	assert.Equal(t, "alice", fields["escalation_chair_id"].Value, "the chair survives a rejected edit")
}

// TestChannelConfig_FirstEditWithDriftedChairDoesNotBlockUnrelatedEdit is the
// regression for the ISSUE-0103 fix's OWN footgun. Seeding the merge base from
// resolved governance promotes the router-held escalation chair INTO the patch
// the apply path validates. If that chair has drifted out of the channel's
// membership (a member who left after the YAML/boot seeding — drift the boot
// replay [ChannelRouter.ResolveFromStore] and dispatch-time escalation
// [ChannelRouter.maybeEscalateStall] both deliberately TOLERATE), re-running the
// cross-field chair-membership rule would reject an UNRELATED first edit with a
// 400 naming a chair the operator never touched. The baseline must instead drop
// a non-enforceable chair: the edit succeeds and the already-inert chair is left
// unset — the same outcome dispatch already produces for it — instead of being
// frozen into the store (which would persist an invalid chair AND keep blocking).
func TestChannelConfig_FirstEditWithDriftedChairDoesNotBlockUnrelatedEdit(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)
	// A chair the boot path seated on the router but who is NOT a declared member
	// of {alice, bob}: membership drift the rest of the system absorbs silently.
	srv.channelRouter.SetEscalationChair(id, "ghost-who-left")

	body, _ := json.Marshal(map[string]any{"interaction_idle_timeout_seconds": 60})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code,
		"an unrelated first edit must NOT be blocked by a drifted (non-member) chair, body=%s", rec.Body.String())

	revision, fields := decodeConfig(t, rec.Body.Bytes())
	assert.Equal(t, int64(1), revision, "the edit committed")
	assert.EqualValues(t, 60, fields["interaction_idle_timeout_seconds"].Value, "the edited knob took effect")
	// The drifted chair was never enforceable, so it is dropped rather than frozen.
	assert.Equal(t, "", fields["escalation_chair_id"].Value,
		"a non-member chair is not seeded into the first-edit baseline")
}

// TestChannelConfig_FirstEditWithObserverChairDoesNotBlockUnrelatedEdit is the
// sibling of the drift case: a chair who IS a declared member but is an observer
// (respond: never) is just as guaranteed-inert as a non-member (the receiver
// gate suppresses it before any LLM), and [ChannelRouter.validateEscalationChair]
// rejects it for exactly that reason. So the first-edit baseline must drop an
// observer chair too, or an unrelated edit would 400 on a chair the operator
// never set.
func TestChannelConfig_FirstEditWithObserverChairDoesNotBlockUnrelatedEdit(t *testing.T) {
	srv, id := channelConfigTestServerWithMembers(t, true, []channels.Member{
		{ParticipantID: "alice"},
		{ParticipantID: "cleo", RespondPolicy: channels.RespondNever},
	})
	srv.channelRouter.SetEscalationChair(id, "cleo") // member, but an observer

	body, _ := json.Marshal(map[string]any{"interaction_idle_timeout_seconds": 60})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code,
		"an unrelated first edit must NOT be blocked by an observer chair, body=%s", rec.Body.String())

	_, fields := decodeConfig(t, rec.Body.Bytes())
	assert.Equal(t, "", fields["escalation_chair_id"].Value,
		"an observer chair is not seeded into the first-edit baseline")
}

// TestChannelConfig_StaleRevisionConflict: an If-Match that no longer matches
// the stored revision yields 409 and does not write.
func TestChannelConfig_StaleRevisionConflict(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)

	body, _ := json.Marshal(map[string]any{"floor_control": false})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code)

	// Replay the same stale If-Match: 0 — the store is now at 1.
	rec = doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	assert.Equal(t, http.StatusConflict, rec.Code, "body=%s", rec.Body.String())
}

// TestChannelConfig_InvalidValueRejected: an out-of-range knob 400s before any
// write (validation runs ahead of the store).
func TestChannelConfig_InvalidValueRejected(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)
	body, _ := json.Marshal(map[string]any{"salience_max_channel_members": 0})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	assert.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())

	// No write happened.
	rec = doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/"+id+"/config", nil)
	revision, _ := decodeConfig(t, rec.Body.Bytes())
	assert.Equal(t, int64(0), revision)
}

// TestChannelConfig_UnknownKeyRejected: a key outside the governed knob set 400s
// (additionalProperties:false at the wire boundary).
func TestChannelConfig_UnknownKeyRejected(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)
	body := []byte(`{"not_a_knob": 1}`)
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	assert.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())
}

// TestChannelConfig_FractionalIntRejected pins the decode strictness on integer
// knobs: a fractional JSON number for an int-typed knob — here end_vote_threshold,
// whose integer form 2 would pass validation — is a 400 at the decode boundary
// ([decodeKnob]), never reaching the apply path, so the revision stays 0. The
// value 2.5 isolates the decode rejection from range validation (2 is in range),
// guarding against a future switch to a lax number type (json.Number / any) that
// would silently truncate.
func TestChannelConfig_FractionalIntRejected(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)
	body := []byte(`{"end_vote_threshold": 2.5}`)
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	assert.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())

	// No write happened — the bad decode preceded the apply.
	rec = doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/"+id+"/config", nil)
	revision, _ := decodeConfig(t, rec.Body.Bytes())
	assert.Equal(t, int64(0), revision)
}

// TestChannelConfig_MissingIfMatch: an absent If-Match header is a 428 — the
// optimistic-concurrency contract requires the caller to state what it saw.
func TestChannelConfig_MissingIfMatch(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)
	body, _ := json.Marshal(map[string]any{"floor_control": false})
	rec := doRequest(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config", body)
	assert.Equal(t, http.StatusPreconditionRequired, rec.Code, "body=%s", rec.Body.String())
}

// TestChannelConfig_ToggleOff_Forbidden: with config_edit_enabled off, both the
// read and the write surfaces are a clean 403 (the whole surface ships dark).
func TestChannelConfig_ToggleOff_Forbidden(t *testing.T) {
	srv, id := channelConfigTestServer(t, false)

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/"+id+"/config", nil)
	assert.Equal(t, http.StatusForbidden, rec.Code, "GET body=%s", rec.Body.String())

	body, _ := json.Marshal(map[string]any{"floor_control": false})
	rec = doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	assert.Equal(t, http.StatusForbidden, rec.Code, "PATCH body=%s", rec.Body.String())
}

// TestChannelConfig_UnknownChannel: a config read/write on a missing channel is
// a 404.
func TestChannelConfig_UnknownChannel(t *testing.T) {
	srv, _ := channelConfigTestServer(t, true)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/group:ghost/config", nil)
	assert.Equal(t, http.StatusNotFound, rec.Code, "body=%s", rec.Body.String())
}

// TestChannelConfig_InteractionBudgetEffectiveResolved: interaction budget is now
// router-held (RFC 0050 amendment — interaction-budget enforcement), so its
// inherited effective value resolves through the router getter (no longer null).
// An unset budget reads {value:0, source:default} (0 = the uncapped fleet
// default), and a PATCH flips the source to channel and echoes the set value.
func TestChannelConfig_InteractionBudgetEffectiveResolved(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/"+id+"/config", nil)
	_, fields := decodeConfig(t, rec.Body.Bytes())
	assert.EqualValues(t, 0, fields["interaction_budget_tokens"].Value, "inherited budget resolves to the fleet default (0 = uncapped), not null")
	assert.Equal(t, "default", fields["interaction_budget_tokens"].Source)

	body, _ := json.Marshal(map[string]any{"interaction_budget_tokens": 50000})
	rec = doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
	_, fields = decodeConfig(t, rec.Body.Bytes())
	assert.EqualValues(t, 50000, fields["interaction_budget_tokens"].Value)
	assert.Equal(t, "channel", fields["interaction_budget_tokens"].Source)
}
