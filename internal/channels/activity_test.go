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

	"github.com/mkhomutov/persatrix/internal/defaults"
)

// Channel-activity tracking (RFC 0048 console presence Tier 1): the router marks
// the members a publish is expected to draw a reply from — the responders, NOT
// the ingestion-only recipients — and clears each when its reply re-enters or
// when the TTL backstop fires. `GET /channels/{id}/activity` reads this so the
// web console shows an accurate "… is thinking" for every trigger, not only the
// turns it fired itself (Tier 0). These tests pin the router half end to end:
// Publish drives fanout synchronously, so the mark is visible on return.

func TestChannelActivity_MarksMentionedRespondersOnPublish(t *testing.T) {
	router, _, store := newRouterTest(t)
	ctx := context.Background()
	// All three agents are when_mentioned (mustCreateGroup's policy). alice
	// addresses two of them; the unmentioned one ingests the message but will
	// not reply, so it must NOT be marked as thinking.
	id := mustCreateGroup(t, store, "planning", "alice", "ember-owl", "crimson-fox", "quiet-mouse")

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
		Content:  "@ember-owl @crimson-fox your reads?",
		Mentions: []string{"ember-owl", "crimson-fox"},
	}, ""))

	assert.Equal(t, []string{"crimson-fox", "ember-owl"}, router.ChannelActivity(id),
		"only the two addressed responders are thinking; the unmentioned member is ingestion-only")
}

func TestChannelActivity_MarksAlwaysRespondersOnBroadcast(t *testing.T) {
	router, _, store := newRouterTest(t)
	ctx := context.Background()
	id := "group:standup"
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: id, Name: "standup", Type: ChannelTypeGroup}))
	require.NoError(t, store.AddMember(ctx, id, "alice", RespondNever))
	require.NoError(t, store.AddMember(ctx, id, "ember-owl", RespondAlways))
	require.NoError(t, store.AddMember(ctx, id, "crimson-fox", RespondAlways))

	// An undirected broadcast (no mentions) draws every always-responder.
	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "standup time",
	}, ""))

	assert.Equal(t, []string{"crimson-fox", "ember-owl"}, router.ChannelActivity(id))
}

func TestChannelActivity_ClearsResponderOnReply(t *testing.T) {
	router, _, store := newRouterTest(t)
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "ember-owl", "crimson-fox")

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
		Content:  "@ember-owl @crimson-fox your reads?",
		Mentions: []string{"ember-owl", "crimson-fox"},
	}, ""))
	require.Equal(t, []string{"crimson-fox", "ember-owl"}, router.ChannelActivity(id))

	// ember-owl's reply re-enters via Publish (sender = the agent) — it clears
	// from the thinking set; crimson-fox is still pending.
	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "ember-owl", Content: "my read is…",
	}, ""))

	assert.Equal(t, []string{"crimson-fox"}, router.ChannelActivity(id))
}

func TestChannelActivity_TTLExpiryClearsAStrandedResponder(t *testing.T) {
	router, _, store := newRouterTest(t)
	ctx := context.Background()
	now := time.Now()
	router.activityNow = func() time.Time { return now } // deterministic clock

	id := mustCreateGroup(t, store, "planning", "alice", "ember-owl")
	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
		Content: "@ember-owl ?", Mentions: []string{"ember-owl"},
	}, ""))
	require.Equal(t, []string{"ember-owl"}, router.ChannelActivity(id))

	// The agent never replies; once the entry ages past the TTL the read prunes
	// it, so a declined/silent turn can't strand the indicator (the
	// fire-and-forget fanout has no server-side await to clear it).
	now = now.Add(activityTTL + time.Second)
	assert.Empty(t, router.ChannelActivity(id))
}

func TestChannelActivity_EmptyForUntouchedChannel(t *testing.T) {
	router, _, _ := newRouterTest(t)
	assert.Empty(t, router.ChannelActivity("group:never-used"))
}

// TestChannelActivity_ClearsClosingVoterOnInteractionEnd pins the regression:
// the clear-on-reply seam must run BEFORE publishCommit's fanout-suppression
// early returns. An end-vote that reaches quorum CLOSES the interaction, so
// processEndVote returns true and publishCommit returns early — if the clear
// sits after that return, the agent whose vote just ended the conversation
// strands in the "thinking" set for the full TTL, exactly when the console
// should show no one thinking.
func TestChannelActivity_ClearsClosingVoterOnInteractionEnd(t *testing.T) {
	router, _, store := newRouterTest(t)
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "ember-owl", "crimson-fox")

	// alice opens a tracked interaction addressing both agents; fanout marks them.
	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
		Content:  "@ember-owl @crimson-fox wrap up?",
		Mentions: []string{"ember-owl", "crimson-fox"},
		Metadata: map[string]any{interactionIDMetadataKey: "i1"},
	}, ""))
	require.Equal(t, []string{"crimson-fox", "ember-owl"}, router.ChannelActivity(id))

	// First end-vote (quorum K=2 not yet reached): processEndVote returns false,
	// fanout proceeds, ember-owl clears on the normal path.
	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "ember-owl",
		Content:  "agreed, done",
		Metadata: map[string]any{interactionIDMetadataKey: "i1", endVoteMetadataKey: true},
	}, ""))
	require.Equal(t, []string{"crimson-fox"}, router.ChannelActivity(id))

	// Second end-vote reaches quorum and CLOSES the interaction — publishCommit
	// suppresses fanout and returns early. The closing voter must still clear.
	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "crimson-fox",
		Content:  "yep, closing",
		Metadata: map[string]any{interactionIDMetadataKey: "i1", endVoteMetadataKey: true},
	}, ""))

	assert.Empty(t, router.ChannelActivity(id),
		"the agent whose vote closed the interaction must clear, not strand until the TTL")
}

