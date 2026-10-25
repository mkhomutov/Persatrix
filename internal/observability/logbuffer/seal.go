package logbuffer

import (
	"errors"
	"sync"
	"time"

	"go.uber.org/zap"
)

// SealGraceDefault is how long SealAfterGrace waits before writing a
// finished execution to disk. The agent log shipper sends a batch at
// least every 0.5 s while its stream is up (FLUSH_INTERVAL_SEC in
// agents/observability/log_shipper.py), so the wait usually puts an
// agent's last batch in the same file as the run. Entries that come
// later, for example once the shipper reconnects (it backs off up to
// 30 s), are still kept and written to a further file.
const SealGraceDefault = 2 * time.Second

// CloseTimeoutDefault is how long Close waits for finished executions to
// reach disk, so a stalled disk cannot hold up shutdown.
const CloseTimeoutDefault = 5 * time.Second

var errCloseTimeout = errors.New("logbuffer: timed out writing finished executions to disk")

// sealQueue tracks finished executions waiting out their grace before Seal.
type sealQueue struct {
	mu      sync.Mutex
	pending map[string]*time.Timer // execution_id → grace timer
	running sync.WaitGroup         // seals under way, from a timer or from Close
}

// Seal marks the named execution's ring as sealed and flushes the
// entries not yet on disk to a new sequence file. A sealed ring still
// admits late entries; a later Seal writes only those, so a repeat Seal
// never writes an entry twice.
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
	ring.flushMu.Lock()
	defer ring.flushMu.Unlock()
	entries, taken := ring.seal()
	if len(entries) == 0 {
		ring.markFlushed(taken)
		return nil
	}
	if err := b.disk.flush(executionID, entries); err != nil {
		return err
	}
	ring.markFlushed(taken)
	return nil
}

// SealAfterGrace seals a finished execution and writes it to disk once
// Config.SealGrace has passed, so the run's entries survive a restart. The
// scheduler calls it when a run reaches a terminal state. The ring is
// sealed at once, which keeps LRU eviction off it until its entries are on
// disk; entries arriving later are still kept and written by a further
// seal. A repeat request while one is waiting is ignored; after Close the
// seal happens at once. Errors are logged rather than returned because the
// seal runs later, on a timer. A nil Buffer does nothing.
func (b *Buffer) SealAfterGrace(executionID string) {
	if b == nil {
		return
	}
	if !validExecutionID(executionID) {
		b.logger.Warn("logbuffer: not sealing an invalid execution_id",
			zap.String("execution_id", executionID))
		return
	}
	// Under b.mu so eviction cannot drop the ring between lookup and mark.
	b.mu.RLock()
	ring, ok := b.rings[executionID]
	if ok {
		ring.markSealed()
	}
	b.mu.RUnlock()
	if !ok {
		b.logger.Warn("logbuffer: no log ring to seal for a finished execution (evicted before it ended)",
			zap.String("execution_id", executionID))
		return
	}
	b.scheduleSeal(executionID)
}

// scheduleSeal seals executionID once SealGrace has passed, or at once
// after Close. A request while one is waiting is ignored. Append calls it
// for an entry that arrives after its execution was sealed.
func (b *Buffer) scheduleSeal(executionID string) {
	q := &b.seals
	q.mu.Lock()
	// Close sets b.closed before drainSeals takes q.mu, so a timer added
	// here while it is false is always one drainSeals will see.
	if b.closed.Load() {
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

// sealOrWarn seals executionID and logs a failure. After a failed write
// the entries stay in memory only: the ring stops counting them as
// waiting for disk, so a broken disk cannot keep every finished ring from
// LRU eviction.
func (b *Buffer) sealOrWarn(executionID string) {
	err := b.Seal(executionID)
	if err == nil {
		return
	}
	b.logger.Warn("logbuffer: writing a finished execution to disk failed; its entries stay in memory only",
		zap.String("execution_id", executionID), zap.Error(err))
	b.mu.RLock()
	ring, ok := b.rings[executionID]
	b.mu.RUnlock()
	if ok {
		// Under flushMu so a concurrent Seal's markFlushed cannot run after
		// the reset and take the count below zero.
		ring.flushMu.Lock()
		ring.mu.Lock()
		ring.unflushed = 0
		ring.mu.Unlock()
		ring.flushMu.Unlock()
	}
}

// drainSeals seals every execution still waiting out its grace and waits
// for seals under way, up to Config.CloseTimeout, so a shutdown inside the
// grace window does not lose a finished run and a stalled disk cannot hold
// up shutdown. Failed writes are logged by sealOrWarn. Called once, from
// Close.
func (b *Buffer) drainSeals() error {
	q := &b.seals
	q.mu.Lock()
	for id, timer := range q.pending {
		timer.Stop()
		q.running.Add(1)
		go func() {
			defer q.running.Done()
			b.sealOrWarn(id)
		}()
	}
	clear(q.pending)
	q.mu.Unlock()

	done := make(chan struct{})
	go func() {
		q.running.Wait()
		close(done)
	}()
	select {
	case <-done:
		return nil
	case <-time.After(b.cfg.CloseTimeout):
		return errCloseTimeout
	}
}
