package server

// agent_enrollment_test.go — RFC 0009 Phases 3–4 plan PR 6: registration
// answers with an agent token, checks the fleet enrollment secret when one is
// presented, and deregistration revokes the agent's tokens.

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/security"
	"github.com/mkhomutov/persatrix/internal/state"
)

const testEnrollmentSecret = "fleet-s3cret-for-tests"

// registerBody is a registration request for id at address.
func registerBody(id, address string) []byte {
	return []byte(fmt.Sprintf(`{"id": %q, "address": %q}`, id, address))
}

// registered is the part of a registration response these tests read.
type registered struct {
	ID             string    `json:"id"`
	Address        string    `json:"address"`
	AgentToken     string    `json:"agent_token"`
	TokenExpiresAt time.Time `json:"agent_token_expires_at"`
}

// requestFrom serves a request that arrives from remoteAddr, as
// doRequestWithHeaders does from httptest's default 192.0.2.1:1234.
func requestFrom(h http.Handler, method, path string, body []byte, remoteAddr string,
	headers map[string]string,
) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, bytes.NewReader(body))
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	req.RemoteAddr = remoteAddr
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func decodeRegistered(t *testing.T, body string) registered {
	t.Helper()
	var r registered
	require.NoError(t, json.Unmarshal([]byte(body), &r))
	return r
}

func enrollmentServer(t *testing.T, opts ...ServerOption) (*Server, *recordingAuditor) {
	t.Helper()
	auditor := &recordingAuditor{}
	logger := zap.NewNop()
	srv, err := New("127.0.0.1:0", t.TempDir(), state.NewInMemoryStore(logger),
		registry.NewInMemoryRegistry(logger), planner.NewYAMLPlanner(logger), logger,
		append([]ServerOption{WithAuditLogger(auditor)}, opts...)...)
	require.NoError(t, err)
	return srv, auditor
}

func auditsOf(a *recordingAuditor, typ security.AuditEventType) []security.AuditEvent {
	var out []security.AuditEvent
	for _, ev := range a.all() {
		if ev.EventType == typ {
			out = append(out, ev)
		}
	}
	return out
}

// assertTokenFor checks that the response carries a token the server's
// issuer accepts for id, with the expiry the issuer gave it.
func assertTokenFor(t *testing.T, srv *Server, body, id string) registered {
	t.Helper()
	r := decodeRegistered(t, body)
	require.NotEmpty(t, r.AgentToken, "registration must answer with an agent token")
	claims, err := srv.agentTokens.Check(r.AgentToken)
	require.NoError(t, err)
	assert.Equal(t, id, claims.AgentID)
	assert.True(t, claims.ExpiresAt.Equal(r.TokenExpiresAt),
		"agent_token_expires_at must be the token's own expiry")
	assert.Equal(t, id, r.ID, "the agent's fields stay in the response")
	return r
}

func TestRegisterAgent_AnswersWithTokenUnderDisabled(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register",
		registerBody("code-writer", "localhost:50051"))
	require.Equal(t, http.StatusCreated, rec.Code, rec.Body.String())
	assertTokenFor(t, srv, rec.Body.String(), "code-writer")
	assert.Equal(t, "no-store", rec.Header().Get("Cache-Control"),
		"a response carrying a credential must not be cached")
}

func TestRegisterAgent_AnswersWithTokenUnderEnabled(t *testing.T) {
	srv, _ := newAuthServer(t, nil)
	require.True(t, srv.authEnforced())
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register",
		registerBody("code-writer", "localhost:50051"))
	require.Equal(t, http.StatusCreated, rec.Code, rec.Body.String())
	assertTokenFor(t, srv, rec.Body.String(), "code-writer")
}

