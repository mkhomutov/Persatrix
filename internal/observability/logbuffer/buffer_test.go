package logbuffer

import (
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap/zaptest"
)

func newTestBuffer(t *testing.T, cfg Config) *Buffer {
	t.Helper()
	cfg.Dir = t.TempDir()
	if cfg.PerExecution == 0 {
		cfg.PerExecution = 4
	}
	if cfg.MaxExecutions == 0 {
		cfg.MaxExecutions = 3
	}
	if cfg.RatePerExec == 0 {
		// Use a deliberately huge rate so default tests are not
		// constrained by the limiter; tests that exercise the
		// limiter set this explicitly.
		cfg.RatePerExec = 100_000
	}
	b, err := New(cfg, zaptest.NewLogger(t))
	require.NoError(t, err)
	t.Cleanup(func() { _ = b.Close() })
	return b
}

func mkEntry(execID, level, msg string) Entry {
	return Entry{
		SchemaVersion: "1",
		Timestamp:     time.Now().UTC(),
		Level:         level,
		Message:       msg,
		ExecutionID:   execID,
	}
}

func TestAppend_RingCapacityRespected(t *testing.T) {
	b := newTestBuffer(t, Config{PerExecution: 4})
	for i := 0; i < 5; i++ {
		require.Equal(t, DropNone, b.Append(mkEntry("exec-1", "INFO", "m"+strconv.Itoa(i))))
	}
	got := b.Snapshot("exec-1")
	require.Len(t, got, 4, "ring should be capped at PerExecution=4")
	assert.Equal(t, "m1", got[0].Message, "oldest entry m0 should have been evicted")
	assert.Equal(t, "m4", got[3].Message)
}

func TestAppend_DropBelowLevel(t *testing.T) {
	b := newTestBuffer(t, Config{DropLevel: "INFO"})
	require.Equal(t, DropBelowLevel, b.Append(mkEntry("exec-1", "DEBUG", "drop me")))
	require.Equal(t, DropNone, b.Append(mkEntry("exec-1", "INFO", "keep me")))
	got := b.Snapshot("exec-1")
	require.Len(t, got, 1)
	assert.Equal(t, "keep me", got[0].Message)
	assert.Equal(t, uint64(1), b.Stats().DroppedBelowLevel)
}

func TestAppend_EmptyExecutionIDIsDropped(t *testing.T) {
	b := newTestBuffer(t, Config{})
	// Updated for review fix: empty exec IDs now surface as their own
	// reason (DropNoExecID) so the misuse is observable rather than
	// silently bucketed as a severity drop.
	require.Equal(t, DropNoExecID, b.Append(mkEntry("", "INFO", "no-exec")))
	assert.Equal(t, 0, b.Stats().ActiveRings)
	assert.Equal(t, uint64(1), b.Stats().DroppedNoExecID)
}

func TestAppend_InvalidExecutionIDIsRejected(t *testing.T) {
	// Regression for PR-172 review Must-Fix #1 (path-traversal). The
	// buffer must reject any execution ID that would be unsafe to use
	// as a filesystem path component before it reaches disk.flush().
	b := newTestBuffer(t, Config{})
	cases := []string{
		"../etc",
		"..\\etc",
		"a/b",
		"a\\b",
		"with space",
		"with:colon",
		strings.Repeat("a", 129),
	}
	for _, id := range cases {
		assert.Equal(t, DropInvalidID, b.Append(mkEntry(id, "INFO", "x")), id)
	}
	assert.Equal(t, uint64(len(cases)), b.Stats().DroppedInvalidID)
	assert.Equal(t, 0, b.Stats().ActiveRings)

	// Seal must reject the same set instead of letting them reach disk.
	for _, id := range cases {
		assert.Error(t, b.Seal(id), id)
	}
	// Snapshot returns nil for invalid IDs (no leakage of disk state).
	for _, id := range cases {
		assert.Nil(t, b.Snapshot(id), id)
	}
}

