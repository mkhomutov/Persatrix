package logbuffer

import (
	"errors"
	"sync"
	"time"

	"go.uber.org/zap"
)

// SealGraceDefault is how long SealAfterGrace waits before sealing. The
// agent log shipper sends a batch at least every 0.5 s
// (FLUSH_INTERVAL_SEC in agents/observability/log_shipper.py), and a sealed
// ring ignores later entries, so the wait keeps an agent's last batch for
// the run.
const SealGraceDefault = 2 * time.Second

// sealQueue tracks finished executions waiting out their grace before Seal.
type sealQueue struct {
	mu      sync.Mutex
	pending map[string]*time.Timer // execution_id → grace timer
	closed  bool                   // set by Close; later requests seal at once
	running sync.WaitGroup         // grace timers that fired and are sealing
}

// Seal marks the named execution's ring as sealed (no further admit
// after seal is allowed only for entries of the same execution; in
// practice executors stop emitting once the workflow terminates) and
// flushes the ring's contents to disk in a single batch.
//
// Sealed rings remain queryable via Snapshot until LRU-evicted.
//
// Invalid execution IDs are rejected here as well: the value would
// otherwise reach disk.flush as a path component (see validExecutionID).
func (b *Buffer) Seal(executionID string) error {
	if !validExecutionID(executionID) {
		return errInvalidExecutionID
	}
	b.mu.RLock()
	ring, ok := b.rings[executionID]
	b.mu.RUnlock()
	if !ok {
		return nil
	}
	entries := ring.seal()
	if len(entries) == 0 {
		ring.markFlushed()
		return nil
	}
	if err := b.disk.flush(executionID, entries); err != nil {
		return err
	}
	ring.markFlushed()
	return nil
}

// SealAfterGrace seals a finished execution once Config.SealGrace has
// passed, so the run's entries reach disk and survive a restart. The
// scheduler calls it when a run reaches a terminal state. A repeat request
// while one is waiting is ignored; after Close the seal happens at once.
// Errors are logged rather than returned because the seal runs later, on
// a timer.
func (b *Buffer) SealAfterGrace(executionID string) {
	if !validExecutionID(executionID) {
		b.logger.Warn("logbuffer: not sealing an invalid execution_id",
			zap.String("execution_id", executionID))
		return
	}
	q := &b.seals
	q.mu.Lock()
	if q.closed {
		q.mu.Unlock()
		b.sealOrWarn(executionID)
		return
	}
	if _, waiting := q.pending[executionID]; waiting {
		q.mu.Unlock()
		return
	}
	q.pending[executionID] = time.AfterFunc(b.cfg.SealGrace, func() {
		q.mu.Lock()
		if _, waiting := q.pending[executionID]; !waiting {
			// Close already took this one and sealed it.
			q.mu.Unlock()
			return
		}
		delete(q.pending, executionID)
		q.running.Add(1)
		q.mu.Unlock()
		defer q.running.Done()
		b.sealOrWarn(executionID)
	})
	q.mu.Unlock()
}

func (b *Buffer) sealOrWarn(executionID string) {
	if err := b.Seal(executionID); err != nil {
		b.logger.Warn("logbuffer: sealing a finished execution failed",
			zap.String("execution_id", executionID), zap.Error(err))
	}
}

// drainSeals seals every execution still waiting out its grace and waits
// for seals already under way, so a shutdown inside the grace window does
// not lose a finished run. Called once, from Close.
func (b *Buffer) drainSeals() error {
	q := &b.seals
	q.mu.Lock()
	q.closed = true
	waiting := make([]string, 0, len(q.pending))
	for id, timer := range q.pending {
		timer.Stop()
		waiting = append(waiting, id)
	}
	clear(q.pending)
	q.mu.Unlock()

	var errs []error
	for _, id := range waiting {
		if err := b.Seal(id); err != nil {
			errs = append(errs, err)
		}
	}
	q.running.Wait()
	return errors.Join(errs...)
}
