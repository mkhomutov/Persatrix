package server

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// RFC 0039 PR 3 — config/security.yaml loader (auth_config.go).

func writeSecurityYAML(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "security.yaml")
	require.NoError(t, os.WriteFile(path, []byte(content), 0o600))
	return path
}

func TestLoadSecurityConfigAbsentFileIsDefaults(t *testing.T) {
	cfg, err := LoadSecurityConfig(filepath.Join(t.TempDir(), "security.yaml"))
	require.NoError(t, err)
	assert.Equal(t, DefaultAuthConfig(), cfg)
}

func TestLoadSecurityConfigDefaults(t *testing.T) {
	cfg := DefaultAuthConfig()
	assert.Equal(t, AuthModeDisabled, cfg.Mode)
	assert.Equal(t, 24*time.Hour, cfg.SessionTTL)
	// OQ #2 (2026-07-29): the cookie TTL is deliberately shorter.
	assert.Equal(t, 8*time.Hour, cfg.CookieSessionTTL)
	// OQ #3 (2026-07-29): per-source 10/60s, per-username 5/60s, own 1000-key LRUs.
	assert.Equal(t, AuthLimiterConfig{CallsPerWindow: 10, WindowSeconds: 60, MaxTracked: 1000}, cfg.LoginPerSource)
	assert.Equal(t, AuthLimiterConfig{CallsPerWindow: 5, WindowSeconds: 60, MaxTracked: 1000}, cfg.LoginPerUsername)
	assert.Empty(t, cfg.TrustedProxies)
}

func TestLoadSecurityConfigEmptyFileIsDefaults(t *testing.T) {
	cfg, err := LoadSecurityConfig(writeSecurityYAML(t, "# all comments\n"))
	require.NoError(t, err)
	assert.Equal(t, DefaultAuthConfig(), cfg)
}

func TestLoadSecurityConfigPartialOverridesOnlyNamedFields(t *testing.T) {
	cfg, err := LoadSecurityConfig(writeSecurityYAML(t, "auth:\n  mode: enabled\n  cookie_session_ttl: 2h\n"))
	require.NoError(t, err)
	assert.Equal(t, AuthModeEnabled, cfg.Mode)
	assert.Equal(t, 2*time.Hour, cfg.CookieSessionTTL)
	// Unnamed fields keep their defaults.
	assert.Equal(t, 24*time.Hour, cfg.SessionTTL)
	assert.Equal(t, DefaultAuthConfig().LoginPerSource, cfg.LoginPerSource)
}

func TestLoadSecurityConfigShippedFileMatchesDefaults(t *testing.T) {
	// The committed config/security.yaml authors every default explicitly;
	// loading it must equal the zero-config defaults so the shipped file
	// and the loader can never drift apart silently.
	cfg, err := LoadSecurityConfig(filepath.Join("..", "..", "config", "security.yaml"))
	require.NoError(t, err)
	assert.Equal(t, DefaultAuthConfig(), cfg)
}

func TestLoadSecurityConfigRejectsBadValues(t *testing.T) {
	cases := map[string]string{
		"unknown mode":      "auth:\n  mode: on\n",
		"unknown key":       "auth:\n  mod: enabled\n", // KnownFields — typos fail loud
		"unparsable ttl":    "auth:\n  session_ttl: soon\n",
		"sub-second ttl":    "auth:\n  cookie_session_ttl: 500ms\n",
		"zero limiter":      "auth:\n  login_throttle:\n    per_source:\n      calls_per_window: 0\n",
		"negative window":   "auth:\n  login_throttle:\n    per_username:\n      window_seconds: -5\n",
		"zero argon memory": "auth:\n  password:\n    argon2_memory_kib: 0\n",
		// The schema's 8 MiB floor is mirrored by the loader — the
		// semantic authority — so bypassing `make validate` cannot boot
		// a KDF weak enough to defeat its purpose (review follow-up).
		"sub-floor argon memory": "auth:\n  password:\n    argon2_memory_kib: 4096\n",
		"bad trusted proxy":      "auth:\n  trusted_proxies: [not-an-ip]\n",
		"non-mapping block":      "auth: enabled\n",
		"unknown top-level":      "authz:\n  mode: enabled\n",
	}
	for name, yaml := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := LoadSecurityConfig(writeSecurityYAML(t, yaml))
			assert.Error(t, err)
		})
	}
}

func TestLoadSecurityConfigTrustedProxies(t *testing.T) {
	cfg, err := LoadSecurityConfig(writeSecurityYAML(t,
		"auth:\n  trusted_proxies: [\"10.0.0.0/8\", \"192.168.1.7\", \"fd00::1\"]\n"))
	require.NoError(t, err)
	require.Len(t, cfg.TrustedProxies, 3)
	// A bare address folds to a single-host network.
	ones, bits := cfg.TrustedProxies[1].Mask.Size()
	assert.Equal(t, 32, ones)
	assert.Equal(t, 32, bits)
	ones, bits = cfg.TrustedProxies[2].Mask.Size()
	assert.Equal(t, 128, ones)
	assert.Equal(t, 128, bits)
}

func TestLoadSecurityConfigLimiterPartialOverride(t *testing.T) {
	cfg, err := LoadSecurityConfig(writeSecurityYAML(t,
		"auth:\n  login_throttle:\n    per_source:\n      calls_per_window: 3\n"))
	require.NoError(t, err)
	assert.Equal(t, AuthLimiterConfig{CallsPerWindow: 3, WindowSeconds: 60, MaxTracked: 1000}, cfg.LoginPerSource)
	assert.Equal(t, DefaultAuthConfig().LoginPerUsername, cfg.LoginPerUsername)
}
