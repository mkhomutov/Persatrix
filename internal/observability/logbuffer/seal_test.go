package logbuffer

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap/zaptest"
)

func sealFiles(t *testing.T, dir, execID string) []string {
	t.Helper()
	files, err := filepath.Glob(filepath.Join(dir, execID, "*.jsonl"))
	require.NoError(t, err)
	return files
}

// An agent entry that arrives inside the grace window is kept with the run,
// and the sealed entries survive a restart. The grace is long so the test
// never races the timer; Close writes the waiting seal.
func TestSealAfterGrace_KeepsLateEntriesAndSurvivesRestart(t *testing.T) {
	dir := t.TempDir()
	logger := zaptest.NewLogger(t)
	cfg := Config{Dir: dir, PerExecution: 8, MaxExecutions: 3, RatePerExec: 1000, SealGrace: time.Hour}

	b1, err := New(cfg, logger)
	require.NoError(t, err)
	t.Cleanup(func() { _ = b1.Close() })
	require.Equal(t, DropNone, b1.Append(mkEntry("exec-grace", "INFO", "orchestrator: run completed")))
	b1.SealAfterGrace("exec-grace")
	require.Equal(t, DropNone, b1.Append(mkEntry("exec-grace", "INFO", "agent: tail entry")))
	assert.Empty(t, sealFiles(t, dir, "exec-grace"), "nothing reaches disk before the grace ends")
	require.NoError(t, b1.Close())

	b2, err := New(cfg, logger)
	require.NoError(t, err)
	t.Cleanup(func() { _ = b2.Close() })
	got := b2.Snapshot("exec-grace")
	require.Len(t, got, 2)
	assert.Equal(t, "orchestrator: run completed", got[0].Message)
	assert.Equal(t, "agent: tail entry", got[1].Message)
}

// The grace timer writes the run to disk on its own, without Close.
func TestSealAfterGrace_WritesWhenTheGraceEnds(t *testing.T) {
	dir := t.TempDir()
	b, err := New(Config{Dir: dir, PerExecution: 8, MaxExecutions: 3, RatePerExec: 1000, SealGrace: 10 * time.Millisecond}, zaptest.NewLogger(t))
	require.NoError(t, err)
	t.Cleanup(func() { _ = b.Close() })

	require.Equal(t, DropNone, b.Append(mkEntry("exec-timer", "INFO", "m0")))
	b.SealAfterGrace("exec-timer")
	require.Eventually(t, func() bool { return len(sealFiles(t, dir, "exec-timer")) == 1 },
		2*time.Second, 10*time.Millisecond, "ring was not flushed to disk after the grace")
}

// An entry that arrives after the run was written to disk (an agent still
// working after its step timed out, or a shipper back from a reconnect) is
// still admitted, shown, and written to a further file, without writing the
// earlier entries again.
func TestSealAfterGrace_KeepsEntriesThatArriveAfterTheWrite(t *testing.T) {
	dir := t.TempDir()
	logger := zaptest.NewLogger(t)
	cfg := Config{Dir: dir, PerExecution: 8, MaxExecutions: 3, RatePerExec: 1000, SealGrace: 10 * time.Millisecond}

	b1, err := New(cfg, logger)
	require.NoError(t, err)
	t.Cleanup(func() { _ = b1.Close() })
	require.Equal(t, DropNone, b1.Append(mkEntry("exec-late", "INFO", "run failed")))
	b1.SealAfterGrace("exec-late")
	require.Eventually(t, func() bool { return len(sealFiles(t, dir, "exec-late")) == 1 },
		2*time.Second, 10*time.Millisecond)

	require.Equal(t, DropNone, b1.Append(mkEntry("exec-late", "ERROR", "agent: late failure detail")))
	assert.Len(t, b1.Snapshot("exec-late"), 2, "a late entry must stay visible")
	require.Eventually(t, func() bool { return len(sealFiles(t, dir, "exec-late")) == 2 },
		2*time.Second, 10*time.Millisecond, "a late entry must reach disk too")
	require.NoError(t, b1.Close())

	b2, err := New(cfg, logger)
	require.NoError(t, err)
	t.Cleanup(func() { _ = b2.Close() })
	got := b2.Snapshot("exec-late")
	require.Len(t, got, 2, "each entry is on disk exactly once")
	assert.Equal(t, "run failed", got[0].Message)
	assert.Equal(t, "agent: late failure detail", got[1].Message)
}

