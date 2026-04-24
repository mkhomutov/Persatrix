package logbuffer

import (
	"sync/atomic"

	"go.uber.org/zap"
)

// lruCounter is a process-wide monotonic counter used to stamp each
// ring's lastTouch on the LRU fast-path. A wall-clock value would
// collapse to the same stamp across rapid admits on platforms with
// coarse clock resolution (Windows time.Now() can have ~15ms
// granularity), making the eviction order non-deterministic. An
// atomic counter is both monotonic and free of clock-skew artefacts.
var lruCounter uint64

// lruClock returns the next monotonic LRU stamp. Cheap and lock-free.
func lruClock() uint64 { return atomic.AddUint64(&lruCounter, 1) }

// getOrCreateRing returns the ring for executionID, creating it lazily.
// Bumps the LRU order on every call so most-recently-used rings sort to
// the tail.
//
// Hot path: if the ring already exists we take an RLock, stamp the
// ring's atomic lastTouch, and return without ever blocking another
// concurrent Append on the same buffer. Only the cold path (first
// admit for an execution_id, or the eviction sweep) needs the write
// lock. Lazy LRU reorder happens inside evictLocked (which the cold
// path holds) by sorting on lastTouch.
func (b *Buffer) getOrCreateRing(executionID string) *executionRing {
	// RLock fast-path for the steady-state case (ring already exists).
	b.mu.RLock()
	if ring, ok := b.rings[executionID]; ok {
		atomic.StoreUint64(&ring.lastTouch, lruClock())
		b.mu.RUnlock()
		return ring
	}
	b.mu.RUnlock()

	b.mu.Lock()
	defer b.mu.Unlock()
	// Re-check after upgrading: another caller may have raced and
	// already created the ring.
	if ring, ok := b.rings[executionID]; ok {
		atomic.StoreUint64(&ring.lastTouch, lruClock())
		return ring
	}
	ring := newExecutionRing(executionID, b.cfg.PerExecution, b.cfg.RatePerExec)
	atomic.StoreUint64(&ring.lastTouch, lruClock())
	b.rings[executionID] = ring
	b.lru = append(b.lru, executionID)
	b.evictLocked()
	return ring
}

// evictLocked enforces MaxExecutions. Caller holds b.mu (write lock).
//
// Victim selection now reads each ring's lastTouch atomically rather
// than relying on b.lru's slice order, so the RLock fast-path in
// getOrCreateRing can update touch stamps without taking the write
// lock. b.lru is kept as a stable iteration source (insertion order)
// to bound work on the cold path; we still pick the oldest-touched
// ring among non-pinned candidates.
//
// Sealed rings with un-flushed entries are protected: eviction skips
// them and falls back to the next-oldest active ring. If only
// protected rings remain, eviction is a no-op (the cap is exceeded
// transiently until the pending flushes complete).
func (b *Buffer) evictLocked() {
	for len(b.rings) > b.cfg.MaxExecutions {
		victim := -1
		var victimTouch uint64
		for i, id := range b.lru {
			ring, ok := b.rings[id]
			if !ok {
				continue
			}
			if ring.hasUnflushed() {
				continue
			}
			t := atomic.LoadUint64(&ring.lastTouch)
			if victim < 0 || t < victimTouch {
				victim = i
				victimTouch = t
			}
		}
		if victim < 0 {
			return
		}
		id := b.lru[victim]
		ring := b.rings[id]
		if ring.isSealed() {
			b.evictedSealed.Add(1)
		} else {
			b.evictedActive.Add(1)
		}
		delete(b.rings, id)
		b.lru = append(b.lru[:victim], b.lru[victim+1:]...)
		// Drop any rate-limit warning gate state for the evicted
		// execution so the rateWarned map cannot grow unbounded for
		// the orchestrator's lifetime (PR #172 review nice-to-have).
		b.forgetRateWarned(id)
	}
}

// warmLoad rebuilds sealed-and-flushed entries for any executions
// already on disk. Loaded executions are inserted as sealed +
// already-flushed rings so subsequent appends for the same execution
// id (rare in practice; only happens on a restart that races a still-
// shipping agent) admit through the normal path without re-flushing
// the warm-loaded prefix.
//
// The loader is bounded by cfg.MaxExecutions: data/logs may legitimately
// contain thousands of sealed executions on a long-running deployment,
// and constructing rings for all of them before evicting down to the
// LRU cap would cause a startup memory spike of (PerExecution *
// total_on_disk) entries. Instead we walk disk.list() newest-first and
// stop after MaxExecutions — older sealed executions remain on disk and
// are still queryable via Snapshot's disk.read() fallback path.
func (b *Buffer) warmLoad() error {
	loaded, err := b.disk.list()
	if err != nil {
		return err
	}
	// disk.list() returns oldest→newest; reverse so we admit the
	// freshest executions first and stop once we hit MaxExecutions.
	for i, j := 0, len(loaded)-1; i < j; i, j = i+1, j-1 {
		loaded[i], loaded[j] = loaded[j], loaded[i]
	}
	var emptySkipped int
	for _, id := range loaded {
		if len(b.rings) >= b.cfg.MaxExecutions {
			break
		}
		entries := b.disk.read(id)
		if len(entries) == 0 {
			emptySkipped++
			continue
		}
		ring := newExecutionRing(id, b.cfg.PerExecution, b.cfg.RatePerExec)
		for _, e := range entries {
			ring.append(e)
			if ring.full {
				break
			}
		}
		ring.mu.Lock()
		ring.sealed = true
		ring.flushed = true
		ring.mu.Unlock()
		// Stamp lastTouch in disk-list order (oldest→newest) so
		// evictLocked picks the oldest warm-loaded ring first if the
		// cap is exceeded by a fresh admit. The wall-clock value is
		// arbitrary; only its relative ordering matters here.
		atomic.StoreUint64(&ring.lastTouch, lruClock())
		b.rings[id] = ring
		// LRU is oldest→newest; we are inserting newest-first, so
		// prepend to keep the LRU order consistent with rest-of-life
		// access patterns.
		b.lru = append([]string{id}, b.lru...)
	}
	if emptySkipped > 0 {
		b.logger.Warn(
			"log buffer warm-load skipped empty executions",
			zap.Int("skipped", emptySkipped),
		)
	}
	if len(loaded) > b.cfg.MaxExecutions {
		b.logger.Info(
			"log buffer warm-load capped at MaxExecutions",
			zap.Int("on_disk", len(loaded)),
			zap.Int("loaded", b.cfg.MaxExecutions),
		)
	}
	return nil
}
