package server

// channel_config_autonomous_runtime_test.go — RFC 0052 §E the convening-count /
// aggregate-bound READOUT (v0.3.11 PR 7b, this slice). TDD-first: pins the LIVE
// (non-config) runtime view the GET …/config payload now carries alongside the
// `autonomous` config block, so the web AutonomousSettings panel + the CLI
// `channel config get` can show an operator how much of a standing channel's
// aggregate convening allowance has been spent.
//
// PR 7b-i landed the count itself ([channels.ChannelRouter.ConveningCount]) and
// the `max_convenings` ceiling that refuses the (max+1)th convene; nothing yet
// SURFACED the count — it was consumed only in channels-package tests. This slice
// reports it (plus the derived remaining allowance) on the RFC 0050 read surface.
//
// The remaining allowance is computed SERVER-SIDE so the "unbounded ⇒ no bound to
// remain against" and the "never report negative" rules live in one place (a
// lowered `max_convenings` can leave the count above the new bound): a positive
// `max_convenings` yields a clamped-at-zero remaining, a non-positive one yields
// JSON null (unbounded). The clients render, they do not compute.

import (
	"encoding/json"
	"net/http"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// autonomousRuntime is the pure count→readout mapping — the null-when-unbounded
// and clamp-at-zero rules, exercised without a router so the arithmetic is pinned
// independently of the convene wiring.
func TestAutonomousRuntime_BoundedReportsRemaining(t *testing.T) {
	got := autonomousRuntime(2, channels.AutonomousConfig{MaxConvenings: 5})
	assert.Equal(t, 2, got.ConveningCount)
	require.NotNil(t, got.ConveningsRemaining, "a positive max_convenings has a remaining allowance")
	assert.Equal(t, 3, *got.ConveningsRemaining)
}

func TestAutonomousRuntime_UnboundedReportsNilRemaining(t *testing.T) {
	got := autonomousRuntime(4, channels.AutonomousConfig{MaxConvenings: 0})
	assert.Equal(t, 4, got.ConveningCount, "the count is tracked even when unbounded")
	assert.Nil(t, got.ConveningsRemaining, "max_convenings=0 is unbounded ⇒ no remaining bound (null)")
}

// A max_convenings LOWERED below the already-spent count must not report a
// negative remaining — the readout clamps to zero (the bound is exhausted).
func TestAutonomousRuntime_LoweredBoundClampsRemainingAtZero(t *testing.T) {
	got := autonomousRuntime(7, channels.AutonomousConfig{MaxConvenings: 5})
	require.NotNil(t, got.ConveningsRemaining)
	assert.Equal(t, 0, *got.ConveningsRemaining, "count above a lowered bound clamps to zero, never negative")
}

// TestChannelConfig_AutonomousRuntime_ReflectsConvene: the end-to-end readout —
// a successful convene increments the router's count, and a follow-up GET
// …/config reports it (with the derived remaining allowance) in the
// `autonomous_runtime` block.
func TestChannelConfig_AutonomousRuntime_ReflectsConvene(t *testing.T) {
	srv, id := channelConfigTestServerWithMembers(t, true, []channels.Member{
		{ParticipantID: "alice", RespondPolicy: channels.RespondAlways},
		{ParticipantID: "bob", RespondPolicy: channels.RespondAlways},
	})
	// Armed standing channel: a convener, a chair (PR 4a mandatory), and a
	// max_convenings aggregate bound so the readout has a ceiling to count against.
	srv.channelRouter.SetAutonomous(id, channels.AutonomousConfig{
		Enabled:       true,
		Convener:      "alice",
		Topic:         "Should we adopt a monorepo?",
		Goal:          "A synthesized recommendation.",
		MaxConvenings: 3,
	})
	srv.channelRouter.SetEscalationChair(id, "bob")

	// Before any convene: count 0, full allowance remaining.
	rt := getAutonomousRuntime(t, srv, id)
	assert.Equal(t, 0, rt.ConveningCount)
	require.NotNil(t, rt.ConveningsRemaining)
	assert.Equal(t, 3, *rt.ConveningsRemaining)

	// One successful convene (NoopDispatcher ⇒ the opener dispatches ⇒ the slot
	// is consumed, not released).
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/"+id+"/convene", nil)
	require.Equal(t, http.StatusAccepted, rec.Code, "body=%s", rec.Body.String())

	rt = getAutonomousRuntime(t, srv, id)
	assert.Equal(t, 1, rt.ConveningCount, "the readout reflects the convening that just dispatched")
	require.NotNil(t, rt.ConveningsRemaining)
	assert.Equal(t, 2, *rt.ConveningsRemaining, "one of three convenings spent")
}

// TestChannelConfig_AutonomousRuntime_UnboundedNullRemaining: an armed channel
// with no count bound reports the count but a JSON-null remaining.
func TestChannelConfig_AutonomousRuntime_UnboundedNullRemaining(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)
	srv.channelRouter.SetAutonomous(id, channels.AutonomousConfig{
		Enabled:  true,
		Convener: "alice",
		Topic:    "Anything.",
		Goal:     "A recommendation.",
		// no MaxConvenings ⇒ unbounded
	})
	srv.channelRouter.SetEscalationChair(id, "bob")

	rt := getAutonomousRuntime(t, srv, id)
	assert.Equal(t, 0, rt.ConveningCount)
	assert.Nil(t, rt.ConveningsRemaining, "unbounded ⇒ remaining is null")
}

// getAutonomousRuntime GETs the channel config and decodes just the
// `autonomous_runtime` readout block.
func getAutonomousRuntime(t *testing.T, srv *Server, id string) autonomousRuntimeResponse {
	t.Helper()
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/"+id+"/config", nil)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
	var envelope struct {
		AutonomousRuntime autonomousRuntimeResponse `json:"autonomous_runtime"`
	}
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &envelope))
	return envelope.AutonomousRuntime
}
