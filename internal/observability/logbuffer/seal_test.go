package logbuffer

import (
	"path/filepath"
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

// A finished execution is sealed after a short grace, so agent entries
// still in the shipper's batch window are kept, and the sealed entries
// survive a restart.
func TestSealAfterGrace_KeepsLateEntriesAndSurvivesRestart(t *testing.T) {
	dir := t.TempDir()
	logger := zaptest.NewLogger(t)
	cfg := Config{Dir: dir, PerExecution: 8, MaxExecutions: 3, RatePerExec: 1000, SealGrace: 50 * time.Millisecond}

	b1, err := New(cfg, logger)
	require.NoError(t, err)
	require.Equal(t, DropNone, b1.Append(mkEntry("exec-grace", "INFO", "orchestrator: run completed")))
	b1.SealAfterGrace("exec-grace")
	// An agent entry arriving inside the grace window is still admitted.
	require.Equal(t, DropNone, b1.Append(mkEntry("exec-grace", "INFO", "agent: tail entry")))
	assert.Empty(t, sealFiles(t, dir, "exec-grace"), "nothing reaches disk before the grace ends")

	require.Eventually(t, func() bool { return len(sealFiles(t, dir, "exec-grace")) == 1 },
		2*time.Second, 10*time.Millisecond, "ring was not flushed to disk after the grace")
	require.NoError(t, b1.Close())

	b2, err := New(cfg, logger)
	require.NoError(t, err)
	t.Cleanup(func() { _ = b2.Close() })
	got := b2.Snapshot("exec-grace")
	require.Len(t, got, 2)
	assert.Equal(t, "orchestrator: run completed", got[0].Message)
	assert.Equal(t, "agent: tail entry", got[1].Message)
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
