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
//     even on timeout / context cancellation. The chan is never
//     closed (cap-1 buffered, single-shot send in `Notify`), so a
//     stale `Notify` after `cancel` cannot panic; the
//     `existing == ch` guard in `cancel` exists to prevent
//     **map clobbering** when `Notify` has already removed the entry
//     and a fresh registration on the same key has slotted in
//     (PR #251 review L-3 — doc accuracy).
//
// Concurrency: all operations are protected by a single mutex. The
// expected QPS is bounded by chat-request rate (a single DM is
// caller-side serialised), so contention is not a concern; if it ever
// becomes one, sharding by channelID is a drop-in change.
//
// Scaling constraint (PR #251 review "Should fix #5"): the table is
// **in-process**. If the orchestrator is ever horizontally scaled and
// the agent's REST publish lands on a different replica than the one
// that called [ChannelRouter.PublishAndAwait], the waiter on the
// origin replica never fires and the chat times out. v0.3.0 ships
// single-replica so this is not a release blocker, but a future
// horizontal-scale rollout MUST replace this table with a
// cross-process correlation primitive (e.g. Redis pub/sub keyed on
// the inbound message id) before chat can survive the topology.
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
//
// Single-shot semantics (PR #251 review M-4): the waiter is removed
// from the table on the first matching publish. Subsequent matching
// publishes for the same `(channelID, senderID)` key (e.g. an agent
// emitting `tool_call → tool_result → final_answer` as separate
// `SEND_CHANNEL_MESSAGE`s in response to one chat turn) are still
// persisted by the caller via the store, but they do not satisfy any
// waiter and the chat caller therefore receives only the first
// message. Callers that need multi-message reply semantics must
// either fold the messages agent-side into a single publish or wait
// on the persisted history rather than the in-process waiter.
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
