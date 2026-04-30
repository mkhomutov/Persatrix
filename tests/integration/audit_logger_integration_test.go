package integration

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap/zaptest"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"

	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/security"
	"github.com/mkhomutov/persatrix/internal/server"
	"github.com/mkhomutov/persatrix/internal/state"
)

// auditEvent is a partial decoder for JSONL audit records — we only assert on
// fields the wiring touches. Keeping the test struct minimal also keeps it
// resilient to additive AuditEvent schema changes.
type auditEvent struct {
	EventType string         `json:"event_type"`
	AgentID   string         `json:"agent_id"`
	Action    string         `json:"action"`
	Resource  string         `json:"resource"`
	Detail    map[string]any `json:"detail,omitempty"`
}

// readAuditEvents drains every JSONL line from path and returns them parsed.
// The file may grow concurrently; the helper is intended to be called after
// the producing operation has flushed (security-class events flush
// synchronously, telemetry-class events flush at the next batch boundary or
// on Close — all integration tests below force a Close before reading).
func readAuditEvents(t *testing.T, path string) []auditEvent {
	t.Helper()
	raw, err := os.ReadFile(path)
	require.NoError(t, err)
	var out []auditEvent
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		if line == "" {
			continue
		}
		var ev auditEvent
		require.NoError(t, json.Unmarshal([]byte(line), &ev), "line: %s", line)
		out = append(out, ev)
	}
	return out
}

// findEvents filters a slice by event_type — handy for asserting on the
// presence of a specific lifecycle marker among the chain-bootstrap and
// other infrastructure events the logger emits at construction time.
func findEvents(events []auditEvent, eventType string) []auditEvent {
	var out []auditEvent
	for _, ev := range events {
		if ev.EventType == eventType {
			out = append(out, ev)
		}
	}
	return out
}

// newAuditLogger constructs a file-backed audit logger under t.TempDir() so
// every test gets an isolated chain. Returns the logger and the resolved
// path so assertions can read the JSONL after Close.
func newAuditLogger(t *testing.T) (security.AuditLogger, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "audit.jsonl")
	auditor, err := security.NewFileAuditLogger(path)
	require.NoError(t, err)
	return auditor, path
}

// TestAuditLogger_AgentRegistrationEmitsAgentRegistered verifies the wiring
// path documented in RFC 0009 PR 1b: a successful POST /api/v1/agents/register
// produces an `agent.registered` audit record carrying the registered
// capabilities in Detail.
func TestAuditLogger_AgentRegistrationEmitsAgentRegistered(t *testing.T) {
	logger := zaptest.NewLogger(t)
	auditor, auditPath := newAuditLogger(t)
	t.Cleanup(func() { _ = auditor.Close() })

	store := state.NewInMemoryStore(logger)
	reg := registry.NewInMemoryRegistry(logger)
	pl := planner.NewYAMLPlanner(logger)

	// Workflows dir must exist for server.New() — point at the real one.
	workflowsDir := filepath.Join("..", "..", "workflows")
	abs, err := filepath.Abs(workflowsDir)
	require.NoError(t, err)

	srv, err := server.New("127.0.0.1:0", abs, store, reg, pl, logger,
		server.WithAuditLogger(auditor),
	)
	require.NoError(t, err)

	ts := httptest.NewServer(srv.Handler())
	t.Cleanup(ts.Close)

	body := `{"id":"audit-test-agent","name":"Audit Test","address":"localhost:50099","capabilities":["planning","code_generation"]}`
	resp, err := http.Post(ts.URL+"/api/v1/agents/register", "application/json", strings.NewReader(body))
	require.NoError(t, err)
	defer resp.Body.Close()
	require.Equal(t, http.StatusCreated, resp.StatusCode)

	// Force flush — agent.registered is telemetry-class, so it sits in the
	// 64-event / 250 ms batch until close.
	require.NoError(t, auditor.Close())

	events := readAuditEvents(t, auditPath)
	registered := findEvents(events, "agent.registered")
	require.Len(t, registered, 1, "expected exactly one agent.registered event")

	ev := registered[0]
	assert.Equal(t, "audit-test-agent", ev.AgentID)
	assert.Equal(t, "register", ev.Action)
	// PR #234 review N-1: Resource is the agent ID (forensic anchor),
	// the rotating address moved to Detail["address"].
	assert.Equal(t, "audit-test-agent", ev.Resource)
	require.NotNil(t, ev.Detail)
	assert.Equal(t, "localhost:50099", ev.Detail["address"])
	caps, ok := ev.Detail["capabilities"].([]any)
	require.True(t, ok, "capabilities should be a JSON array")
	assert.Equal(t, []any{"planning", "code_generation"}, caps)
}

