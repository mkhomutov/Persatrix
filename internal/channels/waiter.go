package channels

import (
	"errors"
	"sync"
)

// ErrWaiterAlreadyRegistered is returned by [replyWaiter.Register] when
// a waiter for the same `(channelID, senderID)` key is already in
// flight, whichever request it waits for. RFC 0011 PR 4a-ii-β-2: the chat-as-DM façade calls
// `PublishAndAwait` once per inbound user message and a DM has at most
// one in-flight chat at a time, so a duplicate register signals a
// programming error (re-entrant chat handler, leaked waiter) rather
// than a benign race — surface it instead of overwriting silently.
var ErrWaiterAlreadyRegistered = errors.New("channels: reply waiter already registered for key")

// replyToMetadataKey is the publish-metadata key under which a reply names
// the message it answers (ISSUE-0200). The agent copies the id of the event
// it was sent (`ChannelMessageEvent.message_id`) onto every publish it makes
// in reply on the same channel: `DispatchContext.for_event` takes the id and
// `same_channel_claim` stamps it (agents/dispatch_context.py; the key literal
// is pinned by the cross-language drift test). A publish the agent makes on
// its own, such as one from a timer, answers nothing and carries no such key.
const replyToMetadataKey = "in_reply_to"

// readInReplyTo returns the id of the message a publish says it answers.
// Tolerant like the other wire readers ([readSynthesisReply],
// [readInteractionID]): absent or not a string reads as "", which answers
// nothing.
func readInReplyTo(metadata map[string]any) string {
	if metadata == nil {
		return ""
	}
	id, _ := metadata[replyToMetadataKey].(string)
	return id
}

// replyWaiter is the orchestrator-side correlation table that powers
// the chat-as-DM publish-and-await flow (RFC 0011 amendment).
//
// Lifecycle:
//
//  1. The chat REST handler calls [ChannelRouter.PublishAndAwait], which
//     calls `Register(channelID, awaitFromAgentID, inbound.ID)` to obtain
//     a buffered chan + cancel func *before* the inbound message is
//     published. Registering before publish closes the race where the
//     agent replies faster than the handler can install the waiter.
//  2. The handler awaits the chan (with timeout) for the
//     `SEND_CHANNEL_MESSAGE` that `awaitFromAgentID` publishes on the
//     same DM channel in reply to that inbound message.
//  3. When the agent's `_handle_send_channel_message` POSTs the reply
//     to `/api/v1/channels/{id}/messages`, [ChannelRouter.Publish]
//     calls `Notify` after the store commit — the waiter is resolved
//     and removed when the reply echoes the inbound message's id
//     ([replyToMetadataKey]).
//  4. The handler always calls `cancel()` (defer) to remove the entry
//     even on timeout / context cancellation. The chan is never
//     closed (cap-1 buffered, single-shot send in `Notify`), so a
//     stale `Notify` after `cancel` cannot panic; the
//     `existing.ch == ch` guard in `cancel` exists to prevent
//     **map clobbering** when `Notify` has already removed the entry
//     and a fresh registration on the same key has slotted in.
//
// The echo is what ties a reply to its request (ISSUE-0200). Channel and
// sender alone do not: after one request timed out, the agent's late
// answer to it satisfied whichever request was waiting next, and from
// then on every answer was one question behind. An interaction id cannot
// do it either: one interaction covers every message on a DM until the DM
// goes idle. The floor round ([ChannelRouter.runFloorTurn]) waits the same
// way, under the id of the round's message.
//
// Concurrency: all operations are protected by a single mutex. The
// expected QPS is bounded by chat-request rate (a single DM is
// caller-side serialised), so contention is not a concern; if it ever
// becomes one, sharding by channelID is a drop-in change.
//
// Scaling constraint: the table is **in-process**. If the orchestrator
// is ever horizontally scaled and
// the agent's REST publish lands on a different replica than the one
// that called [ChannelRouter.PublishAndAwait], the waiter on the
// origin replica never fires and the chat times out. v0.3.0 ships
// single-replica so this is not a release blocker, but a future
// horizontal-scale rollout MUST replace this table with a
// cross-process correlation primitive (e.g. Redis pub/sub keyed on
// the inbound message id) before chat can survive the topology.
type replyWaiter struct {
	mu      sync.Mutex
	waiters map[waiterKey]waiterEntry
}

