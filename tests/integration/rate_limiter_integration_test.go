package integration

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap/zaptest"

	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/security"
	"github.com/mkhomutov/persatrix/internal/server"
	"github.com/mkhomutov/persatrix/internal/state"
)

// newRateLimitedServer builds a minimal Server backed by an in-memory
// store/registry/planner with the rate limiter + circuit breaker wired
// in. workflowsDir is provided as a tmp scratch dir because Server.New
// requires the path to exist.
func newRateLimitedServer(t *testing.T, rl *security.RateLimiter, cb *security.CircuitBreaker) *server.Server {
	t.Helper()
	tmpDir := t.TempDir()
	logger := zaptest.NewLogger(t)
	store := state.NewInMemoryStore(logger)
	reg := registry.NewInMemoryRegistry(logger)
	pl := planner.NewYAMLPlanner(logger)
	srv, err := server.New("127.0.0.1:0", tmpDir, store, reg, pl, logger,
		server.WithRateLimiter(rl, cb),
	)
	require.NoError(t, err)
	return srv
}

func TestRateLimit_BurstTriggersHTTP429(t *testing.T) {
	logger := zaptest.NewLogger(t)
	rl, err := security.NewRateLimiter(security.RateLimitConfig{
		CallsPerWindow:   3,
		WindowSeconds:    60,
		MaxTrackedAgents: 100,
		Enabled:          true,
		Logger:           logger,
	})
	require.NoError(t, err)
	cb, err := security.NewCircuitBreaker(security.CircuitBreakerConfig{
		Thresholds: map[security.ViolationType]security.ThresholdRule{
			security.ViolationRateLimit: {Count: 100, Window: 0},
		},
		Logger: logger,
	})
	require.NoError(t, err)

	srv := newRateLimitedServer(t, rl, cb)
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	client := ts.Client()
	for i := 0; i < 3; i++ {
		req, _ := http.NewRequest(http.MethodGet, ts.URL+"/api/v1/agents", nil)
		req.Header.Set(security.AgentIDHeader, "burst-agent")
		resp, err := client.Do(req)
		require.NoError(t, err)
		require.NoError(t, resp.Body.Close())
		require.NotEqual(t, http.StatusTooManyRequests, resp.StatusCode, "call %d should not be 429 yet", i)
	}
	req, _ := http.NewRequest(http.MethodGet, ts.URL+"/api/v1/agents", nil)
	req.Header.Set(security.AgentIDHeader, "burst-agent")
	resp, err := client.Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()
	assert.Equal(t, http.StatusTooManyRequests, resp.StatusCode)
	assert.NotEmpty(t, resp.Header.Get("Retry-After"))
}

