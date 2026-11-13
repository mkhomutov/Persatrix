package server

// RFC 0009 Phases 3–4 plan PR 8 (design points G-5 and G-7): under
// `auth.mode: enabled` a REST call's X-Agent-Token proves which agent is
// calling. A token that is not good, or that names another agent than the
// call's X-Agent-ID, is refused with 401 and an agent.token_invalid audit
// line, and the 401's code says why, which the agents read
// (agents/orchestrator_session.py). Under `disabled` no token is checked.

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/security"
)

// tokenHarness is [newEnforcedServer] with an auditor, and the handler built.
type tokenHarness struct {
	enforcedHarness
	h       http.Handler
	auditor *recordingAuditor
}

// newTokenHarness builds a server in mode with a channel, group:planning,
// whose members are agent-alice, agent-bot and both accounts' participants.
// None is ever dispatched to.
func newTokenHarness(t *testing.T, mode string, opts ...ServerOption) tokenHarness {
	t.Helper()
	cfg := DefaultAuthConfig()
	cfg.Mode = mode
	auditor := &recordingAuditor{}
	eh := newEnforcedServer(t, cfg, append([]ServerOption{WithAuditLogger(auditor)}, opts...)...)
	th := tokenHarness{enforcedHarness: eh, h: eh.srv.Handler(), auditor: auditor}
	operator := ""
	if mode == AuthModeEnabled {
		operator = bearerFor(t, th.h, "alice")
	}
	body, err := json.Marshal(createChannelRequest{
		Name: "planning",
		Members: []channelMemberRequest{
			{ID: "agent-alice", Respond: "never"},
			{ID: "agent-bot", Respond: "never"},
			{ID: "alice-participant", Respond: "never"},
			{ID: "bob-participant", Respond: "never"},
		},
	})
	require.NoError(t, err)
	headers := map[string]string{}
	if operator != "" {
		headers["Authorization"] = "Bearer " + operator
	}
	rec := doRequestWithHeaders(th.h, http.MethodPost, "/api/v1/channels", body, headers)
	require.Equal(t, http.StatusCreated, rec.Code, rec.Body.String())
	return th
}

// register registers id and returns the agent token it was issued.
func (th tokenHarness) register(t *testing.T, id string) string {
	t.Helper()
	rec := doRequest(th.h, http.MethodPost, "/api/v1/agents/register", registerBody(id, "localhost:50051"))
	require.Equal(t, http.StatusCreated, rec.Code, rec.Body.String())
	token := decodeRegistered(t, rec.Body.String()).AgentToken
	require.NotEmpty(t, token)
	return token
}

// publish posts a message as sender to group:planning with headers, and
// metadata when it is not nil.
func (th tokenHarness) publish(t *testing.T, sender string, headers map[string]string,
	metadata map[string]any,
) *httptest.ResponseRecorder {
	t.Helper()
	msg := map[string]any{"sender_id": sender, "content": "the quarterly plan"}
	if metadata != nil {
		msg["metadata"] = metadata
	}
	body, err := json.Marshal(msg)
	require.NoError(t, err)
	return doRequestWithHeaders(th.h, http.MethodPost, "/api/v1/channels/group:planning/messages", body, headers)
}

// messages returns how many messages group:planning holds.
func (th tokenHarness) messages(t *testing.T) int {
	t.Helper()
	msgs, err := th.chStore.GetHistory(t.Context(), "group:planning", 100, time.Time{})
	require.NoError(t, err)
	return len(msgs)
}

// asAgent is the headers an agent sends: its ID, and its token when not "".
func asAgent(id, token string) map[string]string {
	headers := map[string]string{security.AgentIDHeader: id}
	if token != "" {
		headers[AgentTokenHeader] = token
	}
	return headers
}

// errorCode reads the code of a refusal in the server's usual error body.
func errorCode(t *testing.T, rec *httptest.ResponseRecorder) string {
	t.Helper()
	var body errorResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &body), rec.Body.String())
	return body.Code
}

func TestAgentToken_ProvesItsAgentUnderEnabled(t *testing.T) {
	th := newTokenHarness(t, AuthModeEnabled)
	token := th.register(t, "agent-alice")

	rec := th.publish(t, "agent-alice", asAgent("agent-alice", token), nil)
	assert.Equal(t, http.StatusCreated, rec.Code, rec.Body.String())

	// The token alone names the agent; an X-Agent-ID is not needed.
	rec = th.publish(t, "agent-alice", map[string]string{AgentTokenHeader: token}, nil)
	assert.Equal(t, http.StatusCreated, rec.Code, rec.Body.String())
	assert.Equal(t, 2, th.messages(t))
	assert.Empty(t, auditsOf(th.auditor, security.AuditAgentTokenInvalid))
}

