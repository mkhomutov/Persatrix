package server

// RFC 0011 display-name-mention-lifting amendment (ML1–ML3) — acceptance,
// landed with the amendment doc (PR 1) and skip-guarded until the
// publish-handler wiring exists. PR 3 of the workstream removes the skips;
// PR 2 (the pure lift substrate in internal/channels) carries its own unit
// matrix and never edits these assertions.
//
// The contract under test: an in-text `@`-mention is lifted at the REST
// publish seam — resolved against the channel's member set (exact id, then
// case-insensitive whole-display-name longest-match via the registry's
// AgentInfo.Name) and unioned into the structured `mentions` array as
// canonical participant ids BEFORE persist and fanout, so the floor
// resolution ([resolveFloorMentions]) and the Tier A gate see the ids the
// prose always meant. Ambiguity lifts nobody (ML3): misdirecting the floor
// is worse than the silence it replaces.
//
// Both tests are written entirely against seams that exist today (the
// registry's Name field, the REST publish boundary, the envelope's
// FloorMentions stamp — whose proto projection `floor_mentions` /
// `floor_mentions_resolved` is pinned by the dispatcher suites), so the
// red-without-skip posture needs no planned API: the scenario is the live
// MT-CHANNEL-GOV-004 chair hand-off that deadlocked both escalated
// interactions (ISSUE-0096, three-for-three on outcome (b)).

import (
	"context"
	"encoding/json"
	"net/http"
	"path/filepath"
	"sync"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// liftEnvelopeRecorder captures (envelope, message) pairs under one lock —
// fanout dispatches concurrently, and the assertions below need WHO was
// dispatched and WHAT mentions/floor basis they were handed, race-free
// (the closeDispatchRecorder posture from the close-propagation acceptance).
type liftEnvelopeRecorder struct {
	mu    sync.Mutex
	calls []liftDispatchCall
}

type liftDispatchCall struct {
	env channels.DispatchEnvelope
	msg channels.ChannelMessage
}

func (d *liftEnvelopeRecorder) Dispatch(_ context.Context, env channels.DispatchEnvelope, msg channels.ChannelMessage) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.calls = append(d.calls, liftDispatchCall{env: env, msg: msg})
	return nil
}

func (d *liftEnvelopeRecorder) snapshot() []liftDispatchCall {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]liftDispatchCall, len(d.calls))
	copy(out, d.calls)
	return out
}

// liftTestServer wires a server whose router uses the recording dispatcher
// and whose registry carries the given id→display-name directory — the same
// `AgentInfo.Name` seam the Python roster join reads, standing in for the
// production registry the lift resolves display names against (ML3). The
// addresses are never dialled (the recorder replaces the gRPC dispatcher).
func liftTestServer(t *testing.T, disp channels.MessageDispatcher, displayNames map[string]string) (*Server, *channels.ChannelRouter) {
	t.Helper()
	logger := zap.NewNop()
	dbPath := filepath.Join(t.TempDir(), "channels.db")
	store, err := channels.NewSQLiteStore(dbPath, channels.SQLiteOptions{
		MaxChannels: 50,
		Logger:      logger,
	})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	reg := registry.NewInMemoryRegistry(logger)
	for id, name := range displayNames {
		require.NoError(t, reg.Register(context.Background(), registry.AgentInfo{
			ID: id, Name: name, Address: "127.0.0.1:1", Status: registry.StatusHealthy,
		}))
	}

	router := channels.NewChannelRouter(store, disp, logger, nil)
	srv, err := New("127.0.0.1:0", t.TempDir(),
		state.NewInMemoryStore(logger),
		reg,
		planner.NewYAMLPlanner(logger),
		logger,
		WithChannels(store, router),
	)
	require.NoError(t, err)
	return srv, router
}

