package channels

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// recordingDispatcher captures every Dispatch call so router tests can
// assert the fanout topology without booting gRPC.
type recordingDispatcher struct {
	mu    sync.Mutex
	calls []dispatchCall
	err   error // when set, every Dispatch returns this
}

type dispatchCall struct {
	participantID string
	channelID     string
	senderID      string
}

func (d *recordingDispatcher) Dispatch(_ context.Context, participantID string, msg ChannelMessage) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.calls = append(d.calls, dispatchCall{
		participantID: participantID,
		channelID:     msg.ChannelID,
		senderID:      msg.SenderID,
	})
	return d.err
}

func (d *recordingDispatcher) snapshot() []dispatchCall {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]dispatchCall, len(d.calls))
	copy(out, d.calls)
	return out
}

func newRouterTest(t *testing.T) (*ChannelRouter, *recordingDispatcher, ChannelStore) {
	t.Helper()
	store := newTestStore(t, SQLiteOptions{})
	disp := &recordingDispatcher{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	return router, disp, store
}

// TestChannelRouter_Publish_FanoutFiltersSender pins the §C contract that
// the publisher does not receive its own message back.
func TestChannelRouter_Publish_FanoutFiltersSender(t *testing.T) {
	router, disp, store := newRouterTest(t)
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob", "carol")

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "hi",
	}, ""))

	calls := disp.snapshot()
	require.Len(t, calls, 2, "fanout to bob+carol, sender filtered")
	got := map[string]bool{}
	for _, c := range calls {
		got[c.participantID] = true
		assert.Equal(t, "alice", c.senderID)
	}
	assert.True(t, got["bob"])
	assert.True(t, got["carol"])
	assert.False(t, got["alice"], "sender must not receive own message")
}

// TestChannelRouter_Publish_RejectsChannelTypeMismatch pins RFC 0011 §C
// "channel_type proto-field redundancy": orchestrator MUST validate
// agreement between `channel_type` and the `channel_id` prefix.
func TestChannelRouter_Publish_RejectsChannelTypeMismatch(t *testing.T) {
	router, _, store := newRouterTest(t)
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice")

	err := router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "x",
	}, "dm") // declared dm but id starts with group:
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidChannelType)

	// Verify nothing was persisted (mismatch must short-circuit before store).
	hist, hErr := store.GetHistory(ctx, id, 10, time.Time{})
	require.NoError(t, hErr)
	assert.Empty(t, hist, "rejected publish must not persist")
}

// TestChannelRouter_Publish_RejectsUnknownPrefix pins the same contract
// against an id whose prefix is not a known ChannelType.
func TestChannelRouter_Publish_RejectsUnknownPrefix(t *testing.T) {
	router, _, _ := newRouterTest(t)
	err := router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: "broadcast:planning", SenderID: "alice", Content: "x",
	}, "")
	assert.ErrorIs(t, err, ErrInvalidChannelType)
}

// TestChannelRouter_Publish_NonMemberRejected pins ErrNotMember
// propagation from the store.
func TestChannelRouter_Publish_NonMemberRejected(t *testing.T) {
	router, disp, store := newRouterTest(t)
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice")
	err := router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "bob", Content: "x",
	}, "group")
	assert.ErrorIs(t, err, ErrNotMember)
	assert.Empty(t, disp.snapshot(), "rejected publish must not fan out")
}

// TestChannelRouter_Publish_RespondNeverNotDispatched pins the optimisation
// that `respond: never` members are skipped at the dispatcher seam (the
// canonical enforcement remains in the response gate, deferred to PR 4).
func TestChannelRouter_Publish_RespondNeverNotDispatched(t *testing.T) {
	router, disp, store := newRouterTest(t)
	ctx := context.Background()
	id := "group:planning"
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: id, Name: "planning", Type: ChannelTypeGroup,
	}))
	require.NoError(t, store.AddMember(ctx, id, "alice", RespondAlways))
	require.NoError(t, store.AddMember(ctx, id, "bob", RespondNever))
	require.NoError(t, store.AddMember(ctx, id, "carol", RespondWhenMentioned))

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "x",
	}, ""))

	calls := disp.snapshot()
	require.Len(t, calls, 1)
	assert.Equal(t, "carol", calls[0].participantID, "bob (respond:never) must be skipped")
}

