package main

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
	"go.uber.org/zap/zaptest/observer"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/society"
)

// openChannelStore opens a channels store in a temporary file, as
// initChannels does at startup.
func openChannelStore(t *testing.T) channels.ChannelStore {
	t.Helper()
	cs, err := channels.NewSQLiteStore(filepath.Join(t.TempDir(), "channels.db"), channels.SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = cs.Close() })
	return cs
}

// societyLines returns the society lines a startup logged.
func societyLines(logs *observer.ObservedLogs) []observer.LoggedEntry {
	return logs.FilterMessageSnippet("society:").AllUntimed()
}

// TestOpenSociety_RunsOnTheChannelStoresConnection pins RFC 0029 Phase 3
// plan S-2 at startup: while a society transaction runs, the channels
// store's one connection is the one in use, so the society store opened no
// connection of its own to the same file.
func TestOpenSociety_RunsOnTheChannelStoresConnection(t *testing.T) {
	cs := openChannelStore(t)
	st, err := openSociety(cs)
	require.NoError(t, err)

	conn := cs.(society.ConnSource).SocietyConn()
	require.NoError(t, st.InTx(context.Background(), func(ctx context.Context, tx society.Tx) error {
		assert.Equal(t, 1, conn.Stats().InUse,
			"the society transaction must hold the channels store's connection")
		_, err := tx.Graph(ctx)
		return err
	}))
	assert.Equal(t, 0, conn.Stats().InUse)
}

// TestInitSociety_SaysWhenSocietyFeaturesAreOff covers the three ways
// startup can go: a channels store hands its connection over and the server
// gets the society store; channels are off, so there is no society store; or
// a channels store hands over no connection. Each says so in its own line.
func TestInitSociety_SaysWhenSocietyFeaturesAreOff(t *testing.T) {
	tests := []struct {
		name      string
		store     func(t *testing.T) channels.ChannelStore
		wantOpts  int
		wantLevel zapcore.Level
		wantMsg   string
	}{
		{
			name:      "the channels store hands its connection over",
			store:     openChannelStore,
			wantOpts:  1,
			wantLevel: zapcore.InfoLevel,
			wantMsg:   "society: store ready on the channels store's connection",
		},
		{
			name:      "channels are off",
			store:     func(*testing.T) channels.ChannelStore { return nil },
			wantLevel: zapcore.InfoLevel,
			wantMsg:   "society: channels are off, so there is no society store; society features are off",
		},
		{
			name: "the channels store hands over no connection",
			// A store that is not the SQLite one has no SocietyConn.
			store:     func(*testing.T) channels.ChannelStore { return struct{ channels.ChannelStore }{} },
			wantLevel: zapcore.WarnLevel,
			wantMsg:   "society: store unavailable; society features are off",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			core, logs := observer.New(zapcore.DebugLevel)
			opts := initSociety(tt.store(t), zap.New(core))
			assert.Len(t, opts, tt.wantOpts)
			lines := societyLines(logs)
			require.Len(t, lines, 1)
			assert.Equal(t, tt.wantLevel, lines[0].Level)
			assert.Equal(t, tt.wantMsg, lines[0].Message)
		})
	}
}

// TestInitChannels_BringsTheSocietyStoreUpWithTheChannelsStore pins where
// the society store comes from at startup: initChannels builds it whenever
// the channels store opens, and says society features are off on each path
// where none opens.
func TestInitChannels_BringsTheSocietyStoreUpWithTheChannelsStore(t *testing.T) {
	withConfig := func(t *testing.T) string {
		dir := t.TempDir()
		require.NoError(t, os.WriteFile(filepath.Join(dir, "channels.yaml"), []byte("max_channels: 50\n"), 0o644))
		return dir
	}
	tests := []struct {
		name     string
		cfgDir   func(t *testing.T) string
		dbPath   func(t *testing.T) string
		wantOpts int
		wantMsg  string
	}{
		{
			name:   "channels on",
			cfgDir: withConfig,
			dbPath: func(t *testing.T) string { return filepath.Join(t.TempDir(), "channels.db") },
			// The channels pair, then the society store's.
			wantOpts: 3,
			wantMsg:  "society: store ready on the channels store's connection",
		},
		{
			name:    "channels.yaml absent",
			cfgDir:  func(t *testing.T) string { return t.TempDir() },
			dbPath:  func(t *testing.T) string { return filepath.Join(t.TempDir(), "channels.db") },
			wantMsg: "society: channels are off, so there is no society store; society features are off",
		},
		{
			name:   "the channels store cannot open",
			cfgDir: withConfig,
			// A directory where the database file should be.
			dbPath:  func(t *testing.T) string { return t.TempDir() },
			wantMsg: "society: channels are off, so there is no society store; society features are off",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			core, logs := observer.New(zapcore.InfoLevel)
			opts, cleanup, err := initChannels(tt.cfgDir(t), tt.dbPath(t), "", "", nil, nil, nil, zap.New(core))
			t.Cleanup(cleanup)
			require.NoError(t, err)
			assert.Len(t, opts, tt.wantOpts)
			lines := societyLines(logs)
			require.Len(t, lines, 1)
			assert.Equal(t, tt.wantMsg, lines[0].Message)
		})
	}
}
