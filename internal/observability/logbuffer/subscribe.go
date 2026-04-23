package logbuffer

import (
	"sync"
	"sync/atomic"
)

// ─── SSE / live-tail subscription surface (RFC 0018 PR 5) ──────────────────
//
// Subscribers are a fan-out path used by the orchestrator's SSE endpoint
// (GET /api/v1/executions/{id}/logs/stream).  They are *separate* from
// the ring + disk store: a subscriber that cannot keep up loses entries
// (the slot is dropped non-blockingly), but the buffered + persisted
// copy on disk is unaffected.
//
// A subscriber with executionID == "" receives every admitted entry —
// the cross-execution wildcard surfaced as `id=_` on the HTTP path.
//
// Subscriber count is capped per Buffer instance (defaults pulled from
// PERSATRIX_LOGBUFFER_SUBSCRIBERS, see Defaults() / applyDefaults() —
// added in this PR).  The cap protects against a misbehaving client
// fleet exhausting goroutine + memory budget; over-cap Subscribe
// returns ErrSubscriberCapExceeded so the HTTP layer can respond with
// 429 Too Many Requests.

// MaxSubscribersDefault is the default per-Buffer fan-out cap.  Override
// via Config.MaxSubscribers / PERSATRIX_LOGBUFFER_SUBSCRIBERS.
const MaxSubscribersDefault = 64

// SubscribeBuffer is the per-subscriber channel buffer size.  Sized to
// absorb a small burst (≈one ring's worth of fan-out latency) without
// dropping while the reader catches up; smaller would cause spurious
// drops under normal load, larger would let a slow reader retain a
// disproportionate share of buffer memory.
const SubscribeBuffer = 256

// ErrSubscriberCapExceeded is returned by Subscribe when MaxSubscribers
// is reached.  HTTP callers translate to 429 Too Many Requests.
var ErrSubscriberCapExceeded = subscribeError("logbuffer: subscriber cap exceeded")

type subscribeError string

func (e subscribeError) Error() string { return string(e) }

type subscriber struct {
	executionID string // "" → wildcard
	ch          chan Entry
	dropped     atomic.Uint64
}

// Subscribe registers a fan-out channel.  An executionID of "" subscribes
// to every admitted entry; a non-empty value subscribes to that
// execution only.
//
// The returned cancel func is idempotent and safe to call from any
// goroutine.  It MUST be called exactly once when the caller is done
// (typically via defer in the SSE handler) — otherwise the slot leaks
// against MaxSubscribers and the channel is never closed.
//
// The returned channel is closed by cancel(); readers should treat a
// nil/zero Entry from a closed channel as the unsubscribe signal.
func (b *Buffer) Subscribe(executionID string) (<-chan Entry, func(), error) {
	if executionID != "" && !validExecutionID(executionID) {
		return nil, nil, errInvalidExecutionID
	}
	b.subMu.Lock()
	if len(b.subs) >= b.cfg.MaxSubscribers {
		b.subMu.Unlock()
		return nil, nil, ErrSubscriberCapExceeded
	}
	s := &subscriber{
		executionID: executionID,
		ch:          make(chan Entry, SubscribeBuffer),
	}
	b.subs[s] = struct{}{}
	b.subMu.Unlock()

	var once sync.Once
	cancel := func() {
		once.Do(func() {
			b.subMu.Lock()
			if _, ok := b.subs[s]; ok {
				delete(b.subs, s)
			}
			b.subMu.Unlock()
			close(s.ch)
		})
	}
	return s.ch, cancel, nil
}

// SubscriberCount returns the number of live subscribers.  Exposed for
// the SSE handler tests.
func (b *Buffer) SubscriberCount() int {
	b.subMu.Lock()
	n := len(b.subs)
	b.subMu.Unlock()
	return n
}

// broadcast fans an admitted entry out to every interested subscriber.
// Non-blocking: a subscriber whose channel is full has the entry
// dropped and its drop counter incremented.  Called from Append on the
// admit path only.
func (b *Buffer) broadcast(entry Entry) {
	b.subMu.RLock()
	defer b.subMu.RUnlock()
	for s := range b.subs {
		if s.executionID != "" && s.executionID != entry.ExecutionID {
			continue
		}
		select {
		case s.ch <- entry:
		default:
			s.dropped.Add(1)
		}
	}
}
