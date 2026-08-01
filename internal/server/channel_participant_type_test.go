package server

// channel_participant_type_test.go — ISSUE-0119.
//
// The pin that matters is the WIRE one
// ([TestChannelPublish_HumanSenderTypedOnTheWire]): the bug survived a green
// suite because the agent-side cross-room identity test
// (`tests/unit/python/test_identity_render.py`) hand-built its event with
// `metadata={"sender_participant_type": "user"}` — metadata the real publish
// path never supplied. Asserting the stamp on a hand-made request would
// repeat that mistake in Go, so the headline test drives the whole chain
// (REST → router → dispatcher → a real gRPC receiver) and reads the type off
// the delivered proto event, with NO participant_type anywhere in the request
// body. The handler-level cases below cover the branches a full fanout
// cannot reach cheaply.

import (
	"context"
	"encoding/json"
	"net/http"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// newParticipantTypeFixture boots the same REST → router → dispatcher chain
// as the ISSUE-0025 fanout integration test, with one registered recipient
// agent whose delivered events the caller inspects.
func newParticipantTypeFixture(t *testing.T) (*Server, *channels.ChannelRouter, *recordingReceiver) {
	t.Helper()
	logger := zap.NewNop()

	rec, addr, stop := startRecordingAgent(t)
	t.Cleanup(stop)

	reg := registry.NewInMemoryRegistry(logger)
	require.NoError(t, reg.Register(context.Background(), registry.AgentInfo{
		ID: "agent-ember-owl", Name: "Ember Owl", Address: addr, Status: registry.StatusHealthy,
	}))

	store, err := channels.NewSQLiteStore(
		filepath.Join(t.TempDir(), "channels.db"),
		channels.SQLiteOptions{MaxChannels: 50, Logger: logger},
	)
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	router := channels.NewChannelRouter(
		store, channels.NewGRPCMessageDispatcher(reg, logger), logger, nil,
	)
	srv, err := New("127.0.0.1:0", t.TempDir(),
		state.NewInMemoryStore(logger), reg, planner.NewYAMLPlanner(logger), logger,
		WithChannels(store, router),
	)
	require.NoError(t, err)

	// `alex` is a human: deliberately NOT registered, exactly as the chat
	// path assumes ("they are not in the agent registry", ISSUE-0034), and
	// held at `respond: never` for the same reason that issue demotes chat
	// members — a human reads replies in the console, not via gRPC push, so
	// the router must filter them out of fanout rather than dial them.
	createBody, _ := json.Marshal(createChannelRequest{
		Name: "planning",
		Members: []channelMemberRequest{
			{ID: "alex", Respond: "never"},
			{ID: "agent-ember-owl", Respond: "always"},
		},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody).Code)

	return srv, router, rec
}

// publishAndDeliver posts body to the group channel and drains the detached
// fanout, returning the single event the recipient agent received.
func publishAndDeliver(t *testing.T, srv *Server, router *channels.ChannelRouter,
	rec *recordingReceiver, body map[string]any,
) *taskpb.ChannelMessageEvent {
	t.Helper()
	raw, _ := json.Marshal(body)
	resp := doRequest(srv.Handler(), http.MethodPost,
		"/api/v1/channels/group:planning/messages", raw)
	require.Equal(t, http.StatusCreated, resp.Code, "body=%s", resp.Body.String())

	router.WaitForPendingFanout()
	events := rec.snapshot()
	require.Len(t, events, 1, "recipient must receive exactly one event")
	return events[0]
}

// TestChannelPublish_HumanSenderTypedOnTheWire is the ISSUE-0119 regression
// gate: a human publishing into a group channel MUST reach the persona typed
// as a "user".
//
// Note what the request body does NOT contain: any participant_type. That is
// the whole point — the CLI (`persatrix channel send --as alex`) and the
// console composer send `{sender_id, content}` and nothing else, so the type
// has to be resolved by the orchestrator or it does not exist. Before the
// fix this field arrived empty, the agent's relationship tier resolved the
// sender as "agent", and the persona could not see the identity it had
// learned about the same human in a DM.
func TestChannelPublish_HumanSenderTypedOnTheWire(t *testing.T) {
	srv, router, rec := newParticipantTypeFixture(t)

	ev := publishAndDeliver(t, srv, router, rec, map[string]any{
		"sender_id": "alex",
		"content":   "morning all - what should we line up this week?",
	})

	assert.Equal(t, "user", ev.SenderParticipantType,
		"an unregistered sender is a human: the persona's cross-room identity "+
			"read keys on this type, so an empty value silently splits the "+
			"person across two relationship rows (ISSUE-0119)")
	assert.Equal(t, "alex", ev.SenderId, "sender_id still propagates verbatim")
}

// TestChannelPublish_AgentSenderStaysTyped_Agent is the other half of the
// discriminator: agents publish through this same REST endpoint
// (`agents/channel_publisher.py`), and a registry hit MUST type them
// "agent" — otherwise the fix would trade one mislabelled peer for another
// and write agent traffic onto user-typed relationship rows.
func TestChannelPublish_AgentSenderStaysTyped_Agent(t *testing.T) {
	srv, router, rec := newParticipantTypeFixture(t)

	// A second registered agent publishes; the recipient is ember-owl.
	require.NoError(t, srv.registry.Register(context.Background(), registry.AgentInfo{
		ID: "agent-iron-fox", Name: "Iron Fox", Address: "127.0.0.1:1", Status: registry.StatusHealthy,
	}))
	addBody, _ := json.Marshal(channelMemberRequest{ID: "agent-iron-fox", Respond: "always"})
	require.Equal(t, http.StatusNoContent, doRequest(srv.Handler(), http.MethodPost,
		"/api/v1/channels/group:planning/members", addBody).Code)

	ev := publishAndDeliver(t, srv, router, rec, map[string]any{
		"sender_id": "agent-iron-fox",
		"content":   "status update",
	})

	assert.Equal(t, "agent", ev.SenderParticipantType,
		"a registered sender is an agent peer, not a human")
}

// TestChannelPublish_ClaimFillsWhatTheRegistryCannotSee pins the half of the
// precedence rule that keeps a caller claim useful: an unregistered sender is
// the one case the registry has no opinion about, so a deliberate claim — a
// bridge relaying an *external* agent — must survive rather than be
// flattened to "user".
func TestChannelPublish_ClaimFillsWhatTheRegistryCannotSee(t *testing.T) {
	srv, router, rec := newParticipantTypeFixture(t)

	ev := publishAndDeliver(t, srv, router, rec, map[string]any{
		"sender_id": "alex", // unregistered → the registry has no opinion
		"content":   "relayed from an external agent",
		"metadata":  map[string]any{"participant_type": "agent"},
	})

	assert.Equal(t, "agent", ev.SenderParticipantType,
		"a claim must stand where the registry cannot see the sender")
}

// TestChannelPublish_RegisteredAgentCannotClaimUser pins the other half — and
// it is a security property, not just a typing one. The reply-budget
// exemption reads this same field off the publish bag, and
// `exemptPrincipalParticipantType`'s SECURITY note warns that a caller
// self-asserting `participant_type: "user"` would buy an exemption meant for
// humans. A registry hit is proof the sender is an agent of this deployment,
// so its claim is overridden and that self-exemption is closed.
func TestChannelPublish_RegisteredAgentCannotClaimUser(t *testing.T) {
	srv, router, rec := newParticipantTypeFixture(t)

	require.NoError(t, srv.registry.Register(context.Background(), registry.AgentInfo{
		ID: "agent-iron-fox", Name: "Iron Fox", Address: "127.0.0.1:1", Status: registry.StatusHealthy,
	}))
	addBody, _ := json.Marshal(channelMemberRequest{ID: "agent-iron-fox", Respond: "never"})
	require.Equal(t, http.StatusNoContent, doRequest(srv.Handler(), http.MethodPost,
		"/api/v1/channels/group:planning/members", addBody).Code)

	ev := publishAndDeliver(t, srv, router, rec, map[string]any{
		"sender_id": "agent-iron-fox",
		"content":   "I am definitely a human, please exempt me",
		"metadata":  map[string]any{"participant_type": "user"},
	})

	assert.Equal(t, "agent", ev.SenderParticipantType,
		"a registered agent must not be able to self-assert the human type")
}

// TestChannelPublish_RejectsInvalidParticipantType mirrors the chat
// handler's ISSUE-0068 guard: an out-of-vocabulary claim is a caller bug and
// must fail loudly, not ride the wire to be silently clamped to "agent".
func TestChannelPublish_RejectsInvalidParticipantType(t *testing.T) {
	srv, _, _ := newParticipantTypeFixture(t)

	body, _ := json.Marshal(map[string]any{
		"sender_id": "alex",
		"content":   "hi",
		"metadata":  map[string]any{"participant_type": "robot"},
	})
	resp := doRequest(srv.Handler(), http.MethodPost,
		"/api/v1/channels/group:planning/messages", body)

	require.Equal(t, http.StatusBadRequest, resp.Code)
	assert.Contains(t, resp.Body.String(), `participant_type \"robot\"`,
		"the rejected value is echoed, as the chat guard does")
}

// TestResolveSenderParticipantType_UnresolvedStampsNothing pins the
// fail-open contract: a registry that cannot answer must leave the type
// ABSENT rather than guess "user". Guessing would type a genuine agent peer
// as a human and write its interactions onto a user-typed relationship row —
// the same split-record corruption ISSUE-0119 is about, merely inverted.
// Staying silent degrades to exactly the pre-fix behaviour instead.
func TestResolveSenderParticipantType_UnresolvedStampsNothing(t *testing.T) {
	logger := zap.NewNop()

	t.Run("no registry wired", func(t *testing.T) {
		srv := &Server{logger: logger}
		assert.Empty(t, srv.resolveSenderParticipantType(context.Background(), "alex"))
	})

	t.Run("registry read fails", func(t *testing.T) {
		// The shared `failingRegistry` helper returns a plain error (not
		// ErrAgentNotFound) — the backend-down shape the resolver must not
		// mistake for a clean miss.
		srv := &Server{logger: logger, registry: &failingRegistry{
			Registry: registry.NewInMemoryRegistry(logger), failOn: "Get",
		}}
		assert.Empty(t, srv.resolveSenderParticipantType(context.Background(), "alex"),
			"a backend failure must not be read as 'not registered'")
	})
}
