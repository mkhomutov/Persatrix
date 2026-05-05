package channels

import (
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestReplyWaiter_RegisterAndNotify pins the happy-path contract: a
// waiter registered on (channelID, awaitFromAgentID) is satisfied by the
// next matching ChannelMessage and the chan delivers exactly that
// message. RFC 0011 PR 4a-ii-β-2 — chat-as-DM publish-and-await.
func TestReplyWaiter_RegisterAndNotify(t *testing.T) {
	w := newReplyWaiter()
	ch, cancel, err := w.Register("dm:user:agent-x", "agent-x")
	require.NoError(t, err)
	defer cancel()

	msg := ChannelMessage{ID: "m1", ChannelID: "dm:user:agent-x", SenderID: "agent-x", Content: "hi"}
	delivered := w.Notify(msg)
	assert.True(t, delivered, "matching publish must satisfy the waiter")

	select {
	case got := <-ch:
		assert.Equal(t, msg.ID, got.ID)
		assert.Equal(t, msg.Content, got.Content)
	case <-time.After(time.Second):
		t.Fatal("waiter chan did not deliver")
	}
}

// TestReplyWaiter_NotifyNoMatch — Notify on a key without a registered
// waiter is a no-op (returns false) and does not block. This keeps the
// publish path cheap when no chat is in flight.
func TestReplyWaiter_NotifyNoMatch(t *testing.T) {
	w := newReplyWaiter()
	delivered := w.Notify(ChannelMessage{ChannelID: "dm:a:b", SenderID: "b"})
	assert.False(t, delivered)
}

// TestReplyWaiter_NotifySenderMismatch — a publish from a different
// sender on the same DM channel does not satisfy the waiter (the chat
// handler is awaiting the agent's reply, not an echo of the user's own
// message).
func TestReplyWaiter_NotifySenderMismatch(t *testing.T) {
	w := newReplyWaiter()
	ch, cancel, err := w.Register("dm:user:agent-x", "agent-x")
	require.NoError(t, err)
	defer cancel()

	delivered := w.Notify(ChannelMessage{ChannelID: "dm:user:agent-x", SenderID: "user", Content: "echo"})
	assert.False(t, delivered)

	select {
	case got := <-ch:
		t.Fatalf("waiter must not fire on sender mismatch (got %+v)", got)
	case <-time.After(50 * time.Millisecond):
		// expected: nothing delivered
	}
}

// TestReplyWaiter_DuplicateRegisterRejected — concurrent SendChatMessage
// calls for the same (channel, agent) pair are caller-side serialised by
// design (a DM has one in-flight chat at a time), but defensively reject
// the second register so a programming error surfaces loud rather than
// silently dropping the second waiter.
func TestReplyWaiter_DuplicateRegisterRejected(t *testing.T) {
	w := newReplyWaiter()
	_, cancel1, err := w.Register("dm:user:agent-x", "agent-x")
	require.NoError(t, err)
	defer cancel1()

	_, _, err = w.Register("dm:user:agent-x", "agent-x")
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrWaiterAlreadyRegistered)
}

// TestReplyWaiter_CancelClearsRegistration — a cancelled waiter is
// removed so a subsequent Register on the same key succeeds (covers the
// next chat call after a timeout).
func TestReplyWaiter_CancelClearsRegistration(t *testing.T) {
	w := newReplyWaiter()
	_, cancel, err := w.Register("dm:user:agent-x", "agent-x")
	require.NoError(t, err)
	cancel()

	_, cancel2, err := w.Register("dm:user:agent-x", "agent-x")
	require.NoError(t, err)
	defer cancel2()
}

// TestReplyWaiter_NotifyAfterCancelNoOp — a publish that arrives after
// the chat handler has timed out and cancelled does not panic on a
// closed channel and returns false.
func TestReplyWaiter_NotifyAfterCancelNoOp(t *testing.T) {
	w := newReplyWaiter()
	_, cancel, err := w.Register("dm:user:agent-x", "agent-x")
	require.NoError(t, err)
	cancel()

	delivered := w.Notify(ChannelMessage{ChannelID: "dm:user:agent-x", SenderID: "agent-x"})
	assert.False(t, delivered, "post-cancel notify must be a no-op")
}

// TestReplyWaiter_ConcurrentDistinctKeys — independent DMs do not
// interfere; a publish for one waiter must not fire the other.
func TestReplyWaiter_ConcurrentDistinctKeys(t *testing.T) {
	w := newReplyWaiter()
	chA, cancelA, err := w.Register("dm:user-a:agent-x", "agent-x")
	require.NoError(t, err)
	defer cancelA()
	chB, cancelB, err := w.Register("dm:user-b:agent-x", "agent-x")
	require.NoError(t, err)
	defer cancelB()

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		w.Notify(ChannelMessage{ID: "mA", ChannelID: "dm:user-a:agent-x", SenderID: "agent-x"})
	}()
	wg.Wait()

	select {
	case got := <-chA:
		assert.Equal(t, "mA", got.ID)
	case <-time.After(time.Second):
		t.Fatal("waiter A did not fire")
	}
	select {
	case got := <-chB:
		t.Fatalf("waiter B fired unexpectedly: %+v", got)
	case <-time.After(50 * time.Millisecond):
		// expected
	}
}
