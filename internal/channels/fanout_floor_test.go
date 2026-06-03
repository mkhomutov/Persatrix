package channels

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// floorDispatcher is the integration seam for the RFC 0030 floor loop
// (PR 2). It plays the part of a fleet of agents reachable over the gRPC
// dispatcher: on each per-recipient dispatch it (a) records the dispatch
// order + the channel history visible *at dispatch time* (the proof that a
// speaker reads its predecessors' replies), (b) tracks peak in-flight
// dispatches, and (c) for recipients in `replies`, simulates the agent's
// asynchronous REST publish of a reply by calling `router.Publish` on a
// fresh goroutine — exactly the path the loop correlates against via the
// reply waiter.
//
// The async reply mirrors production: the real `ReceiveChannelMessage`
// dispatch is fire-and-forget and the agent's reply arrives later as an
// independent `POST /messages`. Registering the waiter *before* dispatch
// (the loop's contract) makes the buffered reply land deterministically
// whether or not the loop has parked on the select yet.
type floorDispatcher struct {
	router *ChannelRouter
	store  ChannelStore

	// replies is the set of recipient ids that auto-publish a reply when
	// dispatched. A recipient absent from this set never replies, so the
	// loop must advance on the per-turn timeout.
	replies map[string]bool

	mu                sync.Mutex
	order             []string            // recipient dispatch order
	historyAtDispatch map[string][]string // recipient -> sender ids visible in history when it was dispatched
	inFlight          int
	maxInFlight       int
}

func newFloorDispatcher(store ChannelStore, replies ...string) *floorDispatcher {
	set := make(map[string]bool, len(replies))
	for _, r := range replies {
		set[r] = true
	}
	return &floorDispatcher{
		store:             store,
		replies:           set,
		historyAtDispatch: make(map[string][]string),
	}
}

func (d *floorDispatcher) Dispatch(ctx context.Context, env DispatchEnvelope, msg ChannelMessage) error {
	rid := env.Recipient.ParticipantID

	// Snapshot what this recipient can see in channel history at the
	// moment it is dispatched. Newest-first; we keep sender ids.
	hist, _ := d.store.GetHistory(ctx, msg.ChannelID, 100, time.Time{})
	senders := make([]string, 0, len(hist))
	for _, m := range hist {
		senders = append(senders, m.SenderID)
	}

	d.mu.Lock()
	d.inFlight++
	if d.inFlight > d.maxInFlight {
		d.maxInFlight = d.inFlight
	}
	d.order = append(d.order, rid)
	d.historyAtDispatch[rid] = senders
	d.mu.Unlock()

	if d.replies[rid] {
		// Simulate the agent composing and POSTing its reply on an
		// independent goroutine — the loop awaits this via the waiter.
		go func() {
			_ = d.router.Publish(context.Background(), ChannelMessage{
				ID:        uuid.NewString(),
				ChannelID: msg.ChannelID,
				SenderID:  rid,
				Content:   rid + " reply",
			}, "")
		}()
	}

	d.mu.Lock()
	d.inFlight--
	d.mu.Unlock()
	return nil
}

func (d *floorDispatcher) snapshot() (order []string, peak int, history map[string][]string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	order = append([]string(nil), d.order...)
	history = make(map[string][]string, len(d.historyAtDispatch))
	for k, v := range d.historyAtDispatch {
		history[k] = append([]string(nil), v...)
	}
	return order, d.maxInFlight, history
}

// mustCreateGroupWithPolicies creates a group channel whose members carry the
// declared respond policies (mustCreateGroup hardcodes when_mentioned).
func mustCreateGroupWithPolicies(t *testing.T, store ChannelStore, name string, members map[string]RespondPolicy, order ...string) string {
	t.Helper()
	id := "group:" + name
	require.NoError(t, store.CreateChannel(context.Background(), Channel{
		ID: id, Name: name, Type: ChannelTypeGroup,
	}))
	for _, m := range order {
		require.NoError(t, store.AddMember(context.Background(), id, m, members[m]))
	}
	return id
}

func containsSender(senders []string, id string) bool {
	for _, s := range senders {
		if s == id {
			return true
		}
	}
	return false
}

