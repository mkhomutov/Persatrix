package main

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap/zaptest"
)

// TestInitChannels_CreatesParentDirectory is the Red step for ISSUE-0012
// (PR #245 review Low; PR #246 finding L1).
//
// Without os.MkdirAll, sqlite.Open fails when the parent directory does not
// exist (data/ is gitignored on fresh checkouts). initChannels swallows the
// store-open error and returns nil opts, making all seven channel REST
// endpoints return 503 with only a WARN log — no indication that the fix is
// as simple as creating the directory.
//
// This test drives the fix: initChannels must proactively ensure the parent
// directory exists before calling channels.NewSQLiteStore.
func TestInitChannels_CreatesParentDirectory(t *testing.T) {
	// Write a minimal channels.yaml so LoadConfig does not short-circuit to
	// the "config absent" early return before we ever reach NewSQLiteStore.
	cfgDir := t.TempDir()
	require.NoError(t, os.WriteFile(
		filepath.Join(cfgDir, "channels.yaml"),
		[]byte("max_channels: 50\n"),
		0o644,
	))

	// Point the DB path at a non-existent nested subdirectory.
	dbDir := filepath.Join(t.TempDir(), "nested", "subdir")
	dbPath := filepath.Join(dbDir, "channels.db")

	// Pre-condition: the directory must not exist yet.
	_, statErr := os.Stat(dbDir)
	require.ErrorIs(t, statErr, os.ErrNotExist,
		"pre-condition: dbDir must not exist before initChannels is called")

	logger := zaptest.NewLogger(t)
	opts, cleanup, err := initChannels(cfgDir, dbPath, nil, nil, logger)
	t.Cleanup(cleanup)

	require.NoError(t, err, "initChannels must not return a hard reconcile error")

	// Without the MkdirAll fix initChannels returns nil opts (the store-open
	// fails silently). The assertion below is the failing condition before the
	// fix is applied.
	assert.NotEmpty(t, opts,
		"initChannels must return a server option (store opened successfully); "+
			"got nil — parent directory was not created before sqlite.Open (ISSUE-0012)")

	assert.DirExists(t, dbDir,
		"initChannels must have created the db parent directory")
}

// TestInitChannels_SkipsMkdirAllForMemoryPath verifies that the special
// ":memory:" SQLite path does not trigger os.MkdirAll with filepath.Dir
// (which would produce a literal "." or ":" path on some OSes).
func TestInitChannels_SkipsMkdirAllForMemoryPath(t *testing.T) {
	cfgDir := t.TempDir()
	require.NoError(t, os.WriteFile(
		filepath.Join(cfgDir, "channels.yaml"),
		[]byte("max_channels: 50\n"),
		0o644,
	))

	logger := zaptest.NewLogger(t)
	opts, cleanup, err := initChannels(cfgDir, ":memory:", nil, nil, logger)
	t.Cleanup(cleanup)

	require.NoError(t, err)
	assert.NotEmpty(t, opts,
		":memory: path must open the in-process store without a MkdirAll call")
}
