package server

// RFC 0009 Phases 3–4 plan PR 8 (design point G-7): every publish is bound to
// its caller. Under `auth.mode: enabled` a publish's sender_id must be the
// agent its token proves, or the signed-in account's own participant, typed
// `user`; anything else is refused with 403 and an authz.denied audit line.
// And in both modes an agent's REST calls spend a budget apart from its
// wallet calls, which a refusal never escalates (ISSUE-0187).

import (
	"context"
	"net/http"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	"github.com/mkhomutov/persatrix/internal/security"
)

// senderRefusals returns the authz.denied lines a publish's sender check wrote.
func senderRefusals(a *recordingAuditor) []security.AuditEvent {
	var out []security.AuditEvent
	for _, ev := range auditsOf(a, security.AuditAuthzDenied) {
		if ev.Detail["reason"] == "sender_not_caller" {
			out = append(out, ev)
		}
	}
	return out
}

func TestPublishSender_ATokenNamingAnotherSenderIsRefused(t *testing.T) {
	th := newTokenHarness(t, AuthModeEnabled)
	token := th.register(t, "agent-alice")

	rec := th.publish(t, "agent-bot", asAgent("agent-alice", token), nil)
	require.Equal(t, http.StatusForbidden, rec.Code, rec.Body.String())
	assert.Zero(t, th.messages(t))

	refusals := senderRefusals(th.auditor)
	require.Len(t, refusals, 1)
	assert.Equal(t, "agent-alice", refusals[0].AgentID, "the line names the agent the token proves")
	assert.Equal(t, "agent-bot", refusals[0].Detail["sender_id"])
}

func TestPublishSender_AnAccountPublishesOnlyAsItself(t *testing.T) {
	th := newTokenHarness(t, AuthModeEnabled)
	th.register(t, "agent-alice")
	bob := map[string]string{"Authorization": "Bearer " + bearerFor(t, th.h, "bob")}
	operator := map[string]string{"Authorization": "Bearer " + bearerFor(t, th.h, "alice")}

	for _, tc := range []struct {
		name     string
		headers  map[string]string
		sender   string
		metadata map[string]any
		want     int
	}{
		{"a user as itself", bob, "bob-participant", nil, http.StatusCreated},
		{"a user as itself, typed user", bob, "bob-participant", map[string]any{"participant_type": "user"}, http.StatusCreated},
		{"a user as an agent", bob, "agent-alice", nil, http.StatusForbidden},
		{"a user as itself, typed agent", bob, "bob-participant", map[string]any{"participant_type": "agent"}, http.StatusForbidden},
		{"a user as another account", bob, "alice-participant", nil, http.StatusForbidden},
		{"an operator as an agent", operator, "agent-alice", nil, http.StatusForbidden},
		{"an operator as itself", operator, "alice-participant", nil, http.StatusCreated},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rec := th.publish(t, tc.sender, tc.headers, tc.metadata)
			assert.Equal(t, tc.want, rec.Code, rec.Body.String())
		})
	}
	assert.Equal(t, 3, th.messages(t))
	assert.Len(t, senderRefusals(th.auditor), 4)
}

func TestPublishSender_NoCredentialIsRefusedUnderEnabled(t *testing.T) {
	// A caller that proves nothing names a sender nobody vouched for.
	th := newTokenHarness(t, AuthModeEnabled)
	rec := th.publish(t, "agent-alice", map[string]string{security.AgentIDHeader: "agent-alice"}, nil)
	assert.Equal(t, http.StatusForbidden, rec.Code, rec.Body.String())
	assert.Zero(t, th.messages(t))
	assert.Len(t, senderRefusals(th.auditor), 1)
}

func TestPublishSender_DisabledModeBindsNothing(t *testing.T) {
	th := newTokenHarness(t, AuthModeDisabled)
	for _, sender := range []string{"agent-alice", "bob-participant"} {
		rec := th.publish(t, sender, nil, nil)
		assert.Equal(t, http.StatusCreated, rec.Code, rec.Body.String())
	}
	assert.Empty(t, auditsOf(th.auditor, security.AuditAuthzDenied))
}

// walletCall sends one wallet-shaped gRPC call as agentID through the
// orchestrator's gRPC hook over rl and cb, and returns its code.
func walletCall(rl *security.RateLimiter, cb *security.CircuitBreaker, agentID string) codes.Code {
	ctx := metadata.NewIncomingContext(context.Background(), metadata.Pairs(security.AgentIDMetadataKey, agentID))
	ok := func(context.Context, any) (any, error) { return "ok", nil }
	_, err := security.GRPCRateLimitInterceptor(rl, cb)(ctx, nil,
		&grpc.UnaryServerInfo{FullMethod: "/persatrix.wallet.v1.WalletService/RequestLease"}, ok)
	return status.Code(err)
}