// TestFloorRound_SerializesResponders_MutualVisibility is the core PR-2
// assertion: with floor control on and three `always` responders, the loop
// dispatches one at a time, and each speaker reads its predecessors' replies
// (the whole point of the amendment — no more N mutually-blind replies).
func TestFloorRound_SerializesResponders_MutualVisibility(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := newFloorDispatcher(store, "a", "b", "c")
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	disp.router = router

	id := mustCreateGroupWithPolicies(t, store, "planning", map[string]RespondPolicy{
		"user": RespondNever, "a": RespondAlways, "b": RespondAlways, "c": RespondAlways,
	}, "user", "a", "b", "c")
	router.SetFloorControl(id, true, 2*time.Second)

	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "user", Content: "kickoff",
	}, ""))

	order, peak, history := disp.snapshot()
	require.Equal(t, []string{"a", "b", "c"}, order, "responders dispatched in member order, one at a time")
	assert.Equal(t, 1, peak, "at most one responder dispatch in flight at a time")

	// a saw only the kickoff; b also saw a's reply; c saw a's and b's.
	assert.False(t, containsSender(history["a"], "a"))
	assert.True(t, containsSender(history["b"], "a"), "b must read a's reply")
	assert.True(t, containsSender(history["c"], "a"), "c must read a's reply")
	assert.True(t, containsSender(history["c"], "b"), "c must read b's reply")
}

// TestFloorRound_MentionedFirst pins amendment D3's mentioned-first ordering:
// a stimulus mentioning c grants c the floor before the earlier members.
func TestFloorRound_MentionedFirst(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := newFloorDispatcher(store, "a", "b", "c")
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	disp.router = router

	id := mustCreateGroupWithPolicies(t, store, "planning", map[string]RespondPolicy{
		"user": RespondNever, "a": RespondAlways, "b": RespondAlways, "c": RespondAlways,
	}, "user", "a", "b", "c")
	router.SetFloorControl(id, true, 2*time.Second)

	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "user", Content: "hey @c",
		Mentions: []string{"c"},
	}, ""))

	order, _, _ := disp.snapshot()
	require.Equal(t, []string{"c", "a", "b"}, order, "mentioned responder takes the floor first")
}

// TestFloorRound_DeferredFanout pins amendment D1: a floor-turn reply is
// persisted and visible in history, but it does NOT spawn a competing fanout
// — the loop is the sole dispatcher. With three responders each replying, the
// total dispatch count is exactly three (the stimulus delivered once per
// responder); a re-fanout regression would balloon this.
func TestFloorRound_DeferredFanout(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := newFloorDispatcher(store, "a", "b", "c")
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	disp.router = router

	id := mustCreateGroupWithPolicies(t, store, "planning", map[string]RespondPolicy{
		"user": RespondNever, "a": RespondAlways, "b": RespondAlways, "c": RespondAlways,
	}, "user", "a", "b", "c")
	router.SetFloorControl(id, true, 2*time.Second)

	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "user", Content: "kickoff",
	}, ""))

	order, _, _ := disp.snapshot()
	assert.Len(t, order, 3, "exactly one dispatch per responder; replies must not re-fanout")

	// Each reply is durably persisted (visible to GET /messages), proving
	// the reply was committed even though its fanout was skipped.
	hist, err := store.GetHistory(context.Background(), id, 100, time.Time{})
	require.NoError(t, err)
	got := make(map[string]bool)
	for _, m := range hist {
		got[m.SenderID] = true
	}
	assert.True(t, got["a"] && got["b"] && got["c"], "all three floor-turn replies persisted")
}

// TestFloorRound_TimeoutAdvances pins amendment D2: a responder that never
// replies must not stall the round — the loop advances after the per-turn
// timeout and the next responder still takes the floor.
func TestFloorRound_TimeoutAdvances(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	// Only "b" replies; "a" stays silent and must time out.
	disp := newFloorDispatcher(store, "b")
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	disp.router = router

	id := mustCreateGroupWithPolicies(t, store, "planning", map[string]RespondPolicy{
		"user": RespondNever, "a": RespondAlways, "b": RespondAlways,
	}, "user", "a", "b")
	router.SetFloorControl(id, true, 120*time.Millisecond)

	start := time.Now()
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "user", Content: "kickoff",
	}, ""))
	elapsed := time.Since(start)

	order, _, _ := disp.snapshot()
	require.Equal(t, []string{"a", "b"}, order, "loop advances past the silent responder")
	assert.GreaterOrEqual(t, elapsed, 120*time.Millisecond, "a's turn must wait out the per-turn timeout")
	assert.Less(t, elapsed, 2*time.Second, "round must not hang far beyond a single timeout")
}

// TestFloorRound_SingleResponder_ConcurrentPath confirms that with floor
// control enabled but fewer than two responders, the round is skipped and the
// existing concurrent path runs unchanged (no waiter, no per-turn wait).
func TestFloorRound_SingleResponder_ConcurrentPath(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := newFloorDispatcher(store) // no auto-replies
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	disp.router = router

	id := mustCreateGroupWithPolicies(t, store, "planning", map[string]RespondPolicy{
		"user": RespondNever, "a": RespondAlways,
	}, "user", "a")
	router.SetFloorControl(id, true, 5*time.Second)

	start := time.Now()
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "user", Content: "kickoff",
	}, ""))
	elapsed := time.Since(start)

	order, _, _ := disp.snapshot()
	require.Equal(t, []string{"a"}, order)
	assert.Less(t, elapsed, 1*time.Second, "single responder must not wait on a floor turn")
}

