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
func (b *Buffer) warmLoad() error {
	loaded, err := b.disk.list()
	if err != nil {
		return err
	}
	for _, id := range loaded {
		entries := b.disk.read(id)
		if len(entries) == 0 {
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
		b.lru = append(b.lru, id)
	}
	if dropped := len(loaded) - len(b.rings); dropped > 0 {
		b.logger.Warn(
			"log buffer warm-load skipped empty executions",
			zap.Int("skipped", dropped),
		)
	}
	b.evictLocked()
	return nil
}