// TestChannelRouter_Publish_DispatchErrorDoesNotFailPublish pins the
// fire-and-forget contract: dispatcher errors are logged but the publish
// (already persisted) is reported as success to the caller.
func TestChannelRouter_Publish_DispatchErrorDoesNotFailPublish(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &recordingDispatcher{err: errors.New("boom")}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob")

	err := router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "x",
	}, "")
	require.NoError(t, err, "dispatch errors must not fail publish")
	assert.Len(t, disp.snapshot(), 1, "dispatch was attempted")
}

// TestChannelRouter_ReconcileConfig_CreatesMissingChannels pins the
// startup path: config-declared channels missing from the store are
// created with their declared members.
func TestChannelRouter_ReconcileConfig_CreatesMissingChannels(t *testing.T) {
	router, _, store := newRouterTest(t)
	ctx := context.Background()

	cfg := &Config{
		MaxChannels: 50,
		Channels: []ChannelConfig{{
			Name:        "planning",
			Description: "x",
			Members: []MemberConfig{
				{ID: "alice", RespondPolicy: RespondAlways},
				{ID: "bob", RespondPolicy: RespondWhenMentioned},
			},
		}},
	}
	require.NoError(t, router.ReconcileConfig(ctx, cfg))

	ch, err := store.GetChannel(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, "planning", ch.Name)

	members, err := store.GetMembers(ctx, "group:planning")
	require.NoError(t, err)
	require.Len(t, members, 2)
}

// TestChannelRouter_ReconcileConfig_PreservesRESTCreatedChannels pins the
// §B coexistence rule: store rows that do not appear in config are NOT
// removed.
func TestChannelRouter_ReconcileConfig_PreservesRESTCreatedChannels(t *testing.T) {
	router, _, store := newRouterTest(t)
	ctx := context.Background()

	// Pre-existing REST-created channel.
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:adhoc", Name: "adhoc", Type: ChannelTypeGroup,
	}))
	require.NoError(t, store.AddMember(ctx, "group:adhoc", "alice", RespondAlways))

	require.NoError(t, router.ReconcileConfig(ctx, &Config{MaxChannels: 50}))

	chs, err := store.ListChannels(ctx)
	require.NoError(t, err)
	require.Len(t, chs, 1, "REST-created channel must survive reconcile")
	assert.Equal(t, "group:adhoc", chs[0].ID)
}

// TestChannelRouter_ReconcileConfig_LoudFailureOnDivergence pins the
// §B loud-failure path: a config-declared channel whose member set
// disagrees with the store surfaces ErrConfigStoreMembershipDivergence
// listing the divergent ids.
func TestChannelRouter_ReconcileConfig_LoudFailureOnDivergence(t *testing.T) {
	router, _, store := newRouterTest(t)
	ctx := context.Background()

	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:planning", Name: "planning", Type: ChannelTypeGroup,
	}))
	require.NoError(t, store.AddMember(ctx, "group:planning", "alice", RespondAlways))
	require.NoError(t, store.AddMember(ctx, "group:planning", "intruder", RespondAlways))

	cfg := &Config{
		MaxChannels: 50,
		Channels: []ChannelConfig{{
			Name: "planning",
			Members: []MemberConfig{
				{ID: "alice", RespondPolicy: RespondAlways},
				{ID: "bob", RespondPolicy: RespondAlways},
			},
		}},
	}
	err := router.ReconcileConfig(ctx, cfg)
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrConfigStoreMembershipDivergence)
	// The error message must list both divergent ids so an operator can
	// reconcile without further log digging.
	assert.Contains(t, err.Error(), "intruder", "extra-store member should appear")
	assert.Contains(t, err.Error(), "bob", "missing-from-store member should appear")
}
