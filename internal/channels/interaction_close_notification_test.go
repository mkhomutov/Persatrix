package channels

// RFC 0030 end-vote-close-propagation amendment (CP1/CP2) — acceptance,
// landed with the amendment doc (PR 1) and skip-guarded until the
// close-notification dispatch exists. PR 2 of the workstream removes the
// skip and extends the envelope assertions to the typed
// `interaction_close_notification` marker once the proto field is
// regenerated (the marker cannot be referenced before it compiles).
//
// The contract under test: an `end_votes` close is PROPAGATED to the
// room — every dispatch-served non-sender member (RespondAlways /
// RespondWhenMentioned) receives the closing message at close time
// through the per-recipient dispatch seam — while ordinary fanout of
// the closing vote stays suppressed (§H's posture is unchanged; the
// notification is delivery of a fact, not an invitation to speak).
// `respond: never` members are OUT of scope by design: fanout's v0.3.0
// short-circuit and [DispatchEnvelope.Recipient]'s documented invariant
// both exclude them upstream of the dispatcher, they run no agent-local
// tracker to starve, and the human surface reads the persisted closing
// vote from the store on demand.

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// closeDispatchRecorder captures (envelope, message) pairs under one
// lock. envelopeRecorder drops the message half and
// messageRecordingDispatcher appends to two slices without a mutex —
// fanout dispatches concurrently, and CP1's assertion needs the pair
// (WHO was notified and WHAT they were handed) race-free.
type closeDispatchRecorder struct {
	mu    sync.Mutex
	calls []closeDispatchCall
}

type closeDispatchCall struct {
	env DispatchEnvelope
	msg ChannelMessage
}

func (d *closeDispatchRecorder) Dispatch(_ context.Context, env DispatchEnvelope, msg ChannelMessage) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.calls = append(d.calls, closeDispatchCall{env: env, msg: msg})
	return nil
}

func (d *closeDispatchRecorder) snapshot() []closeDispatchCall {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]closeDispatchCall, len(d.calls))
	copy(out, d.calls)
	return out
}

func TestEndVoteClose_NotifiesEveryMemberOfTheClose(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &closeDispatchRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ch := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"alex":         RespondNever, // the human — reads history on demand, never dispatched
			"ember-owl":    RespondAlways,
			"iron-fox":     RespondAlways,
			"nova-sparrow": RespondAlways,
		}, "alex", "ember-owl", "iron-fox", "nova-sparrow")

	// A short discussion, then the quorum: nova-sparrow proposes in its
	// vote, iron-fox concurs — the second distinct vote closes.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "alex",
		Content: "relay or beacon — final call?",
	}, ""))
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "nova-sparrow",
		Content: "Synthesis: relay. Voting to close.",
		Metadata: map[string]any{
			cascadeDepthMetadataKey: 1,
			endVoteMetadataKey:      true,
		},
	}, ""))

	beforeClose := len(disp.snapshot())
	closingID := uuid.NewString()
	const closingContent = "Agreed — relay. Nothing further."
	closingMentions := []string{"nova-sparrow"}
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: closingID, ChannelID: ch, SenderID: "iron-fox",
		Content:  closingContent,
		Mentions: closingMentions,
		Metadata: map[string]any{
			cascadeDepthMetadataKey: 2,
			endVoteMetadataKey:      true,
		},
	}, ""))

	// CP5 makes the notification dispatch fire-and-forget, so nothing on
	// the publish path joins it — ordinary fanout (whose workers the sync
	// path DOES join) is suppressed for this very message. The router's
	// drain WaitGroup is the documented deterministic assert point
	// ([ChannelRouter.WaitForPendingFanout]): the notification goroutines
	// register on it, so after the drain the dispatch set below is stable.
	router.WaitForPendingFanout()

	// CP1: every dispatch-served non-sender member heard about the close —
	// exactly once each — and what they were handed IS the closing vote
	// (the synthesis/concurrence is real history, not a digest), under a
	// fresh per-recipient event id (CP2, the CE3 dedup lesson). iron-fox
	// closed its own tracker by voting and is excluded; alex sits outside
	// the dispatch contract (see the header note).
	notified := map[string]int{}
	seenIDs := map[string]bool{}
	for _, call := range disp.snapshot()[beforeClose:] {
		notified[call.env.Recipient.ParticipantID]++
		assert.True(t, call.env.InteractionCloseNotification,
			"CP2: the dispatch carries the typed close-notification marker")
		assert.False(t, call.env.ChairEscalation,
			"the notification is not a forced turn — the markers never alias")
		assert.Equal(t, closingContent, call.msg.Content,
			"the notification carries the closing vote verbatim")
		assert.Equal(t, "iron-fox", call.msg.SenderID,
			"the closing message keeps its real author")
		assert.NotEqual(t, closingID, call.msg.ID,
			"CP2: each notification rides a fresh event id, not the persisted vote's")
		assert.False(t, seenIDs[call.msg.ID],
			"CP2: event ids are fresh PER RECIPIENT")
		seenIDs[call.msg.ID] = true
		// CE3's aliasing half applies to EVERY reference field on the copied
		// message, not just the metadata map: the per-recipient goroutines
		// outlive the publish, so a notification sharing the closing vote's
		// mentions backing array would let any future in-place mentions
		// write corrupt a sibling's dispatch (PR #613 review). Same content,
		// different storage.
		assert.Equal(t, closingMentions, call.msg.Mentions,
			"the notification carries the closing vote's mentions verbatim")
		if len(call.msg.Mentions) > 0 {
			assert.NotSame(t, &closingMentions[0], &call.msg.Mentions[0],
				"the mentions slice is cloned per recipient, never aliased")
		}
	}
	// The exactly-once shape carries CP2's suppression half as well:
	// ordinary fanout of the closing vote stays suppressed (a count of 2
	// for any member would mean the close un-suppressed fanout instead of
	// notifying).
	assert.Equal(t, map[string]int{
		"ember-owl":    1,
		"nova-sparrow": 1,
	}, notified,
		"the end_votes close reached every dispatch-served non-sender member exactly once")
}