// waiterEntry is one parked request: where its reply goes, and the id of
// the message the reply must echo.
type waiterEntry struct {
	ch        chan ChannelMessage
	requestID string
}

type waiterKey struct {
	channelID string
	senderID  string
}

func newReplyWaiter() *replyWaiter {
	return &replyWaiter{waiters: make(map[waiterKey]waiterEntry)}
}

// Register installs a waiter for the message `senderID` publishes on
// `channelID` in reply to the message `requestID`. Returns the receive
// chan, a cancel func that must always be called (defer), and an error if
// a waiter for the same `(channelID, senderID)` is already registered.
//
// The chan is buffered with capacity 1 so `Notify` never blocks the
// publish path — it sends-and-continues regardless of whether the
// waiter goroutine has parked on the receive yet.
func (w *replyWaiter) Register(channelID, senderID, requestID string) (<-chan ChannelMessage, func(), error) {
	key := waiterKey{channelID: channelID, senderID: senderID}
	w.mu.Lock()
	defer w.mu.Unlock()
	if _, exists := w.waiters[key]; exists {
		return nil, nil, ErrWaiterAlreadyRegistered
	}
	ch := make(chan ChannelMessage, 1)
	w.waiters[key] = waiterEntry{ch: ch, requestID: requestID}
	cancel := func() {
		w.mu.Lock()
		defer w.mu.Unlock()
		// Only delete if the entry is still ours — guards against
		// `Notify` removing the entry first then `cancel` clobbering
		// a fresh registration on the same key.
		if existing, ok := w.waiters[key]; ok && existing.ch == ch {
			delete(w.waiters, key)
		}
	}
	return ch, cancel, nil
}

// Notify delivers `msg` to the waiter registered for
// `(msg.ChannelID, msg.SenderID)` when `msg` echoes the id of the request
// that waiter was registered for, removing the waiter on success. A
// message that echoes another id, or none, satisfies nothing and leaves
// the waiter in place: it is a late answer to an earlier request, or a
// message the sender published on its own. Returns true when a waiter
// was satisfied. Safe to call from the publish hot path: the lookup is
// O(1) and the send is non-blocking because the chan is buffered.
//
// Single-shot semantics: the waiter is removed
// from the table on the first matching publish. Subsequent matching
// publishes for the same `(channelID, senderID)` key (e.g. an agent
// emitting `tool_call → tool_result → final_answer` as separate
// `SEND_CHANNEL_MESSAGE`s in response to one chat turn) are still
// persisted by the caller via the store, but they do not satisfy any
// waiter and the chat caller therefore receives only the first
// message. Callers that need multi-message reply semantics must
// either fold the messages agent-side into a single publish or wait
// on the persisted history rather than the in-process waiter.
// Tracked as `docs/issues/ISSUE-0033` for v0.4 follow-up.
func (w *replyWaiter) Notify(msg ChannelMessage) bool {
	key := waiterKey{channelID: msg.ChannelID, senderID: msg.SenderID}
	inReplyTo := readInReplyTo(msg.Metadata)
	w.mu.Lock()
	entry, ok := w.waiters[key]
	// An empty id matches nothing, on either side: a waiter registered
	// with no request id cannot be answered by a message that echoes none.
	ok = ok && inReplyTo != "" && inReplyTo == entry.requestID
	if ok {
		delete(w.waiters, key)
	}
	w.mu.Unlock()
	if !ok {
		return false
	}
	entry.ch <- msg
	return true
}
