package main

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"

	"github.com/mkhomutov/persatrix/internal/server"
)

// RFC 0039 PR 3 — initAuth wiring (auth.go): config load posture, store
// creation, and the §H / §B3 startup WARNs.

func TestInitAuthZeroConfigDefaults(t *testing.T) {
	cfgDir := t.TempDir() // no security.yaml — the zero-config default
	dbPath := filepath.Join(t.TempDir(), "nested", "accounts.db")

	opts, cleanup, err := initAuth(cfgDir, dbPath, "127.0.0.1", zap.NewNop())
	require.NoError(t, err)
	defer cleanup()
	assert.Len(t, opts, 1)
	assert.FileExists(t, dbPath, "the store opens (and creates parents) under both modes — Phase 1 ships the mechanism inert, not absent")
}

func TestInitAuthMalformedConfigFailsLoud(t *testing.T) {
	cfgDir := t.TempDir()
	require.NoError(t, os.WriteFile(filepath.Join(cfgDir, "security.yaml"),
		[]byte("auth:\n  mode: enbaled\n"), 0o600)) // the typo the loud-fail posture exists for

	_, _, err := initAuth(cfgDir, filepath.Join(t.TempDir(), "accounts.db"), "127.0.0.1", zap.NewNop())
	assert.Error(t, err, "a typo'd mode must fail startup, not silently boot unauthenticated")
}

func TestBindIsLoopback(t *testing.T) {
	assert.True(t, bindIsLoopback("127.0.0.1"))
	assert.True(t, bindIsLoopback("127.0.0.1:8080"))
	assert.True(t, bindIsLoopback("localhost"))
	assert.True(t, bindIsLoopback("::1"))
	assert.False(t, bindIsLoopback("0.0.0.0"))
	assert.False(t, bindIsLoopback("192.168.1.5"))
	assert.False(t, bindIsLoopback(""), "unparsable → non-loopback so the WARN errs on caution")
}

func TestWarnAuthPosture(t *testing.T) {
	warns := func(cfg *server.AuthConfig, bind string) []string {
		core, logs := observer.New(zap.WarnLevel)
		warnAuthPosture(zap.New(core), cfg, bind)
		var msgs []string
		for _, e := range logs.All() {
			msgs = append(msgs, e.Message)
		}
		return msgs
	}

	disabled := server.DefaultAuthConfig()
	assert.Empty(t, warns(disabled, "127.0.0.1"), "loopback + disabled is the quiet default")
	assert.Len(t, warns(disabled, "0.0.0.0"), 1, "§H: disabled on a non-loopback bind WARNs")

	enabled := server.DefaultAuthConfig()
	enabled.Mode = server.AuthModeEnabled
	assert.Len(t, warns(enabled, "0.0.0.0"), 1, "§B3: enabled + non-loopback + no trusted_proxies WARNs")
	assert.Empty(t, warns(enabled, "127.0.0.1"), "loopback + enabled needs no proxy config")
}