func TestAgentEndpoints_OtherThanRegisterNeverCarryAToken(t *testing.T) {
	srv, _ := testServer(t)
	h := srv.Handler()
	require.Equal(t, http.StatusCreated, doRequest(h, http.MethodPost, "/api/v1/agents/register",
		registerBody("code-writer", "localhost:50051")).Code)
	for _, path := range []string{"/api/v1/agents", "/api/v1/agents/code-writer"} {
		rec := doRequest(h, http.MethodGet, path, nil)
		require.Equal(t, http.StatusOK, rec.Code)
		assert.NotContains(t, rec.Body.String(), "agent_token", path)
	}
}

func TestRegisterAgent_TokenIssuedIsAudited(t *testing.T) {
	srv, auditor := enrollmentServer(t)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register",
		registerBody("code-writer", "localhost:50051"))
	require.Equal(t, http.StatusCreated, rec.Code)
	r := decodeRegistered(t, rec.Body.String())

	issued := auditsOf(auditor, security.AuditAgentTokenIssued)
	require.Len(t, issued, 1)
	ev := issued[0]
	assert.Equal(t, "code-writer", ev.AgentID)
	assert.Equal(t, "register", ev.Action)
	assert.Equal(t, "code-writer", ev.Resource)
	assert.Equal(t, r.TokenExpiresAt.Format(time.RFC3339), ev.Detail["expires_at"])
	assert.Equal(t, "192.0.2.1", ev.Detail["source"], "the line names where the registration came from")
	assert.NotContains(t, ev.Detail, "previous_address", "a first registration has no earlier address")
	assert.NotContains(t, ev.Detail, "previous_source")
	assert.NotContains(t, ev.Detail, "previous_token")
	assert.NotContains(t, ev.Detail, "enrollment_secret", "with no secret set there is nothing to show")
	assert.NotContains(t, fmt.Sprint(auditor.all()), r.AgentToken, "the token itself is never audited")
}

func TestRegisterAgent_WrongEnrollmentSecretRefusedAndAudited(t *testing.T) {
	srv, auditor := enrollmentServer(t, WithEnrollmentSecret(testEnrollmentSecret))
	h := srv.Handler()
	rec := doRequestWithHeaders(h, http.MethodPost, "/api/v1/agents/register",
		registerBody("code-writer", "localhost:50051"),
		map[string]string{EnrollmentSecretHeader: "not-the-secret"})
	require.Equal(t, http.StatusUnauthorized, rec.Code, rec.Body.String())
	assert.NotContains(t, rec.Body.String(), "agent_token")

	assert.Equal(t, http.StatusNotFound,
		doRequest(h, http.MethodGet, "/api/v1/agents/code-writer", nil).Code,
		"a refused registration registers nothing")
	assert.Empty(t, auditsOf(auditor, security.AuditAgentRegistered))
	assert.Empty(t, auditsOf(auditor, security.AuditAgentTokenIssued))

	refused := auditsOf(auditor, security.AuditAgentTokenInvalid)
	require.Len(t, refused, 1)
	ev := refused[0]
	assert.Equal(t, "code-writer", ev.AgentID)
	assert.Equal(t, "register", ev.Action)
	assert.Equal(t, "refused", ev.Outcome)
	assert.Equal(t, "enrollment_secret", ev.Detail["reason"])
	assert.NotEmpty(t, ev.Detail["source"])
	all := fmt.Sprint(auditor.all())
	assert.NotContains(t, all, "not-the-secret", "the presented secret is never audited")
	assert.NotContains(t, all, testEnrollmentSecret, "the configured secret is never audited")
}

func TestRegisterAgent_RightEnrollmentSecretAdmitted(t *testing.T) {
	srv, auditor := enrollmentServer(t, WithEnrollmentSecret(testEnrollmentSecret))
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPost, "/api/v1/agents/register",
		registerBody("code-writer", "localhost:50051"),
		map[string]string{EnrollmentSecretHeader: testEnrollmentSecret})
	require.Equal(t, http.StatusCreated, rec.Code, rec.Body.String())
	assertTokenFor(t, srv, rec.Body.String(), "code-writer")
	assert.Empty(t, auditsOf(auditor, security.AuditAgentTokenInvalid))
}

