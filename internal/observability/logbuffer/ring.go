package logbuffer

import "sync"

// executionRing is a fixed-capacity FIFO ring of Entry values for one
// execution. Sealed rings are immutable (further append is a no-op).
type executionRing struct {
	executionID string
	cap         int

	mu      sync.Mutex
	entries []Entry
	head    int  // index of oldest entry when len(entries) == cap
	full    bool // true once we have wrapped at least once
	sealed  bool
	flushed bool // sealed ring has been written to disk

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

func (r *executionRing) append(e Entry) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.sealed {
		return
	}
	if !r.full {
		r.entries = append(r.entries, e)
		if len(r.entries) == r.cap {
			r.full = true
		}
		return
	}
	r.entries[r.head] = e
	r.head = (r.head + 1) % r.cap
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

// seal marks the ring immutable and returns its current contents in
// chronological order. Subsequent calls return the same snapshot
// without re-allocating.
func (r *executionRing) seal() []Entry {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.sealed = true
	return r.snapshotLocked()
}

// hasUnflushed returns true if the ring has been sealed and its
// contents have not yet been flushed to disk. Tracked alongside the
// ring rather than inside diskStore so the LRU path can skip eviction
// without touching the disk lock.
func (r *executionRing) hasUnflushed() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.sealed && !r.flushed
}

// markFlushed transitions a sealed ring into the disk-backed state so
// LRU eviction can free it.
func (r *executionRing) markFlushed() {
	r.mu.Lock()
	r.flushed = true
	r.mu.Unlock()
}

// isSealed reports whether the ring has been sealed.
func (r *executionRing) isSealed() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.sealed
}
