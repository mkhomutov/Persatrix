package logbuffer

import "go.uber.org/zap"

// getOrCreateRing returns the ring for executionID, creating it lazily.
// Bumps the LRU order on every call so most-recently-used rings sort to
// the tail.
func (b *Buffer) getOrCreateRing(executionID string) *executionRing {
	b.mu.Lock()
	defer b.mu.Unlock()
	if ring, ok := b.rings[executionID]; ok {
		b.touchLocked(executionID)
		return ring
	}
	ring := newExecutionRing(executionID, b.cfg.PerExecution, b.cfg.RatePerExec)
	b.rings[executionID] = ring
	b.lru = append(b.lru, executionID)
	b.evictLocked()
	return ring
}

// touchLocked moves executionID to the tail of b.lru. Caller holds b.mu.
func (b *Buffer) touchLocked(executionID string) {
	for i, id := range b.lru {
		if id != executionID {
			continue
		}
		b.lru = append(b.lru[:i], b.lru[i+1:]...)
		b.lru = append(b.lru, executionID)
		return
	}
}

// evictLocked enforces MaxExecutions. Caller holds b.mu. Sealed rings
// with un-flushed entries are protected: eviction skips them and falls
// back to the next-oldest active ring. If only protected rings remain,
// eviction is a no-op (the cap is exceeded transiently until the
// pending flushes complete).
func (b *Buffer) evictLocked() {
	for len(b.rings) > b.cfg.MaxExecutions {
		victim := -1
		for i, id := range b.lru {
			ring, ok := b.rings[id]
			if !ok {
				continue
			}
			if ring.hasUnflushed() {
				continue
			}
			victim = i
			break
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
