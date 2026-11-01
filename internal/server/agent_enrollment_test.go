package server

// agent_enrollment_test.go — RFC 0009 Phases 3–4 plan PR 6: registration
// answers with an agent token, checks the fleet enrollment secret when one is
// presented, and deregistration revokes the agent's tokens.

import (
	"encoding/json"
	"fmt"
	"net/http"
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
	assert.NotContains(t, ev.Detail, "previous_address", "a first registration has no earlier address")
	assert.NotContains(t, ev.Detail, "previous_token")
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
	assert.Equal(t, "revoked", issued[1].Detail["previous_token"],
		"registering again still retires the earlier token")
}

// From PR 5's review, F-6: a registration and a deregistration of one agent
// that race must not leave the agent registered with a token already revoked,
// nor gone with a token that still works.
func TestRegisterAndDeregister_RacingLeaveTokenAndRegistryAgreeing(t *testing.T) {
	srv, _ := testServer(t)
	h := srv.Handler()
	for i := range 200 {
		var (
			wg  sync.WaitGroup
			reg registered
		)
		wg.Add(2)
		go func() {
			defer wg.Done()
			rec := doRequest(h, http.MethodPost, "/api/v1/agents/register",
				registerBody("code-writer", "localhost:50051"))
			if rec.Code == http.StatusCreated {
				_ = json.Unmarshal(rec.Body.Bytes(), &reg)
			}
		}()
		go func() {
			defer wg.Done()
			doRequest(h, http.MethodDelete, "/api/v1/agents/code-writer", nil)
		}()
		wg.Wait()
		require.NotEmpty(t, reg.AgentToken, "iteration %d", i)

		present := doRequest(h, http.MethodGet, "/api/v1/agents/code-writer", nil).Code == http.StatusOK
		_, err := srv.agentTokens.Check(reg.AgentToken)
		if present {
			require.NoError(t, err, "iteration %d: a registered agent's token must work", i)
		} else {
			require.ErrorIs(t, err, security.ErrAgentTokenRevoked,
				"iteration %d: a deregistered agent's token must not work", i)
		}
		doRequest(h, http.MethodDelete, "/api/v1/agents/code-writer", nil)
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
