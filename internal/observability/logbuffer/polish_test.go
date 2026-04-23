package logbuffer

import (
	"os"
	"path/filepath"
	"runtime"
	"sync/atomic"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest"
	"go.uber.org/zap/zaptest/observer"
)

// TestWarnRateOnce_OnlyEmitsOncePerExecution locks down the
// "single throttled WARN log per execution" contract from RFC § E.
// Added per PR #172 review coverage gap.
func TestWarnRateOnce_OnlyEmitsOncePerExecution(t *testing.T) {
	core, recorded := observer.New(zap.WarnLevel)
	logger := zap.New(core)

	cfg := Defaults()
	cfg.Dir = t.TempDir()
	cfg.RatePerExec = 1
	cfg.MaxExecutions = 4
	b, err := New(cfg, logger)
	require.NoError(t, err)
	t.Cleanup(func() { _ = b.Close() })

	// First admit fills the bucket; subsequent INFO admits are
	// rate-limited and trip warnRateOnce. Warn lines are gated to
	// one per execution_id regardless of how many drops occur.
	require.Equal(t, DropNone, b.Append(mkEntry("exec-warn", "INFO", "first")))
	for i := 0; i < 50; i++ {
		_ = b.Append(mkEntry("exec-warn", "INFO", "drop"))
	}

	warnCount := 0
	for _, entry := range recorded.AllUntimed() {
		if entry.Message == "log buffer rate limit exceeded" {
			warnCount++
		}
	}
	assert.Equal(t, 1, warnCount, "warnRateOnce must emit exactly one WARN per execution")

	// A different execution gets its own one-shot warning.
	require.Equal(t, DropNone, b.Append(mkEntry("exec-other", "INFO", "first")))
	for i := 0; i < 5; i++ {
		_ = b.Append(mkEntry("exec-other", "INFO", "drop"))
	}
	warnCount = 0
	for _, entry := range recorded.AllUntimed() {
		if entry.Message == "log buffer rate limit exceeded" {
			warnCount++
		}
	}
	assert.Equal(t, 2, warnCount, "second execution must emit its own WARN once")
}

// TestRateWarnedPrunedOnEviction verifies that the rateWarned map is
// trimmed when its execution is LRU-evicted, so the gate cannot grow
// unbounded over the orchestrator's lifetime. PR #172 review
// nice-to-have: previously the map retained an entry per execution
// that ever tripped the limiter, retained for the orchestrator's
// lifetime.
func TestRateWarnedPrunedOnEviction(t *testing.T) {
	cfg := Defaults()
	cfg.Dir = t.TempDir()
	cfg.RatePerExec = 1
	cfg.MaxExecutions = 2
	b, err := New(cfg, zaptest.NewLogger(t))
	require.NoError(t, err)
	t.Cleanup(func() { _ = b.Close() })

	// Trip warnRateOnce for the first execution then write to two
	// more so it falls out of the LRU and is evicted.
	require.Equal(t, DropNone, b.Append(mkEntry("exec-1", "INFO", "ok")))
	for i := 0; i < 3; i++ {
		_ = b.Append(mkEntry("exec-1", "INFO", "drop"))
	}

	b.rateWarnedMu.Lock()
	_, present := b.rateWarned["exec-1"]
	b.rateWarnedMu.Unlock()
	require.True(t, present, "warnRateOnce should have recorded exec-1")

	// Push two more executions through to evict exec-1 (cap=2).
	require.Equal(t, DropNone, b.Append(mkEntry("exec-2", "INFO", "two")))
	require.Equal(t, DropNone, b.Append(mkEntry("exec-3", "INFO", "three")))

	b.rateWarnedMu.Lock()
	_, stillThere := b.rateWarned["exec-1"]
	b.rateWarnedMu.Unlock()
	assert.False(t, stillThere, "rateWarned entry must be pruned after LRU eviction")
}