// Shutting down inside the grace window must not lose the execution:
// Close seals and flushes pending executions at once.
func TestSealAfterGrace_CloseSealsPendingAtOnce(t *testing.T) {
	dir := t.TempDir()
	logger := zaptest.NewLogger(t)
	cfg := Config{Dir: dir, PerExecution: 8, MaxExecutions: 3, RatePerExec: 1000, SealGrace: time.Hour}

	b1, err := New(cfg, logger)
	require.NoError(t, err)
	require.Equal(t, DropNone, b1.Append(mkEntry("exec-close", "INFO", "m0")))
	b1.SealAfterGrace("exec-close")
	require.NoError(t, b1.Close())
	require.Len(t, sealFiles(t, dir, "exec-close"), 1, "Close must flush a pending seal")

	b2, err := New(cfg, logger)
	require.NoError(t, err)
	t.Cleanup(func() { _ = b2.Close() })
	require.Len(t, b2.Snapshot("exec-close"), 1)
}

// A second request for the same execution while one is pending must not
// write a second, duplicate sequence file.
func TestSealAfterGrace_RepeatRequestSealsOnce(t *testing.T) {
	dir := t.TempDir()
	b, err := New(Config{Dir: dir, PerExecution: 8, MaxExecutions: 3, RatePerExec: 1000, SealGrace: 30 * time.Millisecond}, zaptest.NewLogger(t))
	require.NoError(t, err)
	t.Cleanup(func() { _ = b.Close() })

	require.Equal(t, DropNone, b.Append(mkEntry("exec-twice", "INFO", "m0")))
	b.SealAfterGrace("exec-twice")
	b.SealAfterGrace("exec-twice")
	require.Eventually(t, func() bool { return len(sealFiles(t, dir, "exec-twice")) >= 1 },
		2*time.Second, 10*time.Millisecond)
	time.Sleep(100 * time.Millisecond)
	assert.Len(t, sealFiles(t, dir, "exec-twice"), 1)
}

// Sealing again with nothing new, or sealing a ring warm-loaded from disk,
// writes nothing: otherwise each entry would show twice after a restart.
func TestSeal_RepeatWritesNothingNew(t *testing.T) {
	dir := t.TempDir()
	logger := zaptest.NewLogger(t)
	cfg := Config{Dir: dir, PerExecution: 8, MaxExecutions: 3, RatePerExec: 1000}

	b1, err := New(cfg, logger)
	require.NoError(t, err)
	require.Equal(t, DropNone, b1.Append(mkEntry("exec-again", "INFO", "m0")))
	require.NoError(t, b1.Seal("exec-again"))
	require.NoError(t, b1.Seal("exec-again"))
	assert.Len(t, sealFiles(t, dir, "exec-again"), 1, "a repeat Seal must not rewrite the ring")
	require.NoError(t, b1.Close())

	b2, err := New(cfg, logger)
	require.NoError(t, err)
	t.Cleanup(func() { _ = b2.Close() })
	require.NoError(t, b2.Seal("exec-again"))
	assert.Len(t, sealFiles(t, dir, "exec-again"), 1, "sealing a warm-loaded ring must not rewrite it")
	assert.Len(t, b2.Snapshot("exec-again"), 1)
}

