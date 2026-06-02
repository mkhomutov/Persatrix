package server

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestDefaultUIConfig pins the Slice-1 panel defaults (RFC 0048 §C): chat and
// channel_timeline ship enabled; memory_strip (Slice 2) and cost (Slice 4) ship
// off so they land additively with no Slice-1 rework.
func TestDefaultUIConfig(t *testing.T) {
	cfg := DefaultUIConfig()
	require.NotNil(t, cfg)

	assert.True(t, cfg.PanelEnabled("chat"), "chat ships enabled in Slice 1")
	assert.True(t, cfg.PanelEnabled("channel_timeline"), "channel_timeline ships enabled in Slice 1")
	assert.False(t, cfg.PanelEnabled("memory_strip"), "memory_strip ships off (Slice 2)")
	assert.False(t, cfg.PanelEnabled("cost"), "cost ships off (Slice 4)")
}

// TestLoadUIConfig_AbsentReturnsDefaults: an absent config/ui.yaml is the
// expected zero-config case, not an error — the loader returns the Slice-1
// defaults so a flag-on binary with no ui.yaml renders the hero panels.
func TestLoadUIConfig_AbsentReturnsDefaults(t *testing.T) {
	cfg, err := LoadUIConfig(filepath.Join(t.TempDir(), "ui.yaml"))
	require.NoError(t, err, "an absent ui.yaml must soft-degrade to defaults, not error")
	require.NotNil(t, cfg)

	assert.True(t, cfg.PanelEnabled("chat"))
	assert.True(t, cfg.PanelEnabled("channel_timeline"))
	assert.False(t, cfg.PanelEnabled("memory_strip"))
}

// TestLoadUIConfig_ParsesToggles: a present ui.yaml overrides the defaults the
// operator names and leaves the rest at their default.
func TestLoadUIConfig_ParsesToggles(t *testing.T) {
	path := filepath.Join(t.TempDir(), "ui.yaml")
	require.NoError(t, os.WriteFile(path, []byte(
		"panels:\n  chat:\n    enabled: false\n  channel_timeline:\n    enabled: true\n"), 0o600))

	cfg, err := LoadUIConfig(path)
	require.NoError(t, err)

	assert.False(t, cfg.PanelEnabled("chat"), "an explicit enabled:false must turn the panel off")
	assert.True(t, cfg.PanelEnabled("channel_timeline"))
}

// TestLoadUIConfig_Malformed: a syntactically broken ui.yaml is an operator bug
// we surface loudly (the caller logs + soft-degrades), mirroring channels.yaml's
// parse-error posture — distinct from the absent-file default path.
func TestLoadUIConfig_Malformed(t *testing.T) {
	path := filepath.Join(t.TempDir(), "ui.yaml")
	require.NoError(t, os.WriteFile(path, []byte("panels: [this is not a map\n"), 0o600))

	_, err := LoadUIConfig(path)
	assert.Error(t, err, "a malformed ui.yaml must return a parse error, not silently default")
}