// TestChannelActivity_ClearsResponderWhenReplyHitsCascadeCap pins the same
// ordering invariant for the OTHER early return in publishCommit: a reply that
// lands at the cascade-depth cap drops fanout and returns early. The responder
// answered, so it is no longer thinking even though its reply will not cascade
// further — the clear must precede the cap return.
func TestChannelActivity_ClearsResponderWhenReplyHitsCascadeCap(t *testing.T) {
	router, _, store := newRouterTest(t)
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "ember-owl")

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
		Content: "@ember-owl ?", Mentions: []string{"ember-owl"},
	}, ""))
	require.Equal(t, []string{"ember-owl"}, router.ChannelActivity(id))

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "ember-owl", Content: "done",
		Metadata: map[string]any{cascadeDepthMetadataKey: defaults.DefaultMaxCascadeDepth},
	}, ""))

	assert.Empty(t, router.ChannelActivity(id),
		"a reply capped at max cascade depth still clears the responder")
}

// floorActivityProbe drives a deterministic two-speaker floor round while
// stepping the activity clock between turns, so the test can prove a late
// speaker is (re)marked when its turn is actually granted — not only stamped
// once at round start. The first speaker's dispatch advances the clock past the
// TTL (simulating a long turn) and then auto-replies so the loop advances; at
// the second speaker's dispatch it snapshots what the console would read.
type floorActivityProbe struct {
	router *ChannelRouter

	mu       sync.Mutex
	now      time.Time
	snapshot []string
	probed   bool
}

func (p *floorActivityProbe) nowFn() time.Time {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.now
}

func (p *floorActivityProbe) Dispatch(_ context.Context, env DispatchEnvelope, msg ChannelMessage) error {
	rid := env.Recipient.ParticipantID
	switch rid {
	case "ember-owl": // first speaker — burn more than the TTL, then reply
		p.mu.Lock()
		p.now = p.now.Add(activityTTL + time.Second)
		p.mu.Unlock()
	case "crimson-fox": // second speaker — record the thinking set at its turn
		snap := p.router.ChannelActivity(msg.ChannelID)
		p.mu.Lock()
		p.snapshot = snap
		p.probed = true
		p.mu.Unlock()
	}
	// Auto-reply on an independent goroutine, mirroring the agent's async REST
	// publish, so the floor loop advances via the waiter rather than the timeout.
	go func() {
		_ = p.router.Publish(context.Background(), ChannelMessage{
			ID: uuid.NewString(), ChannelID: msg.ChannelID, SenderID: rid, Content: rid + " reply",
		}, "")
	}()
	return nil
}

// TestChannelActivity_FloorSpeakerRemarkedAtItsTurn pins finding #2: in a
// serialized floor round the responders are marked once at round start, but the
// TTL is sized for a single turn. A late speaker whose queue wait exceeds the
// TTL would be pruned from the indicator while it is actually its turn to think,
// unless the round re-marks it when the floor is granted. The probe advances the
// clock past the TTL during the first speaker's turn; the second speaker must
// still read as thinking at its own dispatch.
func TestChannelActivity_FloorSpeakerRemarkedAtItsTurn(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	probe := &floorActivityProbe{now: time.Now()}
	router := NewChannelRouter(store, probe, zap.NewNop(), nil)
	probe.router = router
	router.activityNow = probe.nowFn

	id := mustCreateGroup(t, store, "planning", "alice", "ember-owl", "crimson-fox")
	router.SetFloorControl(id, true, 2*time.Second)

	// Publish is synchronous: it runs the whole floor round inline, blocking on
	// each speaker's reply, so the probe's snapshot is set by the time it returns.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
		Content:  "@ember-owl @crimson-fox round?",
		Mentions: []string{"ember-owl", "crimson-fox"},
	}, ""))
	router.WaitForPendingFanout()

	probe.mu.Lock()
	defer probe.mu.Unlock()
	require.True(t, probe.probed, "second speaker was dispatched")
	assert.Contains(t, probe.snapshot, "crimson-fox",
		"a late floor speaker must read as thinking at its turn, even past the round-start TTL")
}