// While a finished execution waits out its grace, LRU eviction must leave
// its ring alone, or the run never reaches disk.
func TestSealAfterGrace_ProtectsTheRingFromEviction(t *testing.T) {
	dir := t.TempDir()
	b, err := New(Config{Dir: dir, PerExecution: 8, MaxExecutions: 2, RatePerExec: 1000, SealGrace: time.Hour}, zaptest.NewLogger(t))
	require.NoError(t, err)
	t.Cleanup(func() { _ = b.Close() })

	require.Equal(t, DropNone, b.Append(mkEntry("exec-done", "INFO", "run completed")))
	b.SealAfterGrace("exec-done")
	for _, id := range []string{"exec-a", "exec-b", "exec-c"} {
		require.Equal(t, DropNone, b.Append(mkEntry(id, "INFO", "executing run")))
	}
	assert.Len(t, b.Snapshot("exec-done"), 1, "a ring waiting for its seal must not be evicted")

	require.NoError(t, b.Close())
	assert.Len(t, sealFiles(t, dir, "exec-done"), 1)
}

// A failed background write must not keep the ring from eviction for good:
// with a broken disk every finished run would otherwise stay in memory.
func TestSealAfterGrace_FailedWriteDoesNotPinTheRing(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("read-only chmod is not portable on Windows")
	}
	dir := t.TempDir()
	b, err := New(Config{Dir: dir, PerExecution: 8, MaxExecutions: 2, RatePerExec: 1000, SealGrace: 10 * time.Millisecond}, zaptest.NewLogger(t))
	require.NoError(t, err)
	t.Cleanup(func() { _ = b.Close() })

	require.Equal(t, DropNone, b.Append(mkEntry("exec-broken", "INFO", "run failed")))
	require.NoError(t, os.Chmod(dir, 0o500))
	t.Cleanup(func() { _ = os.Chmod(dir, 0o700) })
	b.SealAfterGrace("exec-broken")

	b.mu.RLock()
	ring := b.rings["exec-broken"]
	b.mu.RUnlock()
	require.NotNil(t, ring)
	require.Eventually(t, func() bool { return ring.isSealed() && !ring.hasUnflushed() },
		2*time.Second, 10*time.Millisecond, "a failed background write must release the ring")

	for _, id := range []string{"exec-a", "exec-b"} {
		require.Equal(t, DropNone, b.Append(mkEntry(id, "INFO", "executing run")))
	}
	assert.Equal(t, uint64(1), b.Stats().EvictedSealed, "the failed ring must be evictable")
}

// A stalled disk must not hold up shutdown: Close stops waiting for seals
// under way after CloseTimeout and says so.
func TestClose_StopsWaitingForAStalledSeal(t *testing.T) {
	b := newTestBuffer(t, Config{CloseTimeout: 20 * time.Millisecond})
	b.seals.running.Add(1) // a seal stuck on the disk
	t.Cleanup(b.seals.running.Done)

	start := time.Now()
	require.ErrorIs(t, b.Close(), errCloseTimeout)
	assert.Less(t, time.Since(start), time.Second)
}

// After Close there is no timer left to wait for, so a request seals at once.
func TestSealAfterGrace_AfterCloseSealsAtOnce(t *testing.T) {
	dir := t.TempDir()
	b, err := New(Config{Dir: dir, PerExecution: 8, MaxExecutions: 3, RatePerExec: 1000, SealGrace: time.Hour}, zaptest.NewLogger(t))
	require.NoError(t, err)
	require.Equal(t, DropNone, b.Append(mkEntry("exec-late", "INFO", "m0")))
	require.NoError(t, b.Close())

	b.SealAfterGrace("exec-late")
	assert.Len(t, sealFiles(t, dir, "exec-late"), 1)
}

func TestSealAfterGrace_InvalidIDIsIgnored(t *testing.T) {
	b := newTestBuffer(t, Config{SealGrace: time.Millisecond})
	assert.NotPanics(t, func() { b.SealAfterGrace("../escape") })
}

// A nil *Buffer handed to the scheduler as its LogSealer is a non-nil
// interface, so the scheduler's nil check passes; sealing must still be safe.
func TestSealAfterGrace_NilBufferDoesNothing(t *testing.T) {
	var b *Buffer
	assert.NotPanics(t, func() { b.SealAfterGrace("exec-1") })
}