// panickingCloseDispatcher panics on the marked close-notification dispatch
// and accepts everything else, so a test can prove the notification
// goroutines carry their own recover: they run off the request goroutine
// (no recoveryMiddleware umbrella), and an unrecovered panic in any
// goroutine terminates the whole orchestrator — the invariant
// [ChannelRouter.recoverFanout] exists to hold (PR #613 review; the same
// posture [TestPublishAsync] pins for the detached fanout goroutine).
type panickingCloseDispatcher struct {
	mu       sync.Mutex
	ordinary int
}

func (d *panickingCloseDispatcher) Dispatch(_ context.Context, env DispatchEnvelope, _ ChannelMessage) error {
	if env.InteractionCloseNotification {
		panic("panickingCloseDispatcher: simulated close-notification dispatch panic")
	}
	d.mu.Lock()
	d.ordinary++
	d.mu.Unlock()
	return nil
}

// TestEndVoteClose_NotificationDispatchPanicIsRecovered pins the
// recoverFanout contract onto the close-notification goroutines: like every
// per-recipient dispatch worker they run detached from the request
// goroutine, so a panicking dispatch must be recovered at the goroutine's
// top frame or it crashes the process. The drain returning — and the test
// binary surviving — IS the assertion: absent the recover the panic
// propagates and `go test` aborts with a crash.
func TestEndVoteClose_NotificationDispatchPanicIsRecovered(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &panickingCloseDispatcher{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	id := mustCreateGroup(t, store, "planning", "alice", "bob", "carol")
	router.SetEndVoteParams(id, 2, 3)

	require.NoError(t, endVote(t, router, id, "alice", "int-panic"))
	require.NoError(t, endVote(t, router, id, "bob", "int-panic"))
	router.WaitForPendingFanout()

	disp.mu.Lock()
	ordinary := disp.ordinary
	disp.mu.Unlock()
	assert.Positive(t, ordinary,
		"sanity: the first vote's ordinary fanout reached the dispatcher (the panic fired on the notifications, not on everything)")
}

// closeTimedDisconnectStore simulates the one client-disconnect timing the
// close notification's contract calls out: the publishing connection dies
// exactly while the notification's member lookup is in flight. Arm it
// before the closing vote; the next GetMembers call fires the cancel (the
// disconnect) and then behaves like any context-honouring driver — if the
// context it was handed descends from the now-dead request, the lookup
// aborts with the context's error. A lookup on a detached context proceeds.
type closeTimedDisconnectStore struct {
	ChannelStore
	mu     sync.Mutex
	cancel context.CancelFunc // the simulated client disconnect
	armed  bool
}

func (s *closeTimedDisconnectStore) arm(cancel context.CancelFunc) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.cancel, s.armed = cancel, true
}