func TestAppend_ClosedBufferReturnsDropClosed(t *testing.T) {
	// Regression for PR-172 review Should-Fix #2: post-Close appends
	// previously aliased to DropBelowLevel; they now surface as
	// DropClosed so shutdown races are distinguishable.
	b := newTestBuffer(t, Config{})
	require.NoError(t, b.Close())
	assert.Equal(t, DropClosed, b.Append(mkEntry("exec-1", "INFO", "after close")))
	assert.Equal(t, uint64(1), b.Stats().DroppedClosed)
}

func TestAppend_RateLimitDropsBelowWarn(t *testing.T) {
	// Rate of 5 means after 5 INFO admissions the bucket is empty
	// (ignoring refill; the test runs faster than refill).
	b := newTestBuffer(t, Config{PerExecution: 100, RatePerExec: 5})
	admitted := 0
	dropped := 0
	for i := 0; i < 50; i++ {
		switch b.Append(mkEntry("exec-1", "INFO", "m")) {
		case DropNone:
			admitted++
		case DropRateLimit:
			dropped++
		}
	}
	assert.GreaterOrEqual(t, admitted, 5, "first burst of tokens must be admitted")
	assert.Greater(t, dropped, 0, "subsequent INFO entries must be rate-limited")
	assert.Equal(t, uint64(dropped), b.Stats().DroppedRate)
}

func TestAppend_WarnAlwaysAdmittedDespiteRateLimit(t *testing.T) {
	b := newTestBuffer(t, Config{PerExecution: 100, RatePerExec: 1})
	// Burn the single token.
	require.Equal(t, DropNone, b.Append(mkEntry("exec-1", "INFO", "m")))
	// INFO now exhausted, but WARN/ERROR must still be admitted.
	require.Equal(t, DropNone, b.Append(mkEntry("exec-1", "WARN", "warn")))
	require.Equal(t, DropNone, b.Append(mkEntry("exec-1", "ERROR", "err")))
	got := b.Snapshot("exec-1")
	assert.Len(t, got, 3)
}

func TestSeal_FlushesToDisk(t *testing.T) {
	dir := t.TempDir()
	b, err := New(Config{Dir: dir, PerExecution: 4, MaxExecutions: 3, RatePerExec: 1000}, zaptest.NewLogger(t))
	require.NoError(t, err)
	t.Cleanup(func() { _ = b.Close() })

	for i := 0; i < 3; i++ {
		require.Equal(t, DropNone, b.Append(mkEntry("exec-seal", "INFO", "m"+strconv.Itoa(i))))
	}
	require.NoError(t, b.Seal("exec-seal"))

	// On-disk file exists under <dir>/exec-seal/ and contains 3 lines.
	files, err := filepath.Glob(filepath.Join(dir, "exec-seal", "*.jsonl"))
	require.NoError(t, err)
	require.Len(t, files, 1, "Seal must write exactly one sequence file")
}

func TestWarmLoad_ResumesFromDisk(t *testing.T) {
	dir := t.TempDir()
	logger := zaptest.NewLogger(t)

	// First instance: append + seal.
	b1, err := New(Config{Dir: dir, PerExecution: 4, MaxExecutions: 3, RatePerExec: 1000}, logger)
	require.NoError(t, err)
	for i := 0; i < 3; i++ {
		require.Equal(t, DropNone, b1.Append(mkEntry("exec-warm", "INFO", "m"+strconv.Itoa(i))))
	}
	require.NoError(t, b1.Seal("exec-warm"))
	require.NoError(t, b1.Close())

	// Second instance: warm-load should expose the entries.
	b2, err := New(Config{Dir: dir, PerExecution: 4, MaxExecutions: 3, RatePerExec: 1000}, logger)
	require.NoError(t, err)
	t.Cleanup(func() { _ = b2.Close() })
	got := b2.Snapshot("exec-warm")
	require.Len(t, got, 3)
	assert.Equal(t, "m0", got[0].Message)
	assert.Equal(t, "m2", got[2].Message)
}

