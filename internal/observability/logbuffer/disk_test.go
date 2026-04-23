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

func TestDiskStore_DirectoryCreatedWith0700(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "nested", "logs")
	_, err := newDiskStore(dir, 0, zaptest.NewLogger(t))
	require.NoError(t, err)
	info, err := stat(dir)
	require.NoError(t, err)
	assert.True(t, info.IsDir())
	// POSIX-only assertion: Windows does not honour the umask passed
	// to MkdirAll and reports 0o777; skip the bit check there.
	if runtime.GOOS == "windows" {
		return
	}
	mode := info.Mode().Perm()
	assert.Equal(t, os.FileMode(0), mode&0o077, "world/group bits must be unset (mode=%o)", mode)
}

func TestDiskStore_FlushAndRoundTrip(t *testing.T) {
	dir := t.TempDir()
	d, err := newDiskStore(dir, 0, zaptest.NewLogger(t))
	require.NoError(t, err)

	entries := []Entry{
		{SchemaVersion: "1", Timestamp: time.Unix(1, 0).UTC(), Level: "INFO", Message: "a", ExecutionID: "x"},
		{SchemaVersion: "1", Timestamp: time.Unix(2, 0).UTC(), Level: "INFO", Message: "b", ExecutionID: "x"},
	}
	require.NoError(t, d.flush("x", entries))

	got := d.read("x")
	require.Len(t, got, 2)
	assert.Equal(t, "a", got[0].Message)
	assert.Equal(t, "b", got[1].Message)
}

func TestDiskStore_EvictsOldestWhenOverCap(t *testing.T) {
	dir := t.TempDir()

	entries1 := []Entry{{SchemaVersion: "1", Timestamp: time.Unix(1, 0).UTC(), Level: "INFO", Message: "first", ExecutionID: "old"}}
	entries2 := []Entry{{SchemaVersion: "1", Timestamp: time.Unix(2, 0).UTC(), Level: "INFO", Message: "second", ExecutionID: "new"}}

	// Size the cap to fit exactly one of the two flushes (with
	// headroom) but not both — eviction must drop "old" once "new"
	// arrives.
	probe, err := newDiskStore(t.TempDir(), 0, zaptest.NewLogger(t))
	require.NoError(t, err)
	require.NoError(t, probe.flush("probe", entries1))
	perFlush := probe.usage.Load()
	require.Greater(t, perFlush, int64(0))

	d, err := newDiskStore(dir, perFlush+5, zaptest.NewLogger(t))
	require.NoError(t, err)
	require.NoError(t, d.flush("old", entries1))
	// Force mtime ordering.
	time.Sleep(15 * time.Millisecond)
	require.NoError(t, d.flush("new", entries2))

	assert.Empty(t, d.read("old"), "oldest execution must be evicted under disk cap")
	assert.NotEmpty(t, d.read("new"))
}

func TestRateLimit_RefillsOverTime(t *testing.T) {
	tb := newTokenBucket(10)
	// Drain.
	for i := 0; i < 10; i++ {
		assert.True(t, tb.allow())
	}
	assert.False(t, tb.allow(), "bucket must be empty after draining capacity")

	// Inject a virtual 1-second gap; bucket should refill.
	tb.now = func() time.Time { return time.Now().Add(1 * time.Second) }
	assert.True(t, tb.allow(), "token must refill after one second")
}

func TestRateLimit_ZeroRateAllowsEverything(t *testing.T) {
	tb := newTokenBucket(0)
	for i := 0; i < 100; i++ {
		assert.True(t, tb.allow())
	}
}

func TestLevelGE_Ordering(t *testing.T) {
	assert.True(t, levelGE("ERROR", "WARN"))
	assert.True(t, levelGE("WARN", "WARN"))
	assert.False(t, levelGE("INFO", "WARN"))
	// Unknown actual level treated as INFO (fail-open).
	assert.True(t, levelGE("???", "DEBUG"))
	assert.True(t, levelGE("???", "INFO"))
	assert.False(t, levelGE("???", "WARN"))
	// Unknown threshold treated as DEBUG, so DEBUG passes the gate.
	assert.True(t, levelGE("DEBUG", "???"))
}