func TestAgentToken_ForgedAgentIDBesideAValidTokenIsRefusedAndAudited(t *testing.T) {
	th := newTokenHarness(t, AuthModeEnabled)
	token := th.register(t, "agent-alice")

	rec := th.publish(t, "agent-bot", asAgent("agent-bot", token), nil)
	require.Equal(t, http.StatusUnauthorized, rec.Code, rec.Body.String())
	assert.Equal(t, AgentTokenInvalidCode, errorCode(t, rec),
		"the agents read a token naming another agent as invalid")
	assert.Zero(t, th.messages(t), "nothing was published")

	refusals := auditsOf(th.auditor, security.AuditAgentTokenInvalid)
	require.Len(t, refusals, 1)
	assert.Equal(t, "agent-bot", refusals[0].AgentID, "the line names the ID the call claimed")
	assert.Equal(t, "agent_id_mismatch", refusals[0].Detail["reason"])
	assert.Equal(t, "agent-alice", refusals[0].Detail["token_agent_id"])
	assert.NotContains(t, fmt.Sprint(refusals[0]), token, "the line never holds the token")
}

func TestAgentToken_RefusalsNameTheirCode(t *testing.T) {
	th := newTokenHarness(t, AuthModeEnabled)
	revoked := th.register(t, "agent-alice")
	require.Equal(t, http.StatusNoContent,
		doRequestWithHeaders(th.h, http.MethodDelete, "/api/v1/agents/agent-alice", nil,
			asAgent("agent-alice", revoked)).Code)
	replaced := th.register(t, "agent-bot")
	th.register(t, "agent-bot")

	for _, tc := range []struct {
		name, id, token, code, reason string
	}{
		{"not a token", "agent-alice", "agt1.not-a-token", AgentTokenInvalidCode, "invalid"},
		{"revoked by deregistration", "agent-alice", revoked, AgentTokenRevokedCode, "revoked"},
		// A later registration retires the token, and the refusal says
		// revoked too (PR 7 review F-4, settled in this PR).
		{"replaced by a later registration", "agent-bot", replaced, AgentTokenRevokedCode, "revoked"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			before := len(auditsOf(th.auditor, security.AuditAgentTokenInvalid))
			rec := th.publish(t, tc.id, asAgent(tc.id, tc.token), nil)
			require.Equal(t, http.StatusUnauthorized, rec.Code, rec.Body.String())
			assert.Equal(t, tc.code, errorCode(t, rec))
			refusals := auditsOf(th.auditor, security.AuditAgentTokenInvalid)
			require.Len(t, refusals, before+1)
			assert.Equal(t, tc.reason, refusals[before].Detail["reason"])
		})
	}
	assert.Zero(t, th.messages(t))
}

func TestAgentTokenRefusal_MapsEveryCheckError(t *testing.T) {
	for _, tc := range []struct {
		err          error
		code, reason string
	}{
		{security.ErrAgentTokenInvalid, AgentTokenInvalidCode, "invalid"},
		{security.ErrAgentTokenExpired, AgentTokenExpiredCode, "expired"},
		{security.ErrAgentTokenRevoked, AgentTokenRevokedCode, "revoked"},
		{errAgentTokenNamesAnother, AgentTokenInvalidCode, "agent_id_mismatch"},
	} {
		code, reason := agentTokenRefusal(tc.err)
		assert.Equal(t, tc.code, code, tc.err.Error())
		assert.Equal(t, tc.reason, reason, tc.err.Error())
	}
}

func TestAgentToken_RefusedBeforeTheRoutesPolicy(t *testing.T) {
	// A gated route answers a refused token with the token's code, not the
	// policy's 401, so an agent can tell that its token needs replacing.
	th := newTokenHarness(t, AuthModeEnabled)
	for _, path := range []string{"/api/v1/channels", "/api/v1/agents"} {
		rec := doRequestWithHeaders(th.h, http.MethodGet, path, nil, asAgent("agent-alice", "agt1.junk"))
		require.Equal(t, http.StatusUnauthorized, rec.Code, path)
		assert.Equal(t, AgentTokenInvalidCode, errorCode(t, rec), path)
	}
}

