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
	participantID        string
	channelID            string
	senderID             string
	respondPolicy        RespondPolicy
	threadParentSenderID string
	// cascadeDepth is the int representation of the inbound
	// `cascade_depth` carried on `msg.Metadata`. Populated by the
	// recording dispatcher so router tests can assert that the
	// per-recipient dispatch event carries the depth verbatim (the +1
	// lives agent-side on outbound; the orchestrator never increments).
	cascadeDepth int
	// closeNotification mirrors the envelope's CP2 marker so Layer 4
	// tests can tell an end-vote close NOTIFICATION from ordinary fanout
	// (the end-vote-close-propagation amendment).
	closeNotification bool
}

func (d *recordingDispatcher) Dispatch(_ context.Context, env DispatchEnvelope, msg ChannelMessage) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.calls = append(d.calls, dispatchCall{
		participantID:        env.Recipient.ParticipantID,
		channelID:            msg.ChannelID,
		senderID:             msg.SenderID,
		respondPolicy:        env.Recipient.RespondPolicy,
		threadParentSenderID: env.ThreadParentSenderID,
		cascadeDepth:         asInt(msg.Metadata["cascade_depth"]),
		closeNotification:    env.InteractionCloseNotification,
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

	chs, err := store.ListChannels(ctx, 0, "")
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

// TestChannelRouter_ReconcileConfig_AtomicOnInvalidMember pins PR #245
// re-review finding (Med): the missing-channel arm of ReconcileConfig
// must adopt the same atomic CreateChannelWithMembers helper that the
// REST handler now uses. Without atomicity, a reconcile that fails
// mid-loop on an invalid declared member leaves the channel row
// committed with only a prefix of the declared membership; the next
// startup then trips ErrConfigStoreMembershipDivergence and requires
// manual operator cleanup.
//
// We exercise the failure path by constructing a Config whose second
// member carries an invalid participant ID (contains the reserved `:`
// character). Config.Validate would normally reject this at load time,
// but ReconcileConfig accepts a *Config directly so the path is
// reachable when callers compose the struct programmatically — and
// future loaders may relax their pre-checks. The contract pinned here
// is that on partial-failure, no orphan channel row survives.
func TestChannelRouter_ReconcileConfig_AtomicOnInvalidMember(t *testing.T) {
	router, _, store := newRouterTest(t)
	ctx := context.Background()

	cfg := &Config{
		MaxChannels: 50,
		Channels: []ChannelConfig{{
			Name: "planning",
			Members: []MemberConfig{
				{ID: "alice", RespondPolicy: RespondAlways},
				{ID: "bad:id", RespondPolicy: RespondAlways}, // invalid → triggers rollback
			},
		}},
	}
	err := router.ReconcileConfig(ctx, cfg)
	require.Error(t, err, "invalid member must surface as a reconcile failure")

	_, getErr := store.GetChannel(ctx, "group:planning")
	assert.ErrorIs(t, getErr, ErrChannelNotFound,
		"failed reconcile must not leak an orphan channel row")
}

// ─── PublishAndAwait — chat-as-DM façade (RFC 0011 PR 4a-ii-β-2) ───

// TestChannelRouter_PublishAndAwait_ReturnsAgentReply pins the
// happy-path contract: the chat handler publishes the user's inbound
// CHANNEL_MESSAGE, the agent's `_handle_send_channel_message` POSTs the
// reply through the same router, and `PublishAndAwait` returns the
// reply message synchronously. Simulates the agent reply by invoking
// `Publish` from a goroutine after the awaiter is registered.
func TestChannelRouter_PublishAndAwait_ReturnsAgentReply(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &recordingDispatcher{}, zap.NewNop(), nil)
	ctx := context.Background()

	// DM channel: user `alice-user` and agent `agent-x`.
	dm, err := store.GetOrCreateDM(ctx, "alice-user", "agent-x")
	require.NoError(t, err)

	inbound := ChannelMessage{
		ID: uuid.NewString(), ChannelID: dm.ID, SenderID: "alice-user", Content: "hi",
	}
	reply := ChannelMessage{
		ID: uuid.NewString(), ChannelID: dm.ID, SenderID: "agent-x", Content: "hello back",
	}

	// Simulate the agent's REST publish landing ~10ms after our publish.
	go func() {
		time.Sleep(10 * time.Millisecond)
		_ = router.Publish(ctx, reply, "")
	}()

	got, err := router.PublishAndAwait(ctx, inbound, "agent-x", time.Second)
	require.NoError(t, err)
	assert.Equal(t, reply.ID, got.ID)
	assert.Equal(t, "hello back", got.Content)
}

// TestChannelRouter_PublishAndAwait_TimesOut pins the deadline contract:
// if no matching reply arrives within `timeout`, returns
// [ErrChatTimeout] and the inbound message is still persisted (the
// user's turn is not lost just because the agent failed to reply).
func TestChannelRouter_PublishAndAwait_TimesOut(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &recordingDispatcher{}, zap.NewNop(), nil)
	ctx := context.Background()

	dm, err := store.GetOrCreateDM(ctx, "alice-user", "agent-x")
	require.NoError(t, err)

	inbound := ChannelMessage{
		ID: uuid.NewString(), ChannelID: dm.ID, SenderID: "alice-user", Content: "hi",
	}
	_, err = router.PublishAndAwait(ctx, inbound, "agent-x", 50*time.Millisecond)
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrChatTimeout)

	// Inbound persisted despite timeout.
	hist, hErr := store.GetHistory(ctx, dm.ID, 10, time.Time{})
	require.NoError(t, hErr)
	require.Len(t, hist, 1, "inbound message must persist even on chat timeout")
	assert.Equal(t, inbound.ID, hist[0].ID)
}