func TestRESTBurst_LeavesTheWalletAndQuarantinesNothing(t *testing.T) {
	// Each post into a channel makes every member persona read it, so a
	// burst of posts is a burst of the persona's own REST calls.
	for _, mode := range []string{AuthModeDisabled, AuthModeEnabled} {
		t.Run(mode, func(t *testing.T) {
			rl, err := security.NewRateLimiter(security.RateLimitConfig{
				CallsPerWindow: 3, WindowSeconds: 60, Enabled: true, Logger: zap.NewNop(),
			})
			require.NoError(t, err)
			_, cb := productionShapedLimiter(t, &recordingAuditor{})
			th := newTokenHarness(t, mode, WithRateLimiter(rl, cb))
			headers := asAgent("agent-alice", th.register(t, "agent-alice"))

			var refused int
			for i := 0; i < 20; i++ {
				rec := doRequestWithHeaders(th.h, http.MethodGet, "/api/v1/channels/group:planning/messages", nil, headers)
				if rec.Code == http.StatusTooManyRequests {
					refused++
				}
			}
			require.Equal(t, 17, refused, "precondition: the agent's REST budget is spent")
			assert.Empty(t, cb.QuarantinedAgents(), "REST refusals quarantine nothing")
			assert.Equal(t, codes.OK, walletCall(rl, cb, "agent-alice"), "the wallet keeps its own budget")
		})
	}
}

func TestAgentToken_RefusedTokenAddsNothingTowardQuarantine(t *testing.T) {
	th := newTokenHarness(t, AuthModeEnabled)
	rl, cb := productionShapedLimiter(t, &recordingAuditor{})
	WithRateLimiter(rl, cb)(th.srv)
	h := th.srv.Handler()
	token := th.register(t, "agent-alice")

	for i := 0; i < 40; i++ {
		rec := doRequestWithHeaders(h, http.MethodGet, "/api/v1/channels", nil, asAgent("agent-alice", "agt1.junk"))
		require.Equal(t, http.StatusUnauthorized, rec.Code)
	}
	assert.Empty(t, cb.QuarantinedAgents())
	rec := doRequestWithHeaders(h, http.MethodGet, "/api/v1/channels", nil, asAgent("agent-alice", token))
	assert.Equal(t, http.StatusOK, rec.Code, "nothing of the agent's budget was spent")
}

func TestAgentToken_AnUnprovenIDSpendsNoneOfTheAgentsBudget(t *testing.T) {
	// Under `enabled` the budget goes by the agent a token proves, so an
	// outsider naming agent-alice spends the shared anonymous budget.
	rl, err := security.NewRateLimiter(security.RateLimitConfig{
		CallsPerWindow: 2, WindowSeconds: 60, Enabled: true, Logger: zap.NewNop(),
	})
	require.NoError(t, err)
	_, cb := productionShapedLimiter(t, &recordingAuditor{})
	th := newTokenHarness(t, AuthModeEnabled, WithRateLimiter(rl, cb))
	token := th.register(t, "agent-alice")

	for i := 0; i < 2; i++ {
		doRequestWithHeaders(th.h, http.MethodGet, "/api/v1/channels", nil, asAgent("agent-alice", ""))
	}
	require.Equal(t, http.StatusTooManyRequests,
		doRequestWithHeaders(th.h, http.MethodGet, "/api/v1/channels", nil, nil).Code,
		"precondition: the outsider spent the anonymous budget")
	for i := 0; i < 2; i++ {
		rec := doRequestWithHeaders(th.h, http.MethodGet, "/api/v1/channels", nil, asAgent("agent-alice", token))
		assert.Equal(t, http.StatusOK, rec.Code, "call %d: the agent's own budget is whole", i)
	}
}

func TestPublishSender_RefusalLineBoundsThePath(t *testing.T) {
	// A caller with no credential can cause the line (PR 8 review F-3).
	th := newTokenHarness(t, AuthModeEnabled)
	path := "/api/v1/channels/" + strings.Repeat("a", 64*1024) + "/messages"
	rec := doRequestWithHeaders(th.h, http.MethodPost, path,
		[]byte(`{"sender_id":"agent-alice","content":"the quarterly plan"}`), nil)
	require.Equal(t, http.StatusForbidden, rec.Code, rec.Body.String())

	refusals := senderRefusals(th.auditor)
	require.Len(t, refusals, 1)
	assert.True(t, strings.HasPrefix(path, strings.TrimSuffix(refusals[0].Resource, "…")))
	assert.LessOrEqual(t, len(refusals[0].Resource), auditFieldMax+len("…"))
}
