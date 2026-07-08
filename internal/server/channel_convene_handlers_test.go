package server

// channel_convene_handlers_test.go — RFC 0052 §B PR 3: the POST …/convene
// endpoint. Pins the operator-action contract: gated behind the
// config_edit_enabled toggle, 202 + convener ack on an armed channel, 409 on an
// unarmed one, 403 when the toggle is off.

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/registry"
)

// armConvene marks the seeded channel autonomous with `convener` as the opener
// and `bob` as the escalation chair (the §D synthesis author) — an armed channel
// carries a mandatory chair (PR 4a), which the convene pre-flight now re-validates
// (deep-review follow-up), so every convenable-channel test must declare one.
func armConvene(t *testing.T, srv *Server, id, convener string) {
	t.Helper()
	srv.channelRouter.SetAutonomous(id, channels.AutonomousConfig{
		Enabled:  true,
		Convener: convener,
		Topic:    "Should we adopt a monorepo?",
		Goal:     "A synthesized recommendation.",
	})
	srv.channelRouter.SetEscalationChair(id, "bob")
}

func TestConveneHandler_ArmedChannel_Accepted(t *testing.T) {
	// A convenable channel needs an OPEN-FLOOR responder besides the convener:
	// seed bob as `always` explicitly, since an unspecified member defaults to
	// `when_mentioned` (sqlite.go) — which never answers the open-floor opener,
	// so the default alice/bob roster is correctly a no-audience 409.
	srv, id := channelConfigTestServerWithMembers(t, true, []channels.Member{
		{ParticipantID: "alice", RespondPolicy: channels.RespondAlways},
		{ParticipantID: "bob", RespondPolicy: channels.RespondAlways},
	})
	armConvene(t, srv, id, "alice")

	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/"+id+"/convene", nil)
	require.Equal(t, http.StatusAccepted, rec.Code, "body=%s", rec.Body.String())

	var resp struct {
		ChannelID string `json:"channel_id"`
		Convener  string `json:"convener"`
		Status    string `json:"status"`
	}
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, id, resp.ChannelID)
	assert.Equal(t, "alice", resp.Convener)
	assert.Equal(t, "convening", resp.Status)
}

func TestConveneHandler_UnarmedChannel_Conflict(t *testing.T) {
	srv, id := channelConfigTestServer(t, true) // never armed

	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/"+id+"/convene", nil)
	assert.Equal(t, http.StatusConflict, rec.Code, "body=%s", rec.Body.String())
}

func TestConveneHandler_ToggleOff_Forbidden(t *testing.T) {
	srv, id := channelConfigTestServer(t, false) // config_edit_enabled OFF
	armConvene(t, srv, id, "alice")

	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/"+id+"/convene", nil)
	assert.Equal(t, http.StatusForbidden, rec.Code, "body=%s", rec.Body.String())
}

func TestConveneHandler_DriftedConvener_BadRequest(t *testing.T) {
	srv, id := channelConfigTestServer(t, true)
	armConvene(t, srv, id, "ghost") // not a member of the alice/bob roster

	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/"+id+"/convene", nil)
	assert.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())
}

// TestConveneHandler_MissingChannel_NotFound — convening a channel id that does
// not exist reports 404, consistent with GET/PATCH …/config (the deep-review
// fix: it previously fell through AutonomousFor's disabled default and 409'd as
// "not armed", masking a fat-fingered/deleted id).
func TestConveneHandler_MissingChannel_NotFound(t *testing.T) {
	srv, _ := channelConfigTestServer(t, true) // toggle on; convene a different id

	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/group%3Aabsent/convene", nil)
	assert.Equal(t, http.StatusNotFound, rec.Code, "body=%s", rec.Body.String())
}

// refusingDispatcher fails every Dispatch with a fixed error — the
// delivery-miss shapes the real dispatcher returns (grpc_dispatcher.go).
type refusingDispatcher struct{ err error }

func (d refusingDispatcher) Dispatch(context.Context, channels.DispatchEnvelope, channels.ChannelMessage) error {
	return d.err
}

// TestConveneHandler_ConvenerUnreachable_ServiceUnavailable — PR #718 review:
// the dispatcher's delivery-miss returns (a convener not yet re-registered
// after a restart, reported unhealthy, or refusing delivery on queue-full
// backpressure) are routine, retryable conditions the dispatcher documents as
// best-effort — the one synchronous dispatch-returning endpoint must map them
// to 503 UNAVAILABLE, not fall through writeChannelError's default arm as a
// 500 "channel store error" plus an Error-level "unexpected error" log.
func TestConveneHandler_ConvenerUnreachable_ServiceUnavailable(t *testing.T) {
	for name, dispatchErr := range map[string]error{
		"unregistered": fmt.Errorf("dispatch target alice not registered: %w", registry.ErrAgentNotFound),
		"not_ready":    fmt.Errorf("%w: alice status=starting", channels.ErrAgentNotReady),
		"refused_ack":  fmt.Errorf("ReceiveChannelMessage to alice: %w: queue full", channels.ErrDeliveryRefused),
	} {
		t.Run(name, func(t *testing.T) {
			srv, id := channelConfigTestServerWithDispatcher(t, true, []channels.Member{
				{ParticipantID: "alice", RespondPolicy: channels.RespondAlways},
				{ParticipantID: "bob", RespondPolicy: channels.RespondAlways},
			}, refusingDispatcher{err: dispatchErr})
			armConvene(t, srv, id, "alice")

			rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/"+id+"/convene", nil)
			assert.Equal(t, http.StatusServiceUnavailable, rec.Code, "body=%s", rec.Body.String())
			assert.Contains(t, rec.Body.String(), "UNAVAILABLE")
		})
	}
}

