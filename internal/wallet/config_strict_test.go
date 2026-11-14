package wallet

import (
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// A misspelled key in the `wallet:` block is refused (ISSUE-0211). The plain
// decoder dropped it, so the setting silently stayed at its default.

func TestLoadConfig_RefusesAKeyItDoesNotKnow(t *testing.T) {
	tests := []struct {
		name        string
		block       string
		wantInError string
	}{
		{"ttl_seconds misspelled", "  ttl_second: 90\n", "ttl_second"},
		{"reaper_interval_seconds misspelled", "  reaper_interval: 10\n", "reaper_interval"},
		{"max_active_leases misspelled", "  max_active_lease: 32\n", "max_active_lease"},
		{"an extra key beside the three", "  ttl_seconds: 90\n  enabled: true\n", "enabled"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			dir := writeOptimizationYAML(t, "schema_version: \"0.3\"\nwallet:\n"+tt.block)

			_, err := LoadConfig(dir)

			require.Error(t, err)
			assert.Contains(t, err.Error(), "parse optimization config")
			assert.Contains(t, err.Error(), "field "+tt.wantInError+" not found")
		})
	}
}

// The rest of the file belongs to other loaders: this one reads past it,
// whatever it holds.
func TestLoadConfig_ReadsPastWhatItDoesNotOwn(t *testing.T) {
	tests := []struct {
		name string
		rest string
	}{
		{"a profile under a name of the operator's", "aggressive:\n  caching:\n    exact:\n      enabled: false\n"},
		{"the cost block", "cost:\n  budgets:\n    global:\n      max_daily_usd: 100\n"},
		{"a cost block the cost loader would refuse", "cost:\n  budgetz: {}\n"},
		{"the other blocks", "models:\n  aliases: {}\nconversation_window:\n  enabled: true\nmemory_budget:\n  tokens: 1\n"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			dir := writeOptimizationYAML(t, tt.rest+"wallet:\n  ttl_seconds: 90\n")

			c, err := LoadConfig(dir)

			require.NoError(t, err)
			assert.Equal(t, 90*time.Second, c.TTL)
			assert.Equal(t, DefaultConfig().ReaperInterval, c.ReaperInterval)
		})
	}
}

// Every optimization.yaml the repository ships loads: the root config and
// each demo's.
func TestLoadConfig_EveryShippedConfigLoads(t *testing.T) {
	root := filepath.Join("..", "..", "config")
	dirs := []string{root}
	demos, err := filepath.Glob(filepath.Join(root, "demo", "*", "optimization.yaml"))
	require.NoError(t, err)
	require.Len(t, demos, 7, "a demo was added or removed: this list is every shipped config")
	for _, f := range demos {
		dirs = append(dirs, filepath.Dir(f))
	}
	for _, dir := range dirs {
		t.Run(dir, func(t *testing.T) {
			c, err := LoadConfig(dir)

			require.NoError(t, err)
			assert.Equal(t, DefaultConfig(), c, "the shipped files state the defaults")
		})
	}
}