// Until PR 7's agents send the secret and PR 9 requires it, a registration
// that presents none is still admitted, so the fleet keeps registering.
func TestRegisterAgent_NoEnrollmentSecretPresentedStillAdmitted(t *testing.T) {
	for _, mode := range []string{"disabled", "enabled"} {
		t.Run(mode, func(t *testing.T) {
			var srv *Server
			if mode == "enabled" {
				srv, _ = newAuthServer(t, nil, WithEnrollmentSecret(testEnrollmentSecret))
			} else {
				srv, _ = enrollmentServer(t, WithEnrollmentSecret(testEnrollmentSecret))
			}
			rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register",
				registerBody("code-writer", "localhost:50051"))
			require.Equal(t, http.StatusCreated, rec.Code, rec.Body.String())
			assertTokenFor(t, srv, rec.Body.String(), "code-writer")
		})
	}
}

// With no secret set on the orchestrator there is nothing to compare a
// presented one with, so it is ignored: a fleet given the secret before its
// orchestrator keeps registering.
func TestRegisterAgent_SecretPresentedWithNoneSetIsIgnored(t *testing.T) {
	srv, auditor := enrollmentServer(t)
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPost, "/api/v1/agents/register",
		registerBody("code-writer", "localhost:50051"),
		map[string]string{EnrollmentSecretHeader: "anything"})
	require.Equal(t, http.StatusCreated, rec.Code, rec.Body.String())
	assert.Empty(t, auditsOf(auditor, security.AuditAgentTokenInvalid))
}

func TestRegisterAgent_RefusesIDsNoTokenCanBeIssuedFor(t *testing.T) {
	long := strings.Repeat("a", security.MaxAgentIDLen+1)
	for _, id := range []string{security.ConsoleAgentID, security.DefaultUnauthenticatedID, long} {
		t.Run(id[:min(len(id), 16)], func(t *testing.T) {
			srv, auditor := enrollmentServer(t)
			h := srv.Handler()
			rec := doRequest(h, http.MethodPost, "/api/v1/agents/register",
				registerBody(id, "localhost:50051"))
			require.Equal(t, http.StatusBadRequest, rec.Code, rec.Body.String())
			list := doRequest(h, http.MethodGet, "/api/v1/agents", nil)
			assert.Equal(t, "[]", strings.TrimSpace(list.Body.String()), "nothing is registered")
			assert.Empty(t, auditsOf(auditor, security.AuditAgentRegistered))
		})
	}
}

func TestDeleteAgent_RevokesItsTokens(t *testing.T) {
	srv, _ := testServer(t)
	h := srv.Handler()
	rec := doRequest(h, http.MethodPost, "/api/v1/agents/register",
		registerBody("code-writer", "localhost:50051"))
	require.Equal(t, http.StatusCreated, rec.Code)
	first := decodeRegistered(t, rec.Body.String())

	require.Equal(t, http.StatusNoContent,
		doRequest(h, http.MethodDelete, "/api/v1/agents/code-writer", nil).Code)
	_, err := srv.agentTokens.Check(first.AgentToken)
	require.ErrorIs(t, err, security.ErrAgentTokenRevoked)

	// A deregistered agent can register again, and its new token works.
	rec = doRequest(h, http.MethodPost, "/api/v1/agents/register",
		registerBody("code-writer", "localhost:50051"))
	require.Equal(t, http.StatusCreated, rec.Code)
	assertTokenFor(t, srv, rec.Body.String(), "code-writer")
}