// TestAuditLogger_CapabilityViolationOnMalformedName verifies the
// boundary-validation path: a registration carrying a capability that does
// not match the documented charset is rejected at the handler with HTTP 400
// AND emits a `capability.violation` security-class audit event (so the
// failed registration is forensically observable independent of the HTTP
// access log).
func TestAuditLogger_CapabilityViolationOnMalformedName(t *testing.T) {
	logger := zaptest.NewLogger(t)
	auditor, auditPath := newAuditLogger(t)
	t.Cleanup(func() { _ = auditor.Close() })

	store := state.NewInMemoryStore(logger)
	reg := registry.NewInMemoryRegistry(logger)
	pl := planner.NewYAMLPlanner(logger)

	abs, err := filepath.Abs(filepath.Join("..", "..", "workflows"))
	require.NoError(t, err)
	srv, err := server.New("127.0.0.1:0", abs, store, reg, pl, logger,
		server.WithAuditLogger(auditor),
	)
	require.NoError(t, err)
	ts := httptest.NewServer(srv.Handler())
	t.Cleanup(ts.Close)

	// Capability with control character + uppercase + length-overflow class.
	body := `{"id":"bad-cap","name":"Bad","address":"localhost:1","capabilities":["BAD CAP"]}`
	resp, err := http.Post(ts.URL+"/api/v1/agents/register", "application/json", strings.NewReader(body))
	require.NoError(t, err)
	defer resp.Body.Close()
	require.Equal(t, http.StatusBadRequest, resp.StatusCode)

	// capability.violation is security-class, so it is fsync'd before Emit
	// returns — no Close needed for this assertion. We Close anyway to keep
	// the helper symmetrical with the other test.
	require.NoError(t, auditor.Close())

	events := readAuditEvents(t, auditPath)
	violations := findEvents(events, "capability.violation")
	require.Len(t, violations, 1)
	assert.Equal(t, "bad-cap", violations[0].AgentID)
	assert.Equal(t, "BAD CAP", violations[0].Detail["capability"])

	// And no agent.registered for the rejected request.
	assert.Empty(t, findEvents(events, "agent.registered"))
}

// TestAuditLogger_CapabilityViolationName_Truncated locks in the PR #234
// review N-2 fix: a capability name longer than maxCapabilityEchoLen (256)
// must be truncated in the audit event Detail with truncated:true and
// original_length:N markers, bounding the worst-case audit-write size from
// a single registration to maxCapabilitiesPerAgent * maxCapabilityEchoLen
// rather than maxCapabilitiesPerAgent * client_max_body_size. Without this
// regression test, a future refactor that drops the truncation branch
// would silently pass CI (the existing malformed-name test uses a 7-char
// "BAD CAP" payload that never exercises the >256-char path).
func TestAuditLogger_CapabilityViolationName_Truncated(t *testing.T) {
	logger := zaptest.NewLogger(t)
	auditor, auditPath := newAuditLogger(t)
	t.Cleanup(func() { _ = auditor.Close() })

	store := state.NewInMemoryStore(logger)
	reg := registry.NewInMemoryRegistry(logger)
	pl := planner.NewYAMLPlanner(logger)

	abs, err := filepath.Abs(filepath.Join("..", "..", "workflows"))
	require.NoError(t, err)
	srv, err := server.New("127.0.0.1:0", abs, store, reg, pl, logger,
		server.WithAuditLogger(auditor),
	)
	require.NoError(t, err)
	ts := httptest.NewServer(srv.Handler())
	t.Cleanup(ts.Close)

	// 300-char capability — well over the 64-char regex cap (so it fails
	// validation and lands in the violation path) and over the 256-char
	// echo cap (so it triggers the truncation branch). Charset is
	// regex-legal so the rejection is purely on length.
	const oversizedLen = 300
	oversized := strings.Repeat("a", oversizedLen)
	body := `{"id":"trunc-test","name":"Trunc","address":"localhost:1","capabilities":["` + oversized + `"]}`
	resp, err := http.Post(ts.URL+"/api/v1/agents/register", "application/json", strings.NewReader(body))
	require.NoError(t, err)
	defer resp.Body.Close()
	require.Equal(t, http.StatusBadRequest, resp.StatusCode)

	// capability.violation is security-class — fsync'd before Emit returns.
	require.NoError(t, auditor.Close())

	events := readAuditEvents(t, auditPath)
	violations := findEvents(events, "capability.violation")
	require.Len(t, violations, 1)

	ev := violations[0]
	assert.Equal(t, "trunc-test", ev.AgentID)
	assert.Equal(t, "format", ev.Detail["reason"])
	assert.Equal(t, true, ev.Detail["truncated"], "truncated marker must be true for >256-char names")
	assert.EqualValues(t, oversizedLen, ev.Detail["original_length"], "original_length must record pre-truncation length")
	echoed, ok := ev.Detail["capability"].(string)
	require.True(t, ok, "capability detail must be a string")
	assert.Len(t, echoed, 256, "echoed capability must be truncated to maxCapabilityEchoLen")

	// And no agent.registered for the rejected request.
	assert.Empty(t, findEvents(events, "agent.registered"))
}

