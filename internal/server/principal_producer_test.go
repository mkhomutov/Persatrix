package server

// ISSUE-0082 Part 2 PR 2 (v0.3.14) — the producer gates.
//
// PR 1's dispatcher tests pinned the RAIL (a ctx principal emits, an absent
// one does not) with the tests as the only callers. These pin the PRODUCER:
// that a real authenticated HTTP request puts the RFC 0039 §F participant on
// the context the dispatcher reads, that it survives the fanout's detach to
// get there, and — the half that is not an improvement but a CONTRACT — that
// an unauthenticated caller and `auth.mode: disabled` change nothing.
//
// The isolation claim itself (A's disclosure unrecallable for B) is
// persona-side and lives in tests/integration/test_principal_emission_isolation.py,
// which drives the same rail through the live gRPC path into real memory.
// What is provable here is the orchestrator half: the right principal, on the
// right dispatches, and on nothing else.

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// recordingPrincipalDispatcher captures the principal visible on each
// dispatch context — the exact value [channels.GRPCMessageDispatcher] would
// have emitted as `persatrix-principal`. Recording the ctx read rather than
// the wire header keeps these tests on the orchestrator side of the boundary;
// the header itself is PR 1's pin, and the live path is the integration
// test's.
type recordingPrincipalDispatcher struct {
	mu   sync.Mutex
	seen []string
}

func (d *recordingPrincipalDispatcher) Dispatch(ctx context.Context, _ channels.DispatchEnvelope, _ channels.ChannelMessage) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.seen = append(d.seen, channels.PrincipalFromContext(ctx))
	return nil
}

func (d *recordingPrincipalDispatcher) snapshot() []string {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]string(nil), d.seen...)
}

// principalHarness is [newEnforcedServer] with the NoopDispatcher swapped for
// a recording one, so a REST publish can be followed all the way to what the
// dispatcher sees. The accounts (alice/operator/`alice-participant`,
// bob/user/`bob-participant`) come from the shared harness.
type principalHarness struct {
	handler    http.Handler
	router     *channels.ChannelRouter
	dispatcher *recordingPrincipalDispatcher
}

func newPrincipalHarness(t *testing.T, cfg *AuthConfig) principalHarness {
	t.Helper()
	logger := zap.NewNop()
	store, err := channels.NewSQLiteStore(filepath.Join(t.TempDir(), "principal-channels.db"),
		channels.SQLiteOptions{MaxChannels: 50, Logger: logger})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	dispatcher := &recordingPrincipalDispatcher{}
	router := channels.NewChannelRouter(store, dispatcher, logger, nil)
	// WithChannels is last-wins on the two Server fields, so passing it as an
	// extra option replaces the harness's Noop-backed pair wholesale.
	h := newEnforcedServer(t, cfg, WithChannels(store, router))
	return principalHarness{handler: h.srv.Handler(), router: router, dispatcher: dispatcher}
}

// seedChannel creates a channel with exactly one respond-always member
// (`agent-bot`), so every publish produces exactly one dispatch and the
// recorded slice reads one entry per turn. `human` and `agent-alice` are
// members only so both a console publish and a persona reply clear the
// sender-must-be-a-member gate; both are RespondNever so neither is ever a
// recipient. Created through the operator session — channel creation is
// policyOperator.
func (h principalHarness) seedChannel(t *testing.T, operatorToken string) {
	t.Helper()
	body, err := json.Marshal(createChannelRequest{
		Name: "planning",
		Members: []channelMemberRequest{
			{ID: "human", Respond: "never"},
			{ID: "agent-alice", Respond: "never"},
			{ID: "agent-bot", Respond: "always"},
		},
	})
	require.NoError(t, err)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/channels", strings.NewReader(string(body)))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+operatorToken)
	rec := httptest.NewRecorder()
	h.handler.ServeHTTP(rec, req)
	require.Equal(t, http.StatusCreated, rec.Code, rec.Body.String())
}

// publishAs posts a channel message, optionally bearing token.
func (h principalHarness) publishAs(t *testing.T, token, sender, content string) *httptest.ResponseRecorder {
	t.Helper()
	body, err := json.Marshal(map[string]any{"sender_id": sender, "content": content})
	require.NoError(t, err)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/channels/group:planning/messages", strings.NewReader(string(body)))
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	h.handler.ServeHTTP(rec, req)
	return rec
}

// TestPrincipalProducer_AuthenticatedPublishReachesDispatch is the headline
// gate: the §F participant of the account that published is what the
// dispatcher sees. It also implicitly pins the detach hop — PublishAsync runs
// fanout on a goroutine holding a `context.WithoutCancel` copy of the request
// ctx, so a principal carried by anything other than a context VALUE would be
// gone by the time this assertion runs.
func TestPrincipalProducer_AuthenticatedPublishReachesDispatch(t *testing.T) {
	h := newPrincipalHarness(t, nil)
	operator := bearerFor(t, h.handler, "alice")
	h.seedChannel(t, operator)

	require.Equal(t, http.StatusCreated, h.publishAs(t, operator, "human", "the quarterly plan").Code)
	h.router.WaitForPendingFanout()

	assert.Equal(t, []string{"alice-participant"}, h.dispatcher.snapshot(),
		"the publishing account's verified participant must reach the dispatch context")
}

