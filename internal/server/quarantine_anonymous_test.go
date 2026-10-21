package server

import (
	"net/http"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/security"
)

// ISSUE-0152 steps 1–2, end to end through Server.Handler(): an outsider
// invents an agent ID and floods a route the agents use until the real rate
// limiter and circuit breaker quarantine it. In either auth mode, that must
// not shut out the callers that send no agent ID — today every REST call the
// Python agents make and every CLI command — and the operator must be able
// to release the ID without inventing one.

// inventedAgentID is the shape the v0.3.0 release checks used to trip a
// quarantine with a curl loop.
const inventedAgentID = "probe-1778578135"

// productionShapedLimiter mirrors cmd/orchestrator's rate-limit violation
// rule (5 in 10 minutes) with a smaller per-ID budget, so the test needs
// fewer requests to trip it. The breaker's audit events go to auditor.
func productionShapedLimiter(t *testing.T, auditor security.AuditLogger) (*security.RateLimiter, *security.CircuitBreaker) {
	t.Helper()
	rl, err := security.NewRateLimiter(security.RateLimitConfig{
		CallsPerWindow: 20,
		WindowSeconds:  60,
		Enabled:        true,
		Logger:         zap.NewNop(),
	})
	require.NoError(t, err)
	cb, err := security.NewCircuitBreaker(security.CircuitBreakerConfig{
		Thresholds: map[security.ViolationType]security.ThresholdRule{
			security.ViolationRateLimit: {Count: 5, Window: 10 * time.Minute},
		},
		ExemptAgentIDs: []string{security.ConsoleAgentID},
		Logger:         zap.NewNop(),
		Auditor:        auditor,
	})
	require.NoError(t, err)
	return rl, cb
}

// floodUntilQuarantined sends requests as id until the breaker opens on it.
func floodUntilQuarantined(t *testing.T, h http.Handler, cb *security.CircuitBreaker, id string) {
	t.Helper()
	for i := 0; i < 100 && !cb.IsQuarantined(id); i++ {
		doRequestWithHeaders(h, http.MethodGet, "/api/v1/channels", nil,
			map[string]string{security.AgentIDHeader: id})
	}
	require.True(t, cb.IsQuarantined(id), "precondition: the flood must quarantine %s", id)
}

func TestInventedIDQuarantineSparesCallersWithNoID(t *testing.T) {
	// The routes the agents call with no agent ID, all open to callers
	// with no account under `enabled` too (auth_policy.go's carve-out).
	agentRoutes := []struct{ method, path string }{
		{http.MethodPost, "/api/v1/agents/register"},
		{http.MethodDelete, "/api/v1/agents/some-agent"},
		{http.MethodGet, "/api/v1/channels"},
		{http.MethodGet, "/api/v1/channels/group:x"},
		{http.MethodGet, "/api/v1/channels/group:x/messages"},
		{http.MethodPost, "/api/v1/channels/group:x/messages"},
		{http.MethodPost, "/api/v1/channels/group:x/convene"},
	}
	modes := []struct {
		name string
		mode string
		// releaseAs returns the bearer token the release call presents:
		// none under `disabled`, an operator session under `enabled`.
		releaseAs func(t *testing.T, h http.Handler) string
		// wantActor is who the release's audit event names: the
		// handler's fallback, or the verified participant.
		wantActor string
	}{
		{"disabled", AuthModeDisabled, func(*testing.T, http.Handler) string { return "" }, "operator"},
		{"enabled", AuthModeEnabled, func(t *testing.T, h http.Handler) string { return bearerFor(t, h, "alice") }, "alice-participant"},
	}
	for _, m := range modes {
		t.Run(m.name, func(t *testing.T) {
			cfg := DefaultAuthConfig()
			cfg.Mode = m.mode
			harness := newEnforcedServer(t, cfg)
			auditor := &recordingAuditor{}
			rl, cb := productionShapedLimiter(t, auditor)
			WithRateLimiter(rl, cb)(harness.srv)
			h := harness.srv.Handler()

			// What each route answers a caller with no ID before any
			// quarantine opens; a quarantine must change none of it.
			before := make([]int, len(agentRoutes))
			for i, rt := range agentRoutes {
				before[i] = request(h, rt.method, rt.path, "").Code
			}

			floodUntilQuarantined(t, h, cb, inventedAgentID)

			for i, rt := range agentRoutes {
				rec := request(h, rt.method, rt.path, "")
				assert.NotContains(t, rec.Body.String(), "QUARANTINE_ACTIVE",
					"%s %s: one invented ID's quarantine must not refuse callers with no ID", rt.method, rt.path)
				assert.Equal(t, before[i], rec.Code,
					"%s %s answers a caller with no ID as it did before the quarantine", rt.method, rt.path)
			}

			// The quarantine itself still holds for the ID it names.
			rec := doRequestWithHeaders(h, http.MethodGet, "/api/v1/channels", nil,
				map[string]string{security.AgentIDHeader: inventedAgentID})
			assert.Equal(t, http.StatusForbidden, rec.Code)
			assert.Contains(t, rec.Body.String(), `"QUARANTINED"`)

			// Step 1: the operator releases it without inventing an ID.
			rec = request(h, http.MethodPost,
				"/api/v1/agents/"+inventedAgentID+"/unquarantine", m.releaseAs(t, h))
			assert.Equal(t, http.StatusNoContent, rec.Code, rec.Body.String())
			assert.False(t, cb.IsQuarantined(inventedAgentID))
			var actors []any
			for _, ev := range auditor.all() {
				if ev.EventType == security.AuditAgentUnquarantined {
					actors = append(actors, ev.Detail["actor"])
				}
			}
			assert.Equal(t, []any{m.wantActor}, actors)
		})
	}
}