// TestAuditLogger_RedactsBearerTokenInDetail verifies the default-redactor
// install (PR #233 review Should-Fix #3): a caller embedding a secret in
// AuditEvent.Detail must see it scrubbed even when no explicit
// WithRedactor option is passed. This is the regression test for the prior
// nil-default behaviour that silently shipped plaintext secrets.
func TestAuditLogger_RedactsBearerTokenInDetail(t *testing.T) {
	auditor, auditPath := newAuditLogger(t)
	defer auditor.Close()

	require.NoError(t, auditor.Emit(context.Background(), security.AuditEvent{
		EventType: security.AuditAgentRegistered,
		AgentID:   "redact-test",
		Action:    "register",
		Resource:  "https://example.invalid/?token=sk-ant-AbCdEfGhIjKlMnOpQrStUv",
		Detail: map[string]any{
			"auth": "Bearer sk-ant-AbCdEfGhIjKlMnOpQrStUv",
		},
	}))
	require.NoError(t, auditor.Close())

	raw, err := os.ReadFile(auditPath)
	require.NoError(t, err)
	body := string(raw)

	assert.NotContains(t, body, "sk-ant-AbCdEfGhIjKlMnOpQrStUv",
		"default redactor must scrub anthropic-style API keys without explicit WithRedactor")
	assert.Contains(t, body, "[REDACTED:")
}

// TestAuditLogger_ToolInvokedOnDispatch verifies that the executor emits
// `tool.invoked` (telemetry-class) on every successful gRPC dispatch. The
// event carries workflow / step IDs in Detail so the audit chain links to
// the workflow run audit trail (RFC 0009 §G correlation contract).
func TestAuditLogger_ToolInvokedOnDispatch(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	logger := zaptest.NewLogger(t)
	auditor, auditPath := newAuditLogger(t)
	t.Cleanup(func() { _ = auditor.Close() })

	reg := registry.NewInMemoryRegistry(logger)

	lis := bufconn.Listen(bufSize)
	srv := grpc.NewServer()
	taskpb.RegisterAgentServiceServer(srv, &mockAgentServer{})
	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(func() { srv.GracefulStop(); lis.Close() })

	exec := executor.NewGRPCExecutor(reg, logger,
		executor.WithDialOptions(
			grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
				return lis.DialContext(ctx)
			}),
			grpc.WithTransportCredentials(insecure.NewCredentials()),
		),
		executor.WithTimeout(2*time.Second),
		executor.WithMaxRetries(0),
		executor.WithAuditLogger(auditor),
	)
	defer exec.Close() //nolint:errcheck

	require.NoError(t, reg.Register(ctx, registry.AgentInfo{
		ID:      "audit-exec",
		Name:    "audit-exec",
		Address: "passthrough:///bufconn",
		Status:  registry.StatusHealthy,
	}))

	_, err := exec.ExecuteTask(ctx, executor.ExecuteRequest{
		TaskID:      "task-1",
		WorkflowID:  "wf-1",
		ExecutionID: "exec-1",
		StepID:      "step-1",
		AgentID:     "audit-exec",
		Payload:     "noop",
	})
	require.NoError(t, err)

	// tool.invoked is telemetry-class — flush before reading.
	require.NoError(t, auditor.Close())

	events := readAuditEvents(t, auditPath)
	tools := findEvents(events, "tool.invoked")
	require.Len(t, tools, 1)
	ev := tools[0]
	assert.Equal(t, "audit-exec", ev.AgentID)
	assert.Equal(t, "execute_task", ev.Action)
	assert.Equal(t, "wf-1", ev.Detail["workflow_id"])
	assert.Equal(t, "step-1", ev.Detail["step_id"])
}