// TestFanout_FloorOff_ConcurrentDispatch confirms the default (flag off):
// with floor control never enabled for the channel, all responders are
// dispatched via the existing concurrent path with no per-turn serialization.
func TestFanout_FloorOff_ConcurrentDispatch(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := newFloorDispatcher(store) // no auto-replies, floor never enabled
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	disp.router = router

	id := mustCreateGroupWithPolicies(t, store, "planning", map[string]RespondPolicy{
		"user": RespondNever, "a": RespondAlways, "b": RespondAlways, "c": RespondAlways,
	}, "user", "a", "b", "c")
	// SetFloorControl intentionally NOT called.

	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "user", Content: "kickoff",
	}, ""))

	order, _, _ := disp.snapshot()
	assert.ElementsMatch(t, []string{"a", "b", "c"}, order, "all responders dispatched on the concurrent path")
}

// lateReplyDispatcher reproduces the in-round late-reply window for amendment
// D1's re-fanout guard. Recipient "a" stays silent through its floor turn so
// the loop advances on the per-turn timeout (D2); then, the moment "b" is
// granted the floor, "a" publishes its reply *synchronously* — modelling a
// slow agent whose REST publish lands while a later speaker still holds the
// floor and the round is still in flight.
//
// The guard must recognise a's reply as belonging to a speaker this round
// already granted the floor and skip its re-fanout. A regression that tracks
// only the *current* turn-holder would treat a's reply as a fresh stimulus and
// re-dispatch to b — the exact N-way amplification floor control exists to
// prevent.
type lateReplyDispatcher struct {
	router *ChannelRouter

	mu       sync.Mutex
	order    []string
	aReplied bool // a's late reply is published exactly once
}

func (d *lateReplyDispatcher) Dispatch(_ context.Context, env DispatchEnvelope, msg ChannelMessage) error {
	rid := env.Recipient.ParticipantID
	d.mu.Lock()
	d.order = append(d.order, rid)
	// Latch the flag BEFORE publishing, not via sync.Once: in the regression
	// case a's reply re-fanouts straight back into Dispatch("b") on this same
	// goroutine, and a sync.Once re-entered from within its own f deadlocks.
	// Setting aReplied first makes that re-entrant call a no-op instead.
	publishLate := rid == "b" && !d.aReplied
	if publishLate {
		d.aReplied = true
	}
	d.mu.Unlock()

	if publishLate {
		// b now holds the floor; a (already timed out) replies late. Publish
		// synchronously so the reply is guaranteed to land mid-round, with b
		// recorded as the current speaker and a as a prior one.
		_ = d.router.Publish(context.Background(), ChannelMessage{
			ID:        uuid.NewString(),
			ChannelID: msg.ChannelID,
			SenderID:  "a",
			Content:   "a late reply",
		}, "")
	}
	return nil
}

func (d *lateReplyDispatcher) snapshot() []string {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]string(nil), d.order...)
}

// TestFloorRound_LateReplyFromTimedOutSpeaker_NoReFanout pins the guard for a
// timed-out speaker's late reply (amendment D1 + D2 interaction): "a" misses
// its turn budget and replies only after the loop has advanced to "b". Because
// "a" was granted the floor in this round, its late reply must be suppressed
// (persisted, but no competing fanout) — so b is dispatched exactly once, not
// re-dispatched by a's reply.
func TestFloorRound_LateReplyFromTimedOutSpeaker_NoReFanout(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &lateReplyDispatcher{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	disp.router = router

	id := mustCreateGroupWithPolicies(t, store, "planning", map[string]RespondPolicy{
		"user": RespondNever, "a": RespondAlways, "b": RespondAlways,
	}, "user", "a", "b")
	router.SetFloorControl(id, true, 100*time.Millisecond)

	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "user", Content: "kickoff",
	}, ""))

	order := disp.snapshot()
	require.Equal(t, []string{"a", "b"}, order,
		"a's late reply must not re-fanout — b is dispatched once, by the round only")

	// The late reply is still durably persisted and visible to history readers
	// — suppression skips the re-fanout, not the commit.
	hist, err := store.GetHistory(context.Background(), id, 100, time.Time{})
	require.NoError(t, err)
	var sawA bool
	for _, m := range hist {
		if m.SenderID == "a" {
			sawA = true
		}
	}
	assert.True(t, sawA, "timed-out speaker's late reply is still persisted")
}