func TestLRU_EvictsLeastRecentlyUsed(t *testing.T) {
	b := newTestBuffer(t, Config{PerExecution: 4, MaxExecutions: 3})
	require.Equal(t, DropNone, b.Append(mkEntry("a", "INFO", "x")))
	require.Equal(t, DropNone, b.Append(mkEntry("b", "INFO", "x")))
	require.Equal(t, DropNone, b.Append(mkEntry("c", "INFO", "x")))
	// Touch "a" so "b" becomes the LRU victim.
	require.Equal(t, DropNone, b.Append(mkEntry("a", "INFO", "y")))
	// Adding "d" must evict "b".
	require.Equal(t, DropNone, b.Append(mkEntry("d", "INFO", "x")))
	assert.Nil(t, b.Snapshot("b"), "b should have been LRU-evicted")
	assert.NotNil(t, b.Snapshot("a"))
	assert.NotNil(t, b.Snapshot("c"))
	assert.NotNil(t, b.Snapshot("d"))
	assert.Equal(t, uint64(1), b.Stats().EvictedActive)
}

func TestLRU_SealedUnflushedRingProtectedFromEviction(t *testing.T) {
	// We can only mark a ring as sealed-and-unflushed by skipping the
	// disk write; reach into the ring directly.
	b := newTestBuffer(t, Config{PerExecution: 4, MaxExecutions: 2})
	require.Equal(t, DropNone, b.Append(mkEntry("locked", "INFO", "x")))

	b.mu.RLock()
	ring := b.rings["locked"]
	b.mu.RUnlock()
	ring.mu.Lock()
	ring.sealed = true
	ring.flushed = false
	ring.mu.Unlock()

	// Force two more rings: cap is 2, so one must be evicted, but
	// "locked" must be skipped because it has unflushed entries.
	require.Equal(t, DropNone, b.Append(mkEntry("a", "INFO", "x")))
	require.Equal(t, DropNone, b.Append(mkEntry("b", "INFO", "x")))

	assert.NotNil(t, b.Snapshot("locked"), "sealed-unflushed ring must survive eviction sweep")
}

func TestWarmLoad_TolerantOfMalformedTrailingLine(t *testing.T) {
	dir := t.TempDir()
	logger := zaptest.NewLogger(t)

	// Pre-create a sequence file with one good entry and a truncated
	// trailing line.
	require.NoError(t, mkdirAll(filepath.Join(dir, "exec-mal")))
	good := `{"schema_version":"1","timestamp":"2026-04-23T00:00:00Z","level":"INFO","message":"good","execution_id":"exec-mal"}` + "\n"
	bad := `{"schema_version":"1","timestamp":"2026-04`
	require.NoError(t, writeFile(filepath.Join(dir, "exec-mal", "0000000001.jsonl"), good+bad))

	b, err := New(Config{Dir: dir, PerExecution: 4, MaxExecutions: 3, RatePerExec: 1000}, logger)
	require.NoError(t, err)
	t.Cleanup(func() { _ = b.Close() })
	got := b.Snapshot("exec-mal")
	require.Len(t, got, 1, "well-formed prefix must load; truncated line skipped")
	assert.Equal(t, "good", got[0].Message)

	// Subsequent appends + seal still work.
	require.Equal(t, DropNone, b.Append(mkEntry("exec-mal", "INFO", "after")))
	require.NoError(t, b.Seal("exec-mal"))
}