// TestChannelRouter_PublishAndAwait_PublishErrorPropagates pins that a
// publish-side failure (channel-type mismatch, non-member, etc.)
// surfaces to the chat handler instead of being swallowed by the
// timeout — the caller can map it to a 4xx response.
func TestChannelRouter_PublishAndAwait_PublishErrorPropagates(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &recordingDispatcher{}, zap.NewNop(), nil)
	ctx := context.Background()

	// Unknown channel id prefix triggers ErrInvalidChannelType before
	// the store is touched.
	_, err := router.PublishAndAwait(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: "broadcast:unknown", SenderID: "alice-user", Content: "x",
	}, "agent-x", time.Second)
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidChannelType)
	assert.NotErrorIs(t, err, ErrChatTimeout)
}

// TestChannelRouter_PublishAndAwait_IgnoresNonMatchingReply pins that a
// publish from a different sender on the same DM does not satisfy the
// waiter (e.g. an echo of the user's own message via a future
// retransmit path). Only a `SEND_CHANNEL_MESSAGE` originating from the
// awaited agent ID resolves the waiter.
func TestChannelRouter_PublishAndAwait_IgnoresNonMatchingReply(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &recordingDispatcher{}, zap.NewNop(), nil)
	ctx := context.Background()

	dm, err := store.GetOrCreateDM(ctx, "alice-user", "agent-x")
	require.NoError(t, err)

	go func() {
		time.Sleep(10 * time.Millisecond)
		// Wrong sender — must not satisfy the waiter.
		_ = router.Publish(ctx, ChannelMessage{
			ID: uuid.NewString(), ChannelID: dm.ID, SenderID: "alice-user", Content: "echo",
		}, "")
	}()

	_, err = router.PublishAndAwait(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: dm.ID, SenderID: "alice-user", Content: "hi",
	}, "agent-x", 100*time.Millisecond)
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrChatTimeout, "wrong-sender publish must not resolve waiter")
}

// TestChannelRouter_PublishAndAwait_ContextCancelReturnsError pins that
// a cancelled caller context (e.g. client disconnect) tears down the
// waiter promptly without leaking it past the call.
func TestChannelRouter_PublishAndAwait_ContextCancelReturnsError(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &recordingDispatcher{}, zap.NewNop(), nil)
	parent := context.Background()

	dm, err := store.GetOrCreateDM(parent, "alice-user", "agent-x")
	require.NoError(t, err)

	ctx, cancel := context.WithCancel(parent)
	cancel() // cancel before the call
	_, err = router.PublishAndAwait(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: dm.ID, SenderID: "alice-user", Content: "hi",
	}, "agent-x", time.Second)
	require.Error(t, err)
	assert.ErrorIs(t, err, context.Canceled)
}

// TestChannelRouter_PublishAndAwait_RejectsSelfReply pins the
// defense-in-depth guard against `awaitFromAgentID == msg.SenderID`.
//
// Why a separate guard, given that `ChannelStore.GetOrCreateDM` already
// rejects `user == agent` upstream? `PublishAndAwait` is now part of
// the channels package's public surface and will likely gain other
// callers (workflow steps that "ask an agent and wait", future
// scheduler probes, integration tests). If any of them passes the same
// id for sender and awaited-from, the inbound publish would satisfy
// its OWN waiter via the `Publish` → `Notify` path (Notify keys on
// `(channelID, senderID)`) and the call would return the caller's
// inbound message AS the "reply" — silent self-reply with no error.
//
// The fix returns `ErrInvalidParticipantID` BEFORE Register/Publish
// run, so:
//
//   - no waiter slot is consumed (no risk of leaking a never-resolved
//     entry in the table);
//   - no message is persisted (consistent with other early-validation
//     errors in this package);
//   - the chat handler's existing `errors.Is(err,
//     channels.ErrInvalidParticipantID)` arm in [server/chat_handler.go]
//     maps the failure to 400 BAD_REQUEST — the right envelope, since
//     the failure is a caller-side id-hygiene problem, not server I/O.
func TestChannelRouter_PublishAndAwait_RejectsSelfReply(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &recordingDispatcher{}, zap.NewNop(), nil)
	ctx := context.Background()

	// Use a real DM so the failure cannot be confused with any
	// channel-existence or membership check; the only thing that
	// should make this call fail is the self-reply guard.
	dm, err := store.GetOrCreateDM(ctx, "alice-user", "agent-x")
	require.NoError(t, err)

	// Sender == awaitFromAgentID. The guard MUST fire before any
	// store mutation.
	_, err = router.PublishAndAwait(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: dm.ID, SenderID: "agent-x", Content: "hi",
	}, "agent-x", time.Second)
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidParticipantID,
		"self-reply must surface as ErrInvalidParticipantID so the chat handler's existing arm maps it to 400")

	// Defense-in-depth: nothing should have been persisted on the DM,
	// because the guard must run BEFORE the inner Publish call. A
	// stray persisted row would mean the guard was placed downstream
	// of the store commit and is therefore not actually preventing
	// the silent self-reply path.
	hist, hErr := store.GetHistory(ctx, dm.ID, 10, time.Time{})
	require.NoError(t, hErr)
	assert.Empty(t, hist, "self-reply guard must short-circuit before the inner Publish persists")
}
