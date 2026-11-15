package logbuffer

import (
	"sync"
	"sync/atomic"

	"go.uber.org/zap"
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
	s, err := b.addSubscriberLocked(executionID)
	b.subMu.Unlock()
	if err != nil {
		return nil, nil, err
	}
	return s.ch, b.cancelFunc(s), nil
}

// SubscribeWithBacklog registers a fan-out channel as Subscribe does and
// also returns the entries the buffer holds for executionID at that
// moment, or for every execution when executionID is "".  The two halves
// meet exactly: an entry admitted before the registration is in the
// backlog, one admitted after it arrives on the channel, and none is in
// both or in neither.
//
// The backlog is what Snapshot returns: a ring's entries when the
// execution has one in memory, otherwise what is on disk.  It is a copy,
// oldest first within each execution; across executions the caller
// sorts.  Rings are copied while Append is held off, which takes as long
// as copying them; disk is read after the lock is released, so a slow
// disk never stalls Append.  (An entry that arrives for an execution
// with no ring and is sealed to disk before that read finishes would be
// in both halves; only an explicit Seal in those microseconds does it.)
func (b *Buffer) SubscribeWithBacklog(executionID string) ([]Entry, <-chan Entry, func(), error) {
	if executionID != "" && !validExecutionID(executionID) {
		return nil, nil, nil, errInvalidExecutionID
	}
	var backlog []Entry
	inMemory := make(map[string]struct{})

	b.subMu.Lock()
	s, err := b.addSubscriberLocked(executionID)
	if err != nil {
		b.subMu.Unlock()
		return nil, nil, nil, err
	}
	b.mu.RLock()
	for id, ring := range b.rings {
		if executionID != "" && id != executionID {
			continue
		}
		inMemory[id] = struct{}{}
		backlog = append(backlog, ring.snapshot()...)
	}
	b.mu.RUnlock()
	b.subMu.Unlock()

	onDisk := []string{executionID}
	if executionID == "" {
		var listErr error
		if onDisk, listErr = b.disk.list(); listErr != nil {
			b.logger.Warn("logbuffer: disk listing failed", zap.Error(listErr))
		}
	}
	for _, id := range onDisk {
		if _, ok := inMemory[id]; !ok {
			backlog = append(backlog, b.disk.read(id)...)
		}
	}
	return backlog, s.ch, b.cancelFunc(s), nil
}

// addSubscriberLocked registers a subscriber for executionID, or refuses
// when the cap is reached.  Caller holds subMu for writing.
func (b *Buffer) addSubscriberLocked(executionID string) (*subscriber, error) {
	if len(b.subs) >= b.cfg.MaxSubscribers {
		return nil, ErrSubscriberCapExceeded
	}
	s := &subscriber{
		executionID: executionID,
		ch:          make(chan Entry, SubscribeBuffer),
	}
	b.subs[s] = struct{}{}
	return s, nil
}

// cancelFunc returns the idempotent function that removes s and closes
// its channel.
func (b *Buffer) cancelFunc(s *subscriber) func() {
	var once sync.Once
	return func() {
		once.Do(func() {
			b.subMu.Lock()
			delete(b.subs, s)
			b.subMu.Unlock()
			close(s.ch)
		})
	}
}

// SubscriberCount returns the number of live subscribers.  Exposed for
// the SSE handler tests.
func (b *Buffer) SubscriberCount() int {
	b.subMu.Lock()
	n := len(b.subs)
	b.subMu.Unlock()
	return n
}

// broadcastLocked fans an admitted entry out to every interested
// subscriber.  Non-blocking: a subscriber whose channel is full has the
// entry dropped and its drop counter incremented.  Called from Append on
// the admit path only, which holds subMu for reading.
func (b *Buffer) broadcastLocked(entry Entry) {
	for s := range b.subs {
		if s.executionID != "" && s.executionID != entry.ExecutionID {
			continue
		}
		select {
		case s.ch <- entry:
		default:
			// Per-subscriber counter for future per-stream metric.
			// PR #173 review Should-Fix #2: also bump the buffer-wide
			// aggregate so Stats()/operators see fan-out pressure
			// without iterating live subscribers.
			s.dropped.Add(1)
			b.droppedSubscribers.Add(1)
		}
	}
}