func TestAgentToken_RegistrationIsOutsideTheCheck(t *testing.T) {
	// A stale token must not stop the registration that replaces it.
	th := newTokenHarness(t, AuthModeEnabled)
	rec := doRequestWithHeaders(th.h, http.MethodPost, "/api/v1/agents/register",
		registerBody("agent-alice", "localhost:50051"), asAgent("agent-alice", "agt1.stale"))
	assert.Equal(t, http.StatusCreated, rec.Code, rec.Body.String())
	assert.Empty(t, auditsOf(th.auditor, security.AuditAgentTokenInvalid))
}

func TestAgentToken_BesideAnAccountCredentialIsRefused(t *testing.T) {
	th := newTokenHarness(t, AuthModeEnabled)
	token := th.register(t, "agent-alice")
	session := bearerFor(t, th.h, "bob")

	for name, credential := range map[string]map[string]string{
		"a bearer session": {"Authorization": "Bearer " + session},
		"a session cookie": {"Cookie": sessionCookieName + "=" + session},
		// Any Authorization header, not only a session (PR 8 review F-9):
		// a request has one caller.
		"basic credentials": {"Authorization": "Basic YWdlbnQ6c2VjcmV0"},
	} {
		t.Run(name, func(t *testing.T) {
			headers := asAgent("agent-alice", token)
			for k, v := range credential {
				headers[k] = v
			}
			rec := th.publish(t, "agent-alice", headers, nil)
			assert.Equal(t, http.StatusBadRequest, rec.Code, rec.Body.String())
		})
	}
	assert.Zero(t, th.messages(t))
	refusals := auditsOf(th.auditor, security.AuditAgentTokenInvalid)
	require.Len(t, refusals, 3)
	for _, ev := range refusals {
		assert.Equal(t, "account_credential", ev.Detail["reason"])
	}
}

func TestAgentToken_DisabledModeChecksNothing(t *testing.T) {
	th := newTokenHarness(t, AuthModeDisabled)
	token := th.register(t, "agent-alice")

	for name, headers := range map[string]map[string]string{
		"a token that is not one":   asAgent("agent-alice", "agt1.junk"),
		"a token naming another ID": asAgent("agent-bot", token),
	} {
		rec := th.publish(t, "agent-bot", headers, nil)
		assert.Equal(t, http.StatusCreated, rec.Code, "%s: %s", name, rec.Body.String())
	}
	assert.Empty(t, auditsOf(th.auditor, security.AuditAgentTokenInvalid))
}

func TestAgentToken_RefusalsPerSourceAreLimited(t *testing.T) {
	// Each refusal writes an audit line that is flushed at once, so one
	// address may be refused only so often before it is told to wait.
	th := newTokenHarness(t, AuthModeEnabled)
	for i := 0; i < tokenRefusalsPerSourceCalls; i++ {
		rec := doRequestWithHeaders(th.h, http.MethodGet, "/api/v1/channels", nil, asAgent("agent-alice", "agt1.junk"))
		require.Equal(t, http.StatusUnauthorized, rec.Code, "refusal %d", i)
	}
	rec := doRequestWithHeaders(th.h, http.MethodGet, "/api/v1/channels", nil, asAgent("agent-alice", "agt1.junk"))
	assert.Equal(t, http.StatusTooManyRequests, rec.Code)
	assert.NotEmpty(t, rec.Header().Get("Retry-After"))
	assert.Len(t, auditsOf(th.auditor, security.AuditAgentTokenInvalid), tokenRefusalsPerSourceCalls,
		"a refusal past the limit writes no line")

	// Another address is refused as usual.
	rec = requestFrom(th.h, http.MethodGet, "/api/v1/channels", nil, "198.51.100.7:4000",
		asAgent("agent-alice", "agt1.junk"))
	assert.Equal(t, http.StatusUnauthorized, rec.Code)
}

func TestRegisterAgent_RefusesTheDefaultPrincipal(t *testing.T) {
	// PR 6 review F-9: a token naming the shared tenant would let an agent
	// publish as every caller no account signed in for.
	assert.False(t, security.IssuableAgentID(channels.DefaultPrincipalID))
	srv, _ := enrollmentServer(t)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register",
		registerBody(channels.DefaultPrincipalID, "localhost:50051"))
	assert.Equal(t, http.StatusBadRequest, rec.Code, rec.Body.String())
}