// TestAuditLogger_ExcessCapabilitiesEmitsSingleTooManyEvent locks in the
// PR #234 review M-4 fix: a registration carrying more than
// `maxCapabilitiesPerAgent` (64) entries must short-circuit at the handler
// boundary and emit exactly ONE `capability.violation` with
// reason="too_many" — not one per entry. The original wiring iterated
// `validateCapabilities` over every entry, fanning a single hostile
// request into N synchronous fsyncs serialised under the audit logger
// mutex (DoS amplification bypassing per-request HTTP timeouts because
// the work lives inside the audit logger, not the handler). The cap +
// summary emit bounds the worst case to a single fsync regardless of
// caller-controlled slice length.
func TestAuditLogger_ExcessCapabilitiesEmitsSingleTooManyEvent(t *testing.T) {
	logger := zaptest.NewLogger(t)
	auditor, auditPath := newAuditLogger(t)
	t.Cleanup(func() { _ = auditor.Close() })

	store := state.NewInMemoryStore(logger)
	reg := registry.NewInMemoryRegistry(logger)
	pl := planner.NewYAMLPlanner(logger)

	abs, err := filepath.Abs(filepath.Join("..", "..", "workflows"))
	require.NoError(t, err)
	srv, err := server.New("127.0.0.1:0", abs, store, reg, pl, logger,
		server.WithAuditLogger(auditor),
	)
	require.NoError(t, err)
	ts := httptest.NewServer(srv.Handler())
	t.Cleanup(ts.Close)

	// Build a request with 200 well-formed-looking capability names — well
	// over the 64-entry cap. Names are valid against capabilityNameRegex so
	// the rejection is purely on count, not shape.
	const overCount = 200
	caps := make([]string, overCount)
	for i := range caps {
		caps[i] = "cap-" + strconv.Itoa(i)
	}
	payload, err := json.Marshal(map[string]any{
		"id":           "dos-amp",
		"name":         "DoS Amp",
		"address":      "localhost:50101",
		"capabilities": caps,
	})
	require.NoError(t, err)

	resp, err := http.Post(ts.URL+"/api/v1/agents/register", "application/json", strings.NewReader(string(payload)))
	require.NoError(t, err)
	defer resp.Body.Close()
	require.Equal(t, http.StatusBadRequest, resp.StatusCode)

	// capability.violation is security-class — fsync'd before Emit returns.
	// Close to keep the helper symmetrical with sibling tests.
	require.NoError(t, auditor.Close())

	events := readAuditEvents(t, auditPath)
	violations := findEvents(events, "capability.violation")
	require.Len(t, violations, 1, "M-4 regression: hostile registration must produce ONE summary event, not one per capability")

	ev := violations[0]
	assert.Equal(t, "dos-amp", ev.AgentID)
	assert.Equal(t, "register", ev.Action)
	assert.Equal(t, "capability", ev.Resource)
	assert.Equal(t, "too_many", ev.Detail["reason"])
	// JSON numeric decode → float64
	assert.EqualValues(t, overCount, ev.Detail["count"])
	assert.EqualValues(t, 64, ev.Detail["limit"])

	// And no agent.registered for the rejected request.
	assert.Empty(t, findEvents(events, "agent.registered"))
}
