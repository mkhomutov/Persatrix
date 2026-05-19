package wallet

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// RFC 0023 PR 2 — the `wallet:` block of config/optimization.yaml. The block
// is optional: an absent block (or an absent key) falls back to DefaultConfig
// values, so a deployment that never tunes the wallet still gets the RFC 0023
// § B / Open Question §2 defaults.

func writeOptimizationYAML(t *testing.T, body string) string {
	t.Helper()
	dir := t.TempDir()
	require.NoError(t, os.WriteFile(filepath.Join(dir, "optimization.yaml"), []byte(body), 0o644))
	return dir
}

// TestDefaultConfig pins the RFC 0023 default lease-lifecycle tuning: a 60 s
// TTL (2× the 30 s default per-call timeout — OQ §2), a 5 s reaper interval,
// and a 16-lease per-agent concurrency ceiling (Security Considerations).
func TestDefaultConfig(t *testing.T) {
	c := DefaultConfig()
	assert.Equal(t, 60*time.Second, c.TTL)
	assert.Equal(t, 5*time.Second, c.ReaperInterval)
	assert.Equal(t, 16, c.MaxActiveLeases)
}

// TestLoadConfig_FullBlock pins that an explicit wallet block overrides every
// default.
func TestLoadConfig_FullBlock(t *testing.T) {
	dir := writeOptimizationYAML(t, `
schema_version: "0.1"
wallet:
  ttl_seconds: 90
  reaper_interval_seconds: 10
  max_active_leases: 32
`)
	c, err := LoadConfig(dir)
	require.NoError(t, err)
	assert.Equal(t, 90*time.Second, c.TTL)
	assert.Equal(t, 10*time.Second, c.ReaperInterval)
	assert.Equal(t, 32, c.MaxActiveLeases)
}

// TestLoadConfig_MissingBlock pins that a config file with no wallet block at
// all loads cleanly as DefaultConfig — the block is optional.
func TestLoadConfig_MissingBlock(t *testing.T) {
	dir := writeOptimizationYAML(t, `schema_version: "0.1"
`)
	c, err := LoadConfig(dir)
	require.NoError(t, err)
	assert.Equal(t, DefaultConfig(), c)
}

// TestLoadConfig_PartialBlock pins per-key default fallback: a wallet block
// that sets only ttl_seconds keeps the defaults for the other two keys.
func TestLoadConfig_PartialBlock(t *testing.T) {
	dir := writeOptimizationYAML(t, `
schema_version: "0.1"
wallet:
  ttl_seconds: 120
`)
	c, err := LoadConfig(dir)
	require.NoError(t, err)
	assert.Equal(t, 120*time.Second, c.TTL)
	assert.Equal(t, DefaultConfig().ReaperInterval, c.ReaperInterval)
	assert.Equal(t, DefaultConfig().MaxActiveLeases, c.MaxActiveLeases)
}

// TestLoadConfig_MissingFile pins that an absent optimization.yaml is an
// error the caller can fall back on.
func TestLoadConfig_MissingFile(t *testing.T) {
	_, err := LoadConfig(t.TempDir())
	require.Error(t, err)
	assert.Contains(t, err.Error(), "read optimization config")
}

// TestLoadConfig_InvalidYAML pins that a malformed file errors rather than
// silently yielding defaults.
func TestLoadConfig_InvalidYAML(t *testing.T) {
	dir := writeOptimizationYAML(t, ":::not yaml")
	_, err := LoadConfig(dir)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "parse optimization config")
}

// TestLoadConfig_NegativeValues pins validation: a negative tuning value is
// rejected with a message naming the offending key.
func TestLoadConfig_NegativeValues(t *testing.T) {
	tests := []struct {
		name string
		body string
		want string
	}{
		{
			name: "negative ttl",
			body: "wallet:\n  ttl_seconds: -1\n",
			want: "ttl_seconds",
		},
		{
			name: "negative reaper interval",
			body: "wallet:\n  reaper_interval_seconds: -5\n",
			want: "reaper_interval_seconds",
		},
		{
			name: "negative max active leases",
			body: "wallet:\n  max_active_leases: -16\n",
			want: "max_active_leases",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			dir := writeOptimizationYAML(t, tc.body)
			_, err := LoadConfig(dir)
			require.Error(t, err)
			assert.Contains(t, err.Error(), tc.want)
		})
	}
}