func TestRegisterAgent_ReregistrationFromNewAddressIsAudited(t *testing.T) {
	srv, auditor := enrollmentServer(t)
	h := srv.Handler()
	rec := doRequest(h, http.MethodPost, "/api/v1/agents/register",
		registerBody("code-writer", "localhost:50051"))
	require.Equal(t, http.StatusCreated, rec.Code)
	first := decodeRegistered(t, rec.Body.String())

	rec = doRequest(h, http.MethodPost, "/api/v1/agents/register",
		registerBody("code-writer", "10.0.0.7:50051"))
	require.Equal(t, http.StatusCreated, rec.Code)
	second := assertTokenFor(t, srv, rec.Body.String(), "code-writer")

	_, err := srv.agentTokens.Check(first.AgentToken)
	require.ErrorIs(t, err, security.ErrAgentTokenRevoked,
		"the old address's token stops working")

	issued := auditsOf(auditor, security.AuditAgentTokenIssued)
	require.Len(t, issued, 2)
	ev := issued[1]
	assert.Equal(t, "10.0.0.7:50051", ev.Detail["address"])
	assert.Equal(t, "localhost:50051", ev.Detail["previous_address"])
	assert.Equal(t, "revoked", ev.Detail["previous_token"])
	assert.NotContains(t, fmt.Sprint(auditor.all()), second.AgentToken)
}

func TestRegisterAgent_ReregistrationFromSameAddressNamesNoNewAddress(t *testing.T) {
	srv, auditor := enrollmentServer(t)
	h := srv.Handler()
	for range 2 {
		require.Equal(t, http.StatusCreated, doRequest(h, http.MethodPost, "/api/v1/agents/register",
			registerBody("code-writer", "localhost:50051")).Code)
	}
	issued := auditsOf(auditor, security.AuditAgentTokenIssued)
	require.Len(t, issued, 2)
	assert.NotContains(t, issued[1].Detail, "previous_address")
	assert.NotContains(t, issued[1].Detail, "previous_source")
	assert.Equal(t, "revoked", issued[1].Detail["previous_token"],
		"registering again still retires the earlier token")
}

// From PR 6's review: the address in the body is the caller's to choose, so
// a live agent's ID taken from elsewhere with its address copied is told
// apart by where the request came from (G-14).
func TestRegisterAgent_ReregistrationFromAnotherSourceIsAudited(t *testing.T) {
	srv, auditor := enrollmentServer(t)
	h := srv.Handler()
	body := registerBody("code-writer", "localhost:50051")
	require.Equal(t, http.StatusCreated,
		requestFrom(h, http.MethodPost, "/api/v1/agents/register", body, "192.0.2.1:1234", nil).Code)
	require.Equal(t, http.StatusCreated,
		requestFrom(h, http.MethodPost, "/api/v1/agents/register", body, "198.51.100.7:4321", nil).Code)

	issued := auditsOf(auditor, security.AuditAgentTokenIssued)
	require.Len(t, issued, 2)
	ev := issued[1]
	assert.Equal(t, "198.51.100.7", ev.Detail["source"])
	assert.Equal(t, "192.0.2.1", ev.Detail["previous_source"])
	assert.NotContains(t, ev.Detail, "previous_address", "the copied address matches")
}

// From PR 6's review: with a secret set, a registration that shows none is
// still admitted, and its audit line says so.
func TestRegisterAgent_AuditSaysWhetherTheSecretWasShown(t *testing.T) {
	srv, auditor := enrollmentServer(t, WithEnrollmentSecret(testEnrollmentSecret))
	h := srv.Handler()
	body := registerBody("code-writer", "localhost:50051")
	require.Equal(t, http.StatusCreated,
		doRequest(h, http.MethodPost, "/api/v1/agents/register", body).Code)
	require.Equal(t, http.StatusCreated, doRequestWithHeaders(h, http.MethodPost, "/api/v1/agents/register",
		body, map[string]string{EnrollmentSecretHeader: testEnrollmentSecret}).Code)

	issued := auditsOf(auditor, security.AuditAgentTokenIssued)
	require.Len(t, issued, 2)
	assert.Equal(t, "absent", issued[0].Detail["enrollment_secret"])
	assert.Equal(t, "shown", issued[1].Detail["enrollment_secret"])
}