func (s *closeTimedDisconnectStore) GetMembers(ctx context.Context, channelID string) ([]Member, error) {
	s.mu.Lock()
	cancel, armed := s.cancel, s.armed
	s.armed = false
	s.mu.Unlock()
	if armed {
		cancel()
		if err := ctx.Err(); err != nil {
			return nil, err
		}
	}
	return s.ChannelStore.GetMembers(ctx, channelID)
}

// TestEndVoteClose_NotificationSurvivesClientDisconnect pins the detachment
// half of CP5's posture: the close is a one-shot, unrecoverable signal (the
// interaction is already closed; nothing retries it), so the publishing
// client hanging up must not be able to kill the room's notification.
// That requires the member lookup — not just the per-recipient dispatches —
// to run on a context detached from the request: a lookup still descended
// from the request context dies with it, and the whole fan is dropped with
// only a warn while every member's tracker mislabels the close as "went
// idle" — the exact MT-CHANNEL-GOV-004 regression the amendment exists to
// fix (PR #613 review).
func TestEndVoteClose_NotificationSurvivesClientDisconnect(t *testing.T) {
	base := newTestStore(t, SQLiteOptions{})
	store := &closeTimedDisconnectStore{ChannelStore: base}
	disp := &closeDispatchRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	id := mustCreateGroup(t, store, "planning", "alice", "bob", "carol")
	router.SetEndVoteParams(id, 2, 3)

	require.NoError(t, endVote(t, router, id, "alice", "int-disconnect"))
	before := len(disp.snapshot())

	// The closing publish rides a cancellable request context, and the
	// armed store cancels it from inside the very GetMembers call the
	// notification makes — the disconnect lands at the worst possible
	// instant, deterministically.
	reqCtx, cancel := context.WithCancel(context.Background())
	defer cancel()
	store.arm(cancel)
	require.NoError(t, router.Publish(reqCtx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "bob",
		Content: "Concur — closing.",
		Metadata: map[string]any{
			endVoteMetadataKey:       true,
			interactionIDMetadataKey: "int-disconnect",
		},
	}, ""))
	router.WaitForPendingFanout()

	require.Error(t, reqCtx.Err(),
		"sanity: the simulated disconnect fired during the member lookup")
	notified := map[string]int{}
	for _, call := range disp.snapshot()[before:] {
		notified[call.env.Recipient.ParticipantID]++
	}
	assert.Equal(t, map[string]int{"alice": 1, "carol": 1}, notified,
		"a client disconnect mid-lookup must not drop the room's close signal")
}

// TestEndVoteClose_NotificationFanoutRespectsConcurrencyBound pins
// ISSUE-0014's bound onto the close-notification fan: ordinary fanout caps
// peak in-flight dispatches at channelFanoutMaxConcurrency with the
// acquire on the spawning loop (backpressure, not goroutine-per-recipient),
// and the close notification dispatches to exactly the same recipient set —
// an unbounded spawn would burst N simultaneous dispatches at the close
// instant on a channel ordinary fanout deliberately paces (PR #613 review).
func TestEndVoteClose_NotificationFanoutRespectsConcurrencyBound(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &slowDispatcher{delay: 50 * time.Millisecond}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)

	n := channelFanoutMaxConcurrency + 8 // exceed the bound
	members := []string{"alice", "bob"}
	for i := 0; i < n; i++ {
		members = append(members, fmt.Sprintf("member%d", i))
	}
	id := mustCreateGroup(t, store, "big", members...)
	router.SetEndVoteParams(id, 2, 3)

	require.NoError(t, endVote(t, router, id, "alice", "int-bound"))
	require.NoError(t, endVote(t, router, id, "bob", "int-bound"))
	router.WaitForPendingFanout()

	calls, peak := disp.snapshot()
	// The first vote fans out to its n+1 non-sender members; the closing
	// vote's fanout is suppressed and replaced by n+1 close notifications.
	assert.Equal(t, 2*(n+1), calls,
		"every member is fanned the first vote and notified of the close")
	assert.LessOrEqual(t, peak, channelFanoutMaxConcurrency,
		"peak in-flight dispatches must not exceed the fanout bound at close time")
	// Sanity-check the lower side too: a regression to sequential dispatch
	// would peak at 1, which would also "respect the bound" trivially.
	assert.Greater(t, peak, 1,
		"the notification fan must actually run concurrently (peak in-flight > 1)")
}
