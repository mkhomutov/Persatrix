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
// group channel `group:planning`). Pass enabled=false to exercise the
// toggle-off gate. Returns the server and the canonical channel id.
func channelConfigTestServer(t *testing.T, enabled bool) (*Server, string) {
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
		[]channels.Member{{ParticipantID: "alice"}, {ParticipantID: "bob"}},
	))
	// Match the runtime-create governance seeding so the router has live entries
	// for the channel (floor on, default salience + reply budget).
	srv.applyRuntimeGroupGovernance(id)
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

// TestChannelConfig_InteractionBudgetDeferredEffective: interaction budget is
// persisted by a PATCH (source flips to channel) but its inherited effective
// value is reported null while live application is deferred (RFC 0050 Open
// item 4) — so an unset budget reads {value:null, source:default}.
func TestChannelConfig_InteractionBudgetDeferredEffective(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/"+id+"/config", nil)
	_, fields := decodeConfig(t, rec.Body.Bytes())
	assert.Nil(t, fields["interaction_budget_tokens"].Value, "inherited budget effective value is deferred → null")
	assert.Equal(t, "default", fields["interaction_budget_tokens"].Source)

	body, _ := json.Marshal(map[string]any{"interaction_budget_tokens": 50000})
	rec = doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		body, map[string]string{"If-Match": "0"})
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
	_, fields = decodeConfig(t, rec.Body.Bytes())
	assert.EqualValues(t, 50000, fields["interaction_budget_tokens"].Value)
	assert.Equal(t, "channel", fields["interaction_budget_tokens"].Source)
}
