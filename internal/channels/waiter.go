package channels

import (
	"errors"
	"sync"
)

// ErrWaiterAlreadyRegistered is returned by [replyWaiter.Register] when
// a waiter for the same `(channelID, senderID)` key is already in
// flight. RFC 0011 PR 4a-ii-β-2: the chat-as-DM façade calls
// `PublishAndAwait` once per inbound user message and a DM has at most
// one in-flight chat at a time, so a duplicate register signals a
// programming error (re-entrant chat handler, leaked waiter) rather
// than a benign race — surface it instead of overwriting silently.
var ErrWaiterAlreadyRegistered = errors.New("channels: reply waiter already registered for key")

// replyWaiter is the orchestrator-side correlation table that powers
// the chat-as-DM publish-and-await flow (RFC 0011 amendment).
//
// Lifecycle:
//
//  1. The chat REST handler calls [ChannelRouter.PublishAndAwait], which
//     calls `Register(channelID, awaitFromAgentID)` to obtain a
//     buffered chan + cancel func *before* the inbound message is
//     published. Registering before publish closes the race where the
//     agent replies faster than the handler can install the waiter.
//  2. The handler awaits the chan (with timeout) for the next
//     `SEND_CHANNEL_MESSAGE` published by `awaitFromAgentID` on the
//     same DM channel.
//  3. When the agent's `_handle_send_channel_message` POSTs the reply
//     to `/api/v1/channels/{id}/messages`, [ChannelRouter.Publish]
//     calls `Notify` after the store commit — matching waiter is
//     resolved and removed.
//  4. The handler always calls `cancel()` (defer) to remove the entry
//     even on timeout / context cancellation; subsequent Notify calls
//     for the cancelled key are no-ops (no closed-channel panic).
//
// Concurrency: all operations are protected by a single mutex. The
// expected QPS is bounded by chat-request rate (a single DM is
// caller-side serialised), so contention is not a concern; if it ever
// becomes one, sharding by channelID is a drop-in change.
type replyWaiter struct {
	mu      sync.Mutex
	waiters map[waiterKey]chan ChannelMessage
}

type waiterKey struct {
	channelID string
	senderID  string
}

func newReplyWaiter() *replyWaiter {
	return &replyWaiter{waiters: make(map[waiterKey]chan ChannelMessage)}
}

// Register installs a waiter for the next message on `channelID` whose
// sender is `senderID`. Returns the receive chan, a cancel func that
// must always be called (defer), and an error if a waiter for the same
// key is already registered.
//
// The chan is buffered with capacity 1 so `Notify` never blocks the
// publish path — it sends-and-continues regardless of whether the
// waiter goroutine has parked on the receive yet.
func (w *replyWaiter) Register(channelID, senderID string) (<-chan ChannelMessage, func(), error) {
	key := waiterKey{channelID: channelID, senderID: senderID}
	w.mu.Lock()
	defer w.mu.Unlock()
	if _, exists := w.waiters[key]; exists {
		return nil, nil, ErrWaiterAlreadyRegistered
	}
	ch := make(chan ChannelMessage, 1)
	w.waiters[key] = ch
	cancel := func() {
		w.mu.Lock()
		defer w.mu.Unlock()
		// Only delete if the entry is still ours — guards against
		// `Notify` removing the entry first then `cancel` clobbering
		// a fresh registration on the same key.
		if existing, ok := w.waiters[key]; ok && existing == ch {
			delete(w.waiters, key)
		}
	}
	return ch, cancel, nil
}

// Notify delivers `msg` to a waiter registered for
// `(msg.ChannelID, msg.SenderID)`, removing the waiter on success.
// Returns true when a waiter was satisfied. Safe to call from the
// publish hot path: the lookup is O(1) and the send is non-blocking
// because the chan is buffered.
func (w *replyWaiter) Notify(msg ChannelMessage) bool {
	key := waiterKey{channelID: msg.ChannelID, senderID: msg.SenderID}
	w.mu.Lock()
	ch, ok := w.waiters[key]
	if ok {
		delete(w.waiters, key)
	}
	w.mu.Unlock()
	if !ok {
		return false
	}
	ch <- msg
	return true
}
