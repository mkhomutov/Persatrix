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
	// keep, when set, is asked about an admitted entry before it takes a
	// place in the subscriber's queue.
	keep func(Entry) bool

	// The hold of a subscriber made by SubscribeWithBacklog.  While
	// holding is set, entries wait in held and not in ch, so that the
	// caller can send the backlog first however long that takes.
	holding atomic.Bool
	holdMu  sync.Mutex
	held    []Entry
	holdCap int
}

// offer gives the subscriber one admitted entry that it keeps, and
// reports whether there was room for it.
func (s *subscriber) offer(entry Entry) bool {
	if s.holding.Load() {
		s.holdMu.Lock()
		if s.holding.Load() {
			room := len(s.held) < s.holdCap
			if room {
				s.held = append(s.held, entry)
			}
			s.holdMu.Unlock()
			return room
		}
		s.holdMu.Unlock()
	}
	select {
	case s.ch <- entry:
		return true
	default:
		return false
	}
}

// Subscription is a registered subscriber: what Subscribe gives, with
// the filter, the count of lost entries and, from SubscribeWithBacklog,
// the buffered entries and the hold.
type Subscription struct {
	// Backlog holds the entries the buffer had when the subscriber was
	// registered.  Nil unless SubscribeWithBacklog made the subscription.
	Backlog []Entry
	// Live delivers the entries admitted after the registration, once
	// the hold has ended (see Held).  Cancel closes it.
	Live <-chan Entry

	sub    *subscriber
	cancel func()
}

// Held returns the live entries that have waited since the last call,
// oldest first.  When none has, it ends the hold and returns nil: from
// that moment entries arrive on Live.  A caller sends the backlog, then
// calls Held until it returns nil, then reads Live; every entry admitted
// after the registration is then in exactly one of the two, in order.
//
// A subscription made without a backlog never holds: Held returns nil.
func (s *Subscription) Held() []Entry {
	s.sub.holdMu.Lock()
	defer s.sub.holdMu.Unlock()
	held := s.sub.held
	s.sub.held = nil
	if len(held) == 0 {
		s.sub.holding.Store(false)
	}
	return held
}

// Dropped returns how many entries this subscriber has lost so far
// because its hold or its channel was full.
func (s *Subscription) Dropped() uint64 { return s.sub.dropped.Load() }

// Cancel removes the subscriber and closes Live.  It is idempotent and
// must be called once the caller is done, as Subscribe's cancel func.
func (s *Subscription) Cancel() { s.cancel() }

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
	s, err := b.SubscribeFiltered(executionID, nil)
	if err != nil {
		return nil, nil, err
	}
	return s.Live, s.Cancel, nil
}

// SubscribeFiltered is Subscribe for a subscriber that wants only some
// entries.  keep is asked about every admitted entry of the execution
// before the entry takes a place in the subscriber's channel; an entry
// it refuses is not queued and not counted as lost, so entries the
// subscriber does not want cannot push out the ones it does.  A nil keep
// keeps every entry.
//
// keep runs inside Append: it must be quick and must not call the buffer.
func (b *Buffer) SubscribeFiltered(executionID string, keep func(Entry) bool) (*Subscription, error) {
	if executionID != "" && !validExecutionID(executionID) {
		return nil, errInvalidExecutionID
	}
	b.subMu.Lock()
	s, err := b.addSubscriberLocked(executionID, keep, 0)
	b.subMu.Unlock()
	if err != nil {
		return nil, err
	}
	return b.subscription(s, nil), nil
}

// SubscribeWithBacklog registers a subscriber as SubscribeFiltered does
// and also returns the entries the buffer holds for executionID at that
// moment, or for every execution when executionID is "".  The two halves
// meet exactly: an entry admitted before the registration is in the
// backlog, one admitted after it is live, and none is in both or in
// neither.
//
// The live entries do not go to the channel at once.  Up to hold of them
// wait in memory, from the registration on, until the caller has sent
// the backlog and Held has run dry; an entry that arrives when hold are
// waiting is lost and counted (Dropped).  Reading the backlog from disk
// and writing it to a client can take seconds, far longer than the
// channel's SubscribeBuffer entries cover.
//
// The backlog is what Snapshot returns: a ring's entries when the
// execution has one in memory, otherwise what is on disk.  It is a copy,
// oldest first within each execution; across executions the caller
// sorts.  Rings are copied while Append is held off, which takes as long
// as copying them; disk is read after the lock is released, so a slow
// disk never stalls Append.  (An entry that arrives for an execution
// with no ring and is sealed to disk before that read finishes would be
// in both halves.  Only an explicit Seal during the read does it, and
// for every execution at once the read lasts as long as the disk store
// takes to read: seconds when it is full.)
func (b *Buffer) SubscribeWithBacklog(executionID string, keep func(Entry) bool, hold int) (*Subscription, error) {
	if executionID != "" && !validExecutionID(executionID) {
		return nil, errInvalidExecutionID
	}
	var backlog []Entry
	inMemory := make(map[string]struct{})

	b.subMu.Lock()
	s, err := b.addSubscriberLocked(executionID, keep, hold)
	if err != nil {
		b.subMu.Unlock()
		return nil, err
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
	return b.subscription(s, backlog), nil
}

// addSubscriberLocked registers a subscriber for executionID, or refuses
// when the cap is reached.  A hold above zero starts it holding.  Caller
// holds subMu for writing.
func (b *Buffer) addSubscriberLocked(executionID string, keep func(Entry) bool, hold int) (*subscriber, error) {
	if len(b.subs) >= b.cfg.MaxSubscribers {
		return nil, ErrSubscriberCapExceeded
	}
	s := &subscriber{
		executionID: executionID,
		ch:          make(chan Entry, SubscribeBuffer),
		keep:        keep,
		holdCap:     hold,
	}
	s.holding.Store(hold > 0)
	b.subs[s] = struct{}{}
	return s, nil
}

// subscription wraps a registered subscriber for its caller.
func (b *Buffer) subscription(s *subscriber, backlog []Entry) *Subscription {
	return &Subscription{Backlog: backlog, Live: s.ch, sub: s, cancel: b.cancelFunc(s)}
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
// subscriber.  Non-blocking: a subscriber with no room has the entry
// dropped and its drop counter incremented.  Called from Append on the
// admit path only, which holds subMu for reading.
func (b *Buffer) broadcastLocked(entry Entry) {
	for s := range b.subs {
		if s.executionID != "" && s.executionID != entry.ExecutionID {
			continue
		}
		if s.keep != nil && !s.keep(entry) {
			continue
		}
		if !s.offer(entry) {
			// PR #173 review Should-Fix #2: also bump the buffer-wide
			// aggregate so Stats()/operators see fan-out pressure
			// without iterating live subscribers.
			s.dropped.Add(1)
			b.droppedSubscribers.Add(1)
		}
	}
}
