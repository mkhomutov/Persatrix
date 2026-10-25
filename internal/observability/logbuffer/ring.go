package logbuffer

import "sync"

// executionRing is a fixed-capacity FIFO ring of Entry values for one
// execution. A sealed ring belongs to a finished execution: it still
// admits late entries, and each Seal writes only the entries not yet on
// disk.
type executionRing struct {
	executionID string
	cap         int

	// lastTouch is the LRU stamp written atomically by the
	// getOrCreateRing RLock fast-path. evictLocked reads it under the
	// Buffer's write lock to pick the oldest-touched victim. Kept on
	// the ring (not on the Buffer) so the touch never blocks an
	// admission on another execution.
	lastTouch uint64

	mu        sync.Mutex
	entries   []Entry
	head      int  // index of oldest entry when len(entries) == cap
	full      bool // true once we have wrapped at least once
	sealed    bool
	unflushed int // entries appended since the last write to disk

	// flushMu serialises Seal on this ring so two seals never write
	// the same entries.
	flushMu sync.Mutex

	limiter *tokenBucket
}

func newExecutionRing(executionID string, capacity, ratePerExec int) *executionRing {
	return &executionRing{
		executionID: executionID,
		cap:         capacity,
		entries:     make([]Entry, 0, capacity),
		limiter:     newTokenBucket(ratePerExec),
	}
}

// allow consults the rate limiter; WARN/ERROR are always admitted
// regardless of remaining tokens (matches RFC § E "severity ≥ WARN
// always admitted").
func (r *executionRing) allow(level string) bool {
	if levelGE(level, "WARN") {
		return true
	}
	return r.limiter.allow()
}

// append adds e and reports whether the ring is sealed, i.e. whether e
// arrived after its execution finished and needs another seal to reach
// disk.
func (r *executionRing) append(e Entry) (sealed bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.unflushed++
	if !r.full {
		r.entries = append(r.entries, e)
		if len(r.entries) == r.cap {
			r.full = true
		}
		return r.sealed
	}
	r.entries[r.head] = e
	r.head = (r.head + 1) % r.cap
	return r.sealed
}

func (r *executionRing) snapshot() []Entry {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.snapshotLocked()
}

func (r *executionRing) snapshotLocked() []Entry {
	if !r.full {
		out := make([]Entry, len(r.entries))
		copy(out, r.entries)
		return out
	}
	out := make([]Entry, r.cap)
	copy(out, r.entries[r.head:])
	copy(out[r.cap-r.head:], r.entries[:r.head])
	return out
}

// seal marks the ring sealed and returns, oldest first, the entries not
// yet written to disk, plus the count to pass to markFlushed once they
// are. Entries the ring has already overwritten are gone.
func (r *executionRing) seal() ([]Entry, int) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.sealed = true
	all := r.snapshotLocked()
	return all[len(all)-min(r.unflushed, len(all)):], r.unflushed
}

// markSealed marks the ring as belonging to a finished execution, which
// keeps LRU eviction off it while it holds entries not yet on disk.
func (r *executionRing) markSealed() {
	r.mu.Lock()
	r.sealed = true
	r.mu.Unlock()
}

// hasUnflushed returns true if the ring has been sealed and holds
// entries not yet flushed to disk. Tracked alongside the ring rather
// than inside diskStore so the LRU path can skip eviction without
// touching the disk lock.
func (r *executionRing) hasUnflushed() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.sealed && r.unflushed > 0
}

// markFlushed records that n entries, the count seal returned, are now
// on disk; entries appended since that seal still count as unflushed.
func (r *executionRing) markFlushed(n int) {
	r.mu.Lock()
	r.unflushed -= n
	r.mu.Unlock()
}

// isSealed reports whether the ring has been sealed.
func (r *executionRing) isSealed() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.sealed
}