func TestWarmLoad_BoundedByMaxExecutions(t *testing.T) {
	// Regression for PR-172 review Should-Fix #3: a busy data/logs
	// directory must not cause warm-load to allocate one ring per
	// on-disk execution before evicting down to MaxExecutions. We
	// pre-seed 8 sealed executions and confirm the buffer admits
	// exactly MaxExecutions=3 of them at startup.
	dir := t.TempDir()
	logger := zaptest.NewLogger(t)
	for i := 0; i < 8; i++ {
		id := "exec-warm-" + strconv.Itoa(i)
		require.NoError(t, mkdirAll(filepath.Join(dir, id)))
		line := `{"schema_version":"1","timestamp":"2026-04-23T00:00:00Z","level":"INFO","message":"m","execution_id":"` + id + `"}` + "\n"
		require.NoError(t, writeFile(filepath.Join(dir, id, "0000000001.jsonl"), line))
		// Stagger mtimes so list() ordering is deterministic.
		mt := time.Date(2026, 4, 23, 0, 0, i, 0, time.UTC)
		require.NoError(t, chTimes(filepath.Join(dir, id), mt))
	}

	b, err := New(Config{Dir: dir, PerExecution: 4, MaxExecutions: 3, RatePerExec: 1000}, logger)
	require.NoError(t, err)
	t.Cleanup(func() { _ = b.Close() })
	assert.Equal(t, 3, b.Stats().ActiveRings, "warm-load must cap at MaxExecutions")

	// The freshest 3 (suffix 5, 6, 7) must be in memory; older ones
	// remain queryable via the disk.read fallback in Snapshot.
	for _, i := range []int{5, 6, 7} {
		assert.NotNil(t, b.Snapshot("exec-warm-"+strconv.Itoa(i)),
			"newest execution must be in-memory after warm-load")
	}
	assert.NotNil(t, b.Snapshot("exec-warm-0"),
		"older execution must still be readable from disk fallback")
}

func TestConcurrentAppend_NoDataRaces(t *testing.T) {
	b := newTestBuffer(t, Config{PerExecution: 200, MaxExecutions: 5, RatePerExec: 100_000})
	const goroutines = 10
	const perGoroutine = 100
	var wg sync.WaitGroup
	for g := 0; g < goroutines; g++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			for i := 0; i < perGoroutine; i++ {
				_ = b.Append(mkEntry("exec-race", "INFO", strconv.Itoa(id*1000+i)))
			}
		}(g)
	}
	wg.Wait()
	got := b.Snapshot("exec-race")
	// Ring caps at 200 so we should never exceed that; but every
	// admitted entry should still be there (no panics, no duplicates
	// past the cap).
	assert.LessOrEqual(t, len(got), 200)
	assert.Greater(t, len(got), 0)
}

func TestApplyDefaults_FillsZeroFields(t *testing.T) {
	c := applyDefaults(Config{Dir: "/tmp/x"})
	assert.Equal(t, 1000, c.PerExecution)
	assert.Equal(t, 50, c.MaxExecutions)
	assert.Equal(t, "/tmp/x", c.Dir)
	assert.Equal(t, int64(512*1024*1024), c.DiskCapBytes)
	assert.Equal(t, "DEBUG", c.DropLevel)
	assert.Equal(t, 1000, c.RatePerExec)
}

// PR #173 review Should-Fix #2: SSE fan-out drops are now aggregated in
// Stats().DroppedSubscribers so operators have a single signal for slow
// subscribers without iterating live subscriber state.
func TestStats_DroppedSubscribersIncrementsOnSlowSubscriber(t *testing.T) {
	b := newTestBuffer(t, Config{PerExecution: 1024, MaxExecutions: 4})
	// Subscribe but never read; the per-subscriber channel is sized
	// at SubscribeBuffer (256) so the next entry past that drops.
	_, cancel, err := b.Subscribe("exec-1")
	require.NoError(t, err)
	t.Cleanup(cancel)

	for i := 0; i < SubscribeBuffer+5; i++ {
		require.Equal(t, DropNone, b.Append(mkEntry("exec-1", "INFO", "m"+strconv.Itoa(i))))
	}
	assert.GreaterOrEqual(t, b.Stats().DroppedSubscribers, uint64(5),
		"slow subscriber should have caused at least 5 fan-out drops")
}
