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

// TestReplyWaiter_ConcurrentSameKeyStressNoLeak hammers the
// `(channelID, senderID)` key with interleaved Register / Notify /
// cancel goroutines and asserts (a) no panic from a stale `cancel`
// clobbering a fresh registration, (b) no leaked map entries when
// the dust settles. The identity-equality guard in `cancel`
// (`if existing == ch`) is the defence; without a
// stress test it was unverified that a Notify-then-fresh-Register
// interleave on the same key cannot cross-fire or leak.
//
// The test runs many short Register→cancel cycles in parallel with
// independent Notify calls. After all goroutines finish, the
// waiter map must be empty — a leaked entry would mean either
// `cancel` failed to delete its own slot (regression) or `Notify`
// removed an entry but the next `Register` re-leaked it.
func TestReplyWaiter_ConcurrentSameKeyStressNoLeak(t *testing.T) {
	w := newReplyWaiter()
	const (
		workers    = 16
		iterations = 500
	)
	channelID := "dm:user:agent-x"
	senderID := "agent-x"

	var wg sync.WaitGroup
	wg.Add(workers)
	for i := 0; i < workers; i++ {
		go func() {
			defer wg.Done()
			for j := 0; j < iterations; j++ {
				_, cancel, err := w.Register(channelID, senderID)
				if err != nil {
					// Concurrent worker holds the slot; back off
					// briefly. Either we'll get the slot on retry
					// or our peer will release it. Either way the
					// next iteration is the unit of work, not this
					// one — so just continue.
					continue
				}
				// 50/50 mix of Notify-vs-cancel-resolves the
				// waiter. Both must be safe.
				if j%2 == 0 {
					w.Notify(ChannelMessage{ChannelID: channelID, SenderID: senderID, ID: "stress"})
				}
				cancel()
			}
		}()
	}
	wg.Wait()

	// Final invariant: no leaked entries. A leak here is the loud
	// signal that the identity-equality guard regressed.
	w.mu.Lock()
	leaked := len(w.waiters)
	w.mu.Unlock()
	assert.Equal(t, 0, leaked, "waiter map must be empty after all stress workers finish")
}