// From PR 6's review: registrations are counted by where they come from,
// not by the X-Agent-ID a caller picks, so guessing the secret, or forcing
// the audit log to flush, stops at the limit.
func TestRegisterAgent_RegistrationsPerSourceAreLimited(t *testing.T) {
	srv, auditor := enrollmentServer(t, WithEnrollmentSecret(testEnrollmentSecret))
	h := srv.Handler()
	body := registerBody("code-writer", "localhost:50051")
	guess := func(i int) map[string]string {
		return map[string]string{EnrollmentSecretHeader: fmt.Sprintf("guess-%d", i), "X-Agent-ID": fmt.Sprintf("caller-%d", i)}
	}
	for i := range registerPerSourceCalls {
		require.Equal(t, http.StatusUnauthorized,
			requestFrom(h, http.MethodPost, "/api/v1/agents/register", body, "192.0.2.1:1234", guess(i)).Code)
	}
	rec := requestFrom(h, http.MethodPost, "/api/v1/agents/register", body, "192.0.2.1:1234",
		map[string]string{EnrollmentSecretHeader: testEnrollmentSecret})
	require.Equal(t, http.StatusTooManyRequests, rec.Code, "even the right secret waits once the limit is reached")
	assert.Equal(t, strconv.Itoa(registerPerSourceWindowSeconds), rec.Header().Get("Retry-After"))
	assert.Len(t, auditsOf(auditor, security.AuditAgentTokenInvalid), registerPerSourceCalls,
		"a refused request over the limit writes no line of its own")
	assert.Empty(t, auditsOf(auditor, security.AuditAgentTokenIssued))

	require.Equal(t, http.StatusCreated, requestFrom(h, http.MethodPost, "/api/v1/agents/register", body,
		"198.51.100.7:4321", map[string]string{EnrollmentSecretHeader: testEnrollmentSecret}).Code,
		"another source has its own budget")
}

// From PR 6's review: anyone can deregister an agent, which revokes its
// tokens, so the audit chain records each one and where it came from.
func TestDeleteAgent_IsAudited(t *testing.T) {
	srv, auditor := enrollmentServer(t)
	h := srv.Handler()
	require.Equal(t, http.StatusCreated, doRequest(h, http.MethodPost, "/api/v1/agents/register",
		registerBody("code-writer", "localhost:50051")).Code)
	require.Equal(t, http.StatusNoContent,
		requestFrom(h, http.MethodDelete, "/api/v1/agents/code-writer", nil, "198.51.100.7:4321", nil).Code)
	require.Equal(t, http.StatusNotFound,
		doRequest(h, http.MethodDelete, "/api/v1/agents/code-writer", nil).Code)

	gone := auditsOf(auditor, security.AuditAgentDeregistered)
	require.Len(t, gone, 1, "a deregistration that removes nothing writes no line")
	ev := gone[0]
	assert.Equal(t, "code-writer", ev.AgentID)
	assert.Equal(t, "deregister", ev.Action)
	assert.Equal(t, "code-writer", ev.Resource)
	assert.Equal(t, "revoked", ev.Outcome)
	assert.Equal(t, "198.51.100.7", ev.Detail["source"])
}

// stallingRegistry holds its first registration after the registry write,
// before the token is issued, until a deregistration has removed the agent
// or a short wait has passed. Without one lock over both steps, the
// deregistration then revokes the agent's tokens before the new one is
// issued, so the agent is gone with a token that still works.
type stallingRegistry struct {
	registry.Registry
	registered, unregistered chan struct{}
	stallOnce, signalOnce    sync.Once
}

func newStallingRegistry() *stallingRegistry {
	return &stallingRegistry{
		Registry:     registry.NewInMemoryRegistry(zap.NewNop()),
		registered:   make(chan struct{}),
		unregistered: make(chan struct{}),
	}
}