// TestPublishMessage_LiftsDisplayNameMentionsFromContent pins ML1/ML2/ML3's
// happy path on the live failure verbatim: the chair's prose hand-off
// (`@Ember Owl @Iron Fox …`) with the synthesized inbound-sender entry as
// its only structured mention (`["alex"]` — the respond:never human, exactly
// what channel_reply.py emits for a prose reply to the forced turn). Today
// that publish reclassifies to open floor and dies in silence; under the
// amendment the lift unions the canonical ids in BEFORE persist and fanout,
// so the persisted row names the addressees and every recipient's envelope
// carries the floor-capable basis — the hand-off is directed, not open floor.
func TestPublishMessage_LiftsDisplayNameMentionsFromContent(t *testing.T) {
	t.Skip("ML acceptance (0011-amendment-display-name-mention-lifting §E) — unskip in PR 3, the publish-handler wiring")

	disp := &liftEnvelopeRecorder{}
	srv, router := liftTestServer(t, disp, map[string]string{
		"nova-sparrow": "Nova Sparrow",
		"ember-owl":    "Ember Owl",
		"iron-fox":     "Iron Fox",
		// alex — the human — has no registry row by design (ML3/OQ 3):
		// display names ARE registry data; the structured mention of an
		// unregistered member must keep working untouched.
	})
	handler := srv.Handler()

	createBody, _ := json.Marshal(createChannelRequest{
		Name: "planning",
		Members: []channelMemberRequest{
			{ID: "alex", Respond: "never"}, // the human operator (documented join convention)
			{ID: "nova-sparrow", Respond: "always"},
			{ID: "ember-owl", Respond: "always"},
			{ID: "iron-fox", Respond: "when_mentioned"},
		},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(handler, http.MethodPost, "/api/v1/channels", createBody).Code)

	// Runtime-created groups get floor control ON by default
	// (applyRuntimeGroupGovernance); with ≥2 candidate responders that is
	// the serialized round — each silent turn burning the full per-turn
	// timeout against this test's reply-less recorder. The lift under test
	// sits UPSTREAM of the floor/concurrent branch (the union happens
	// before persist; the FloorMentions stamp rides both paths' envelopes
	// identically), so the concurrent path asserts the same contract in
	// milliseconds.
	router.SetFloorControl("group:planning", false, 0)

	// The chair's hand-off, spelled the way the conversation window renders
	// speakers (`**Iron Fox:**` headers) — NOT as participant ids.
	pubBody, _ := json.Marshal(map[string]any{
		"sender_id": "nova-sparrow",
		"content":   "@Ember Owl @Iron Fox — alex needs one risk each from all of us on the relay plan.",
		"mentions":  []string{"alex"},
	})
	rec := doRequest(handler, http.MethodPost, "/api/v1/channels/group:planning/messages", pubBody)
	require.Equal(t, http.StatusCreated, rec.Code, "body=%s", rec.Body.String())

	// ML1's union order is contract: structured entries first (the
	// producer's explicit intent outranks prose), lifted ids in content
	// order after. ML2: canonical ids only — the display-name spellings
	// never appear.
	var resp channelMessageResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, []string{"alex", "ember-owl", "iron-fox"}, resp.Mentions,
		"the persisted mentions carry the structured entry plus the lifted canonical ids, in union order")

	// The 201 answers at the persistence boundary; fanout is detached
	// (RFC 0048). Drain before snapshotting the dispatch set.
	router.WaitForPendingFanout()

	// Exactly the two dispatch-served non-sender members hear the message
	// (alex is respond:never — outside the dispatch contract; the sender is
	// filtered), and BOTH halves of the resolution ride every envelope:
	// the unioned mentions on the message, the floor-capable subset on the
	// FloorMentions stamp (alex is structurally present in mentions but not
	// floor-capable — the directedness amendment's basis is unchanged, it
	// just finally sees the addressees).
	dispatched := map[string]int{}
	for _, call := range disp.snapshot() {
		dispatched[call.env.Recipient.ParticipantID]++
		assert.Equal(t, []string{"alex", "ember-owl", "iron-fox"}, call.msg.Mentions,
			"the dispatched message carries the unioned mentions, not the producer's raw array")
		assert.Equal(t, []string{"ember-owl", "iron-fox"}, call.env.FloorMentions,
			"the floor basis is the lifted ids' floor-capable subset — the hand-off is directed, not open floor")
	}
	assert.Equal(t, map[string]int{
		"ember-owl": 1,
		"iron-fox":  1,
	}, dispatched,
		"the named members are dispatched exactly once each; the human and the sender are not")
}

// TestPublishMessage_AmbiguousDisplayNameLiftsNobody pins ML3's collision
// rule: two members whose folded display names collide make that name
// unresolvable — the colliding token lifts NEITHER (misdirecting the floor
// to the wrong member is strictly worse than today's silence; the collision
// is a config smell that logs WARN) — while unambiguous tokens in the same
// publish still lift.
func TestPublishMessage_AmbiguousDisplayNameLiftsNobody(t *testing.T) {
	t.Skip("ML acceptance (0011-amendment-display-name-mention-lifting §E) — unskip in PR 3, the publish-handler wiring")

	disp := &liftEnvelopeRecorder{}
	srv, router := liftTestServer(t, disp, map[string]string{
		"nova-sparrow": "Nova Sparrow",
		"river-heron":  "River Heron",
		"river-finch":  "River Heron", // the collision — same display name, distinct ids
		"iron-fox":     "Iron Fox",
	})
	handler := srv.Handler()

	createBody, _ := json.Marshal(createChannelRequest{
		Name: "review",
		Members: []channelMemberRequest{
			{ID: "nova-sparrow", Respond: "always"},
			{ID: "river-heron", Respond: "always"},
			{ID: "river-finch", Respond: "always"},
			{ID: "iron-fox", Respond: "always"},
		},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(handler, http.MethodPost, "/api/v1/channels", createBody).Code)

	// Off the serialized floor round, as above — three always-responders
	// would otherwise burn a full silent turn timeout each.
	router.SetFloorControl("group:review", false, 0)

	pubBody, _ := json.Marshal(map[string]any{
		"sender_id": "nova-sparrow",
		"content":   "@River Heron and @Iron Fox — split the relay review between you.",
	})
	rec := doRequest(handler, http.MethodPost, "/api/v1/channels/group:review/messages", pubBody)
	require.Equal(t, http.StatusCreated, rec.Code, "body=%s", rec.Body.String())

	var resp channelMessageResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, []string{"iron-fox"}, resp.Mentions,
		"the ambiguous name lifts nobody; the unambiguous token in the same publish still lifts")

	router.WaitForPendingFanout()

	// Every member still HEARS the message (ordinary fanout to always
	// members is untouched) — but the floor basis names only the
	// unambiguous addressee: neither river member was guessed at.
	for _, call := range disp.snapshot() {
		assert.Equal(t, []string{"iron-fox"}, call.env.FloorMentions,
			"the floor basis carries only the unambiguous lift — no misdirection on a name collision")
	}
	require.NotEmpty(t, disp.snapshot(), "sanity: fanout dispatched the publish")
}