// TestPrincipalProducer_TwoAccountsPartitionOneAgent is the multi-user shape
// this release exists for, at the orchestrator layer: one process, one agent,
// one channel, two authenticated people — two DIFFERENT principals on the
// wire. Without this the two turns are indistinguishable downstream and land
// in one tenant, which is precisely the co-mingling ISSUE-0081/0082 close.
func TestPrincipalProducer_TwoAccountsPartitionOneAgent(t *testing.T) {
	h := newPrincipalHarness(t, nil)
	operator := bearerFor(t, h.handler, "alice")
	h.seedChannel(t, operator)

	require.Equal(t, http.StatusCreated, h.publishAs(t, operator, "human", "alice speaks").Code)
	h.router.WaitForPendingFanout()
	user := bearerFor(t, h.handler, "bob")
	require.Equal(t, http.StatusCreated, h.publishAs(t, user, "human", "bob speaks").Code)
	h.router.WaitForPendingFanout()

	assert.Equal(t, []string{"alice-participant", "bob-participant"}, h.dispatcher.snapshot(),
		"two accounts publishing to one agent must dispatch under two distinct principals")
}

// TestPrincipalProducer_UnauthenticatedPublishEmitsNothing pins the declared
// agent-origin contract on the one route where it matters most: publish is
// policyPublic *because* the persona fleet's HTTPChannelPublisher drives it
// (auth_policy.go §Non-Goals), so under `enabled` this route carries both
// human and agent traffic. A persona reply must stay on the `'local'`
// default — stamping the anonymous identity's literal "local" participant as
// an explicit principal would put a header on the wire where there is none
// today, for no change in resolved value.
func TestPrincipalProducer_UnauthenticatedPublishEmitsNothing(t *testing.T) {
	h := newPrincipalHarness(t, nil)
	operator := bearerFor(t, h.handler, "alice")
	h.seedChannel(t, operator)

	require.Equal(t, http.StatusCreated, h.publishAs(t, "", "agent-alice", "a persona reply").Code)
	h.router.WaitForPendingFanout()

	assert.Equal(t, []string{""}, h.dispatcher.snapshot(),
		"an unauthenticated (agent-origin) publish must carry no principal")
}

// TestPrincipalProducer_DisabledModeNoDelta is the release's byte-level
// acceptance criterion: under `auth.mode: disabled` nothing is emitted, even
// for a caller presenting a credential shape. The mode is the whole switch —
// there is no partial activation.
func TestPrincipalProducer_DisabledModeNoDelta(t *testing.T) {
	cfg := DefaultAuthConfig()
	cfg.Mode = AuthModeDisabled
	h := newPrincipalHarness(t, cfg)
	h.seedChannel(t, "") // everything is reachable anonymously under disabled

	require.Equal(t, http.StatusCreated, h.publishAs(t, "irrelevant-token", "human", "hello").Code)
	h.router.WaitForPendingFanout()

	assert.Equal(t, []string{""}, h.dispatcher.snapshot(),
		"under auth.mode: disabled no dispatch may carry a principal")
}

// TestAuthMiddlewareStampsPrincipalForEveryRoute is the structural claim
// principal.go makes, tested where it is made rather than route by route: the
// principal is threaded by the middleware that wraps the root mux, so it is
// on the context of EVERY handler — including
// `POST /api/v1/channels/{id}/convene`, the origin the plan's surface audit
// caught calling `ConveneChannel(r.Context(), …)` outside the publish path.
// Route-by-route dispatch tests could never prove this for a route added
// tomorrow; this does.
func TestAuthMiddlewareStampsPrincipalForEveryRoute(t *testing.T) {
	probe := func(t *testing.T, cfg *AuthConfig, token, path string) string {
		t.Helper()
		h := newEnforcedServer(t, cfg)
		// The login has to go through the real handler chain; the probe is
		// then mounted on the same Server's middleware directly.
		var seen string
		next := http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
			seen = channels.PrincipalFromContext(r.Context())
		})
		if token == "login:alice" {
			token = bearerFor(t, h.srv.Handler(), "alice")
		}
		req := httptest.NewRequest(http.MethodPost, path, strings.NewReader("{}"))
		req.Header.Set("Content-Type", "application/json")
		if token != "" {
			req.Header.Set("Authorization", "Bearer "+token)
		}
		h.srv.authMiddleware(next).ServeHTTP(httptest.NewRecorder(), req)
		return seen
	}

	enabled := func() *AuthConfig { c := DefaultAuthConfig(); c.Mode = AuthModeEnabled; return c }
	disabled := func() *AuthConfig { c := DefaultAuthConfig(); c.Mode = AuthModeDisabled; return c }

	for _, path := range []string{
		"/api/v1/channels/group:planning/messages",
		"/api/v1/channels/group:planning/convene",
		"/api/v1/agents/agent-bot/chat",
		"/api/v1/brand-new-route-nobody-classified-yet",
	} {
		t.Run("authenticated "+path, func(t *testing.T) {
			assert.Equal(t, "alice-participant", probe(t, enabled(), "login:alice", path),
				"every route inherits the principal from the middleware")
		})
		t.Run("anonymous "+path, func(t *testing.T) {
			assert.Empty(t, probe(t, enabled(), "", path),
				"an unauthenticated caller carries no principal on any route")
		})
		t.Run("disabled "+path, func(t *testing.T) {
			assert.Empty(t, probe(t, disabled(), "", path),
				"auth.mode: disabled carries no principal on any route")
		})
	}
}