func (r *stallingRegistry) Register(ctx context.Context, info registry.AgentInfo) error {
	if err := r.Registry.Register(ctx, info); err != nil {
		return err
	}
	r.stallOnce.Do(func() {
		close(r.registered)
		select {
		case <-r.unregistered:
			time.Sleep(20 * time.Millisecond) // the revocation runs meanwhile
		case <-time.After(100 * time.Millisecond): // the lock keeps it out
		}
	})
	return nil
}

func (r *stallingRegistry) Unregister(ctx context.Context, id string) error {
	err := r.Registry.Unregister(ctx, id)
	r.signalOnce.Do(func() { close(r.unregistered) })
	return err
}

// From PR 5's review, F-6: a registration and a deregistration of one agent
// that race must not leave the agent registered with a token already revoked,
// nor gone with a token that still works. The stalling registry puts the
// deregistration between the registration's two steps whenever it can.
func TestRegisterAndDeregister_RacingLeaveTokenAndRegistryAgreeing(t *testing.T) {
	stall := newStallingRegistry()
	srv := testServerWithRegistry(t, stall)
	h := srv.Handler()
	done := make(chan registered)
	go func() {
		var out registered
		rec := doRequest(h, http.MethodPost, "/api/v1/agents/register",
			registerBody("code-writer", "localhost:50051"))
		_ = json.Unmarshal(rec.Body.Bytes(), &out)
		done <- out
	}()
	<-stall.registered
	doRequest(h, http.MethodDelete, "/api/v1/agents/code-writer", nil)
	reg := <-done
	require.NotEmpty(t, reg.AgentToken)

	present := doRequest(h, http.MethodGet, "/api/v1/agents/code-writer", nil).Code == http.StatusOK
	_, err := srv.agentTokens.Check(reg.AgentToken)
	if present {
		require.NoError(t, err, "a registered agent's token must work")
	} else {
		require.ErrorIs(t, err, security.ErrAgentTokenRevoked, "a deregistered agent's token must not work")
	}
}

func TestWithAgentTokenIssuer_IsTheOneRegistrationUses(t *testing.T) {
	issuer := security.NewAgentTokenIssuer()
	srv, _ := enrollmentServer(t, WithAgentTokenIssuer(issuer))
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register",
		registerBody("code-writer", "localhost:50051"))
	require.Equal(t, http.StatusCreated, rec.Code)
	_, err := issuer.Check(decodeRegistered(t, rec.Body.String()).AgentToken)
	require.NoError(t, err, "the injected issuer signs the tokens, so PR 10's gRPC check can share it")
}

// From PR 6's review: the orchestrator makes one enrollment and hands it to
// every server that registers agents, so they all take the same lock.
func TestWithAgentEnrollment_IsTheOneRegistrationUses(t *testing.T) {
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	enrollment := security.NewAgentEnrollment(reg, security.NewAgentTokenIssuer())
	srv, err := New("127.0.0.1:0", t.TempDir(), state.NewInMemoryStore(zap.NewNop()), reg,
		planner.NewYAMLPlanner(zap.NewNop()), zap.NewNop(), WithAgentEnrollment(enrollment))
	require.NoError(t, err)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register",
		registerBody("code-writer", "localhost:50051"))
	require.Equal(t, http.StatusCreated, rec.Code)
	_, err = enrollment.Issuer().Check(decodeRegistered(t, rec.Body.String()).AgentToken)
	require.NoError(t, err, "the enrollment's issuer signs the tokens")
	require.NoError(t, enrollment.Deregister(context.Background(), "code-writer"))
	assert.Equal(t, http.StatusNotFound,
		doRequest(srv.Handler(), http.MethodGet, "/api/v1/agents/code-writer", nil).Code,
		"the enrollment and the server share one registry")
}