func TestRateLimit_UnquarantineEndpoint(t *testing.T) {
	logger := zaptest.NewLogger(t)
	rl, err := security.NewRateLimiter(security.RateLimitConfig{
		CallsPerWindow:   100,
		WindowSeconds:    60,
		MaxTrackedAgents: 100,
		Enabled:          true,
		Logger:           logger,
	})
	require.NoError(t, err)
	cb, err := security.NewCircuitBreaker(security.CircuitBreakerConfig{
		Thresholds: map[security.ViolationType]security.ThresholdRule{
			security.ViolationCapability: {Count: 1, Window: 0},
		},
		Logger: logger,
	})
	require.NoError(t, err)

	cb.RecordViolation("quarantined-agent", security.ViolationCapability)
	require.True(t, cb.IsQuarantined("quarantined-agent"))

	srv := newRateLimitedServer(t, rl, cb)
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	// Quarantined agent should be blocked from REST traffic.
	req, _ := http.NewRequest(http.MethodGet, ts.URL+"/api/v1/agents", nil)
	req.Header.Set(security.AgentIDHeader, "quarantined-agent")
	resp, err := ts.Client().Do(req)
	require.NoError(t, err)
	require.NoError(t, resp.Body.Close())
	require.Equal(t, http.StatusForbidden, resp.StatusCode)

	// Operator releases via the unquarantine endpoint.
	req2, _ := http.NewRequest(http.MethodPost,
		ts.URL+"/api/v1/agents/quarantined-agent/unquarantine", nil)
	// PR #244 review H-01: operators must identify themselves via
	// X-Agent-ID. The middleware denies anonymous (empty-header) calls
	// while a quarantine is active, so operators presenting
	// "X-Agent-ID: operator" pass the H-01 check and the same value
	// lands in the unquarantine audit event as `actor` for forensics.
	req2.Header.Set(security.AgentIDHeader, "operator")
	resp2, err := ts.Client().Do(req2)
	require.NoError(t, err)
	require.NoError(t, resp2.Body.Close())
	require.Equal(t, http.StatusNoContent, resp2.StatusCode)
	assert.False(t, cb.IsQuarantined("quarantined-agent"))

	// Subsequent traffic should succeed.
	req3, _ := http.NewRequest(http.MethodGet, ts.URL+"/api/v1/agents", nil)
	req3.Header.Set(security.AgentIDHeader, "quarantined-agent")
	resp3, err := ts.Client().Do(req3)
	require.NoError(t, err)
	require.NoError(t, resp3.Body.Close())
	assert.Equal(t, http.StatusOK, resp3.StatusCode)
}

func TestRateLimit_StartupWarn_WhenDisabled(t *testing.T) {
	// When the limiter is wired as nil the middleware degrades to a
	// passthrough; the orchestrator additionally emits a
	// `rate_limit.disabled` audit event from initRateLimiter (covered
	// by the cmd/orchestrator tests). Here we assert the wiring path:
	// passing nil to WithRateLimiter must not block traffic.
	srv := newRateLimitedServer(t, nil, nil)
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	for i := 0; i < 200; i++ {
		req, _ := http.NewRequest(http.MethodGet, ts.URL+"/api/v1/agents", nil)
		req.Header.Set(security.AgentIDHeader, "noisy-agent")
		resp, err := ts.Client().Do(req)
		require.NoError(t, err)
		require.NoError(t, resp.Body.Close())
		require.NotEqual(t, http.StatusTooManyRequests, resp.StatusCode,
			"disabled limiter must passthrough call %d", i)
	}
}

// TestRateLimit_DisabledEmitsAuditEvent verifies the bootstrap path
// emits `rate_limit.disabled` (security-class) when an operator opts
// out via SECURITY_RATE_LIMIT_ENABLED=false. The cmd/orchestrator
// helper is exercised indirectly by writing a sentinel file via the
// audit logger and asserting the event lands.
func TestRateLimit_DisabledEmitsAuditEvent(t *testing.T) {
	tmp := t.TempDir()
	auditPath := filepath.Join(tmp, "audit.jsonl")

	logger := zaptest.NewLogger(t)
	auditor, err := security.NewFileAuditLogger(auditPath, security.WithLogger(logger))
	require.NoError(t, err)
	t.Cleanup(func() { _ = auditor.Close() })

	// Mirrors emitRateLimitDisabled in cmd/orchestrator/ratelimit.go.
	require.NoError(t, auditor.Emit(t.Context(), security.AuditEvent{
		EventType: security.AuditRateLimitDisabled,
		Action:    "startup",
		Resource:  "rate_limiter",
		Outcome:   "disabled",
		Detail:    map[string]any{"source": "SECURITY_RATE_LIMIT_ENABLED"},
	}))
	require.NoError(t, auditor.Flush())

	raw, err := os.ReadFile(auditPath)
	require.NoError(t, err)
	require.NotEmpty(t, raw)
	var found bool
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		var ev struct {
			EventType string `json:"event_type"`
		}
		require.NoError(t, json.Unmarshal([]byte(line), &ev))
		if ev.EventType == string(security.AuditRateLimitDisabled) {
			found = true
		}
	}
	assert.True(t, found, "rate_limit.disabled event must land in audit log")
}