func TestAgentToken_RefusalLineNamesOnlyAnIDAnAgentCouldHold(t *testing.T) {
	// The claimed ID is the caller's to fill (PR 8 review F-9).
	th := newTokenHarness(t, AuthModeEnabled)
	for _, claimed := range []string{strings.Repeat("a", 300), "Agent Alice", "web-console"} {
		rec := doRequestWithHeaders(th.h, http.MethodGet, "/api/v1/channels", nil, asAgent(claimed, "agt1.junk"))
		require.Equal(t, http.StatusUnauthorized, rec.Code, claimed)
	}
	refusals := auditsOf(th.auditor, security.AuditAgentTokenInvalid)
	require.Len(t, refusals, 3)
	for _, ev := range refusals {
		assert.Empty(t, ev.AgentID)
	}
}

func TestAgentToken_RefusalLineBoundsThePathAndMethod(t *testing.T) {
	// Anyone can cause the line, and the per-source limit counts lines, not
	// bytes (PR 8 review F-3).
	th := newTokenHarness(t, AuthModeEnabled)
	path := "/api/v1/" + strings.Repeat("a", 64*1024)
	rec := doRequestWithHeaders(th.h, strings.Repeat("M", 4096), path, nil, asAgent("agent-alice", "agt1.junk"))
	require.Equal(t, http.StatusUnauthorized, rec.Code)

	refusals := auditsOf(th.auditor, security.AuditAgentTokenInvalid)
	require.Len(t, refusals, 1)
	assert.True(t, strings.HasPrefix(path, strings.TrimSuffix(refusals[0].Resource, "…")),
		"the line keeps the start of the path")
	assert.LessOrEqual(t, len(refusals[0].Resource), auditFieldMax+len("…"))
	assert.LessOrEqual(t, len(fmt.Sprint(refusals[0].Detail["method"])), auditFieldMax+len("…"))
}

func TestRegistration_UnderEnabledSpendsItsAddressesBudget(t *testing.T) {
	// A registration shows no token, so under enabled it proves no agent.
	// Counted with every caller that has no account, anyone spending that
	// budget kept every agent from registering (PR 8 review F-2); it is
	// counted by its address, as the registration limiter counts it.
	limiterAudit := &recordingAuditor{}
	now := time.Now()
	rl, err := security.NewRateLimiter(security.RateLimitConfig{
		CallsPerWindow: 5, WindowSeconds: 60, Enabled: true, Logger: zap.NewNop(), Auditor: limiterAudit,
		Now: func() time.Time { return now },
	})
	require.NoError(t, err)
	_, cb := productionShapedLimiter(t, &recordingAuditor{})
	th := newTokenHarness(t, AuthModeEnabled, WithRateLimiter(rl, cb))
	for i := 0; i < 10; i++ { // callers with no account spend their budget
		doRequestWithHeaders(th.h, http.MethodGet, "/api/v1/channels", nil, nil)
	}
	register := func(id, remote string) int {
		return requestFrom(th.h, http.MethodPost, "/api/v1/agents/register",
			registerBody(id, "localhost:50051"), remote, asAgent(id, "")).Code
	}

	for i := 0; i < 5; i++ {
		require.Equal(t, http.StatusCreated, register(fmt.Sprintf("agent-%d", i), "198.51.100.9:5000"), i)
	}
	assert.Equal(t, http.StatusTooManyRequests, register("agent-5", "198.51.100.9:5001"),
		"one address shares one budget")
	assert.Equal(t, http.StatusCreated, register("agent-5", "198.51.100.10:5000"))

	// The limiter reports callers with no account once a window, with the
	// number of calls since its last line (ISSUE-0208). The first of the ten
	// calls above wrote the first line; one more call a window later writes
	// the second, which counts the other nine and itself, and none of the
	// seven registrations.
	now = now.Add(61 * time.Second)
	doRequestWithHeaders(th.h, http.MethodGet, "/api/v1/channels", nil, nil)
	lines := auditsOf(limiterAudit, security.AuditRateLimitUnauthenticatedCall)
	require.Len(t, lines, 2)
	assert.Equal(t, 10, lines[1].Detail["calls_since_last_event"],
		"a registration is not counted as a caller with no account")
}