// TestConveneHandler_NoTopic_Conflict — an armed channel with a real audience but
// no topic/agenda/goal 409s (the convener would open on an empty directive).
func TestConveneHandler_NoTopic_Conflict(t *testing.T) {
	srv, id := channelConfigTestServerWithMembers(t, true, []channels.Member{
		{ParticipantID: "alice", RespondPolicy: channels.RespondAlways},
		{ParticipantID: "bob", RespondPolicy: channels.RespondAlways},
	})
	// Armed with a valid convener + audience + chair, but deliberately no
	// topic/agenda/goal.
	srv.channelRouter.SetAutonomous(id, channels.AutonomousConfig{Enabled: true, Convener: "alice"})
	srv.channelRouter.SetEscalationChair(id, "bob")

	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/"+id+"/convene", nil)
	assert.Equal(t, http.StatusConflict, rec.Code, "body=%s", rec.Body.String())
}

// TestConveneHandler_DriftedChair_BadRequest — the deep-review fix at the REST
// seam: an armed channel with a valid convener + audience + subject but a chair
// that drifted out of the roster (the mandatory chair cleared/removed after
// arming) 400s rather than convening into a discussion whose close cannot produce
// the §D synthesis artifact. Mirrors the drifted-convener 400.
func TestConveneHandler_DriftedChair_BadRequest(t *testing.T) {
	srv, id := channelConfigTestServerWithMembers(t, true, []channels.Member{
		{ParticipantID: "alice", RespondPolicy: channels.RespondAlways},
		{ParticipantID: "bob", RespondPolicy: channels.RespondAlways},
	})
	// A full, otherwise-convenable config — but the chair names a non-member.
	srv.channelRouter.SetAutonomous(id, channels.AutonomousConfig{
		Enabled: true, Convener: "alice", Topic: "Should we adopt a monorepo?",
	})
	srv.channelRouter.SetEscalationChair(id, "ghost-chair")

	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/"+id+"/convene", nil)
	assert.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())
	assert.Contains(t, rec.Body.String(), "synthesis turn")
}

// TestConveneHandler_ConveningBoundReached_TooManyRequests — RFC 0052 §E PR 7b:
// once a channel has been convened its `autonomous.max_convenings` times, a
// further convene is refused with 429 Too Many Requests (the aggregate quota is
// exhausted — the sibling of the RFC 0030 Layer-2 participant-budget 429), not a
// 400/409. The recording test dispatcher never replies, so no interaction commits
// between the two convenes — the second is refused on the bound, not the
// orthogonal already-convening guard.
func TestConveneHandler_ConveningBoundReached_TooManyRequests(t *testing.T) {
	srv, id := channelConfigTestServerWithMembers(t, true, []channels.Member{
		{ParticipantID: "alice", RespondPolicy: channels.RespondAlways},
		{ParticipantID: "bob", RespondPolicy: channels.RespondAlways},
	})
	srv.channelRouter.SetAutonomous(id, channels.AutonomousConfig{
		Enabled: true, Convener: "alice", Topic: "Weekly review",
		Goal: "A recommendation.", ScheduleIntervalSeconds: 3600, MaxConvenings: 1,
	})
	srv.channelRouter.SetEscalationChair(id, "bob")

	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/"+id+"/convene", nil)
	require.Equal(t, http.StatusAccepted, rec.Code, "1st convening under the bound; body=%s", rec.Body.String())

	rec = doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/"+id+"/convene", nil)
	assert.Equal(t, http.StatusTooManyRequests, rec.Code, "2nd convening exceeds max_convenings; body=%s", rec.Body.String())
	assert.Contains(t, rec.Body.String(), "TOO_MANY_REQUESTS")
}

// TestWriteChannelError_StandingBudgetExhausted_TooManyRequests — RFC 0052 §E
// PR 7b: the aggregate SPEND ceiling (`standing_budget_tokens`) maps to 429 Too
// Many Requests, the sibling of the convening-COUNT ceiling's 429 above.
// Exercised directly on the error mapper because reaching budget exhaustion needs
// folded wallet spend the handler harness cannot drive (convening does not spend;
// folding rides the interaction close). The convene handler routes ConveneChannel
// errors through this mapper — proven by the convening-bound test above — and
// ConveneChannel returns this sentinel once folded spend >= standing_budget_tokens
// (proven in the channels package), so the two together cover the production path.
func TestWriteChannelError_StandingBudgetExhausted_TooManyRequests(t *testing.T) {
	srv, _ := channelConfigTestServer(t, true)
	rec := httptest.NewRecorder()
	srv.writeChannelError(rec, fmt.Errorf("channels: convene c1: %w", channels.ErrAutonomousStandingBudgetExhausted))
	assert.Equal(t, http.StatusTooManyRequests, rec.Code)
	assert.Contains(t, rec.Body.String(), "TOO_MANY_REQUESTS")
}