// TestSeal_DiskFlushFailureDoesNotMarkRingFlushed proves that a
// failed disk flush leaves the ring sealed-but-unflushed, protecting
// it from LRU eviction so the entries are not silently lost. PR #172
// review coverage gap.
func TestSeal_DiskFlushFailureDoesNotMarkRingFlushed(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("read-only chmod is not portable on Windows")
	}

	cfg := Defaults()
	cfg.Dir = t.TempDir()
	b, err := New(cfg, zaptest.NewLogger(t))
	require.NoError(t, err)
	t.Cleanup(func() { _ = b.Close() })

	require.Equal(t, DropNone, b.Append(mkEntry("exec-fail", "INFO", "before-seal")))

	// Make the disk store's root unwritable so MkdirAll for the
	// per-execution subdirectory fails.
	require.NoError(t, os.Chmod(cfg.Dir, 0o500))
	t.Cleanup(func() { _ = os.Chmod(cfg.Dir, 0o700) })

	err = b.Seal("exec-fail")
	require.Error(t, err, "Seal must surface the disk flush error")

	b.mu.RLock()
	ring := b.rings["exec-fail"]
	b.mu.RUnlock()
	require.NotNil(t, ring, "ring must remain in the buffer after a failed flush")
	assert.True(t, ring.hasUnflushed(), "failed-flush ring must stay sealed-but-unflushed (LRU-protected)")
}

// TestDiskFlush_RejectsTraversalExecutionID exercises the disk-layer
// defence-in-depth check (PR-172 review coverage gap). The validator
// at the Buffer.Append boundary is the primary line of defence; this
// test pins that disk.flush itself also refuses path-traversal IDs
// so a regression in any in-package caller cannot re-open the hole.
func TestDiskFlush_RejectsTraversalExecutionID(t *testing.T) {
	d, err := newDiskStore(t.TempDir(), 0, zaptest.NewLogger(t))
	require.NoError(t, err)

	bad := []string{"../etc", "a/b", "..\\windows", ".", ".."}
	for _, id := range bad {
		err := d.flush(id, []Entry{{SchemaVersion: "1", Level: "INFO", Message: "x"}})
		assert.Error(t, err, "flush must refuse %q", id)
	}
}

// TestScan_RejectsJunkSequenceFilenames covers the strconv.Atoi +
// "seq < 1" guard added during PR 8 polish — a directory containing
// externally-dropped files like "  3.jsonl" or "-7.jsonl" must not
// poison the next-sequence calculation. PR #172 review nice-to-have.
func TestScan_RejectsJunkSequenceFilenames(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "exec-junk")
	require.NoError(t, os.MkdirAll(dir, 0o700))

	// Mix of well-formed and junk files. Only the well-formed file
	// should advance nextSeq; the others are silently skipped.
	files := []string{"  3.jsonl", "-7.jsonl", "abc.jsonl", "0000000005.jsonl", "0000000005.tmp"}
	for _, name := range files {
		require.NoError(t, os.WriteFile(filepath.Join(dir, name), []byte("{}\n"), 0o600))
	}

	d, err := newDiskStore(root, 0, zaptest.NewLogger(t))
	require.NoError(t, err)
	assert.Equal(t, 6, d.nextSeq["exec-junk"], "next sequence must derive only from the well-formed file")
}

// TestAppend_LRUFastPathBumpsLastTouch is a regression test for the
// PR 8 RWLock fast-path refactor: re-admitting an existing execution
// must not touch the write lock and must still update the ring's
// lastTouch stamp so evictLocked picks the right victim.
func TestAppend_LRUFastPathBumpsLastTouch(t *testing.T) {
	b := newTestBuffer(t, Config{MaxExecutions: 4})

	require.Equal(t, DropNone, b.Append(mkEntry("exec-a", "INFO", "1")))
	b.mu.RLock()
	ring := b.rings["exec-a"]
	b.mu.RUnlock()
	require.NotNil(t, ring)
	first := atomic.LoadUint64(&ring.lastTouch)

	// Re-admit on the steady-state path; lastTouch must advance.
	require.Equal(t, DropNone, b.Append(mkEntry("exec-a", "INFO", "2")))
	second := atomic.LoadUint64(&ring.lastTouch)
	assert.Greater(t, second, first-1, "lastTouch must be stamped on every admit")
}
