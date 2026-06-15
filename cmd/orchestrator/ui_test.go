package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
	"go.uber.org/zap/zaptest/observer"

	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/server"
	"github.com/mkhomutov/persatrix/internal/state"
)

// TestInitUI_DisabledLoadsTogglesButNotConsole pins the post-decoupling
// contract: --enable-ui=false still wires the feature toggles (one option,
// server.WithUIConfig) so non-web surfaces can read config/ui.yaml, but it does
// NOT serve the embedded console — the /ui/ assets and the /api/v1/ui/* boot
// endpoints stay a clean 404 (RFC 0048 §Security off-by-default).
func TestInitUI_DisabledLoadsTogglesButNotConsole(t *testing.T) {
	opts := initUI(false, t.TempDir(), zap.NewNop())
	require.Len(t, opts, 1,
		"--enable-ui=false must still wire the config/ui.yaml toggles (WithUIConfig) so the RFC 0050 config-edit gate is reachable")

	srv := buildUITestServer(t, opts...)
	hs := httptest.NewServer(srv.Handler())
	t.Cleanup(hs.Close)

	for _, path := range []string{"/ui/", "/api/v1/ui/config"} {
		resp, err := http.Get(hs.URL + path)
		require.NoError(t, err)
		_ = resp.Body.Close()
		assert.Equal(t, http.StatusNotFound, resp.StatusCode,
			"%s must 404 with the console disabled (assets are console-gated, not toggle-gated)", path)
	}
}

// TestInitUI_DisabledHonorsConfigEditToggle is the regression guard for the
// PR-643 finding: the RFC 0050 config-edit surface must be reachable WITHOUT
// --enable-ui. An operator opts into runtime config editing
// (config_edit_enabled: true in config/ui.yaml) but runs the orchestrator with
// the console off; the PATCH/GET /api/v1/channels/{id}/config endpoints — mounted
// unconditionally by registerChannelRoutes — must honor that toggle.
//
// The decisive signal is the status on a MISSING channel: a 404 proves the
// config_edit gate was passed (the toggle reached the server); the pre-fix
// behaviour was a 403, because initUI dropped server.WithUIConfig whenever the
// console was off, leaving server.uiConfig nil → the default-OFF fallback.
func TestInitUI_DisabledHonorsConfigEditToggle(t *testing.T) {
	cfgDir := t.TempDir()
	require.NoError(t, os.WriteFile(filepath.Join(cfgDir, "ui.yaml"), []byte(
		"panels:\n  channel_timeline:\n    enabled: true\n    config_edit_enabled: true\n"), 0o600))
	require.NoError(t, os.WriteFile(filepath.Join(cfgDir, "channels.yaml"),
		[]byte("max_channels: 50\n"), 0o644))

	status := configEndpointStatus(t, cfgDir, false /* enableUI */)
	assert.Equal(t, http.StatusNotFound, status,
		"config_edit_enabled:true must be honored with --enable-ui off (404 = gate passed, channel missing); a 403 is the regression")
}

// TestInitUI_DisabledConfigEditDefaultStillForbidden is the negative control:
// with the toggle at its default (OFF, no ui.yaml authored), the same disabled
// orchestrator still 403s the config endpoint — the gate works; the fix only
// stops initUI from silently dropping an operator's explicit opt-in.
func TestInitUI_DisabledConfigEditDefaultStillForbidden(t *testing.T) {
	cfgDir := t.TempDir()
	require.NoError(t, os.WriteFile(filepath.Join(cfgDir, "channels.yaml"),
		[]byte("max_channels: 50\n"), 0o644))

	status := configEndpointStatus(t, cfgDir, false /* enableUI */)
	assert.Equal(t, http.StatusForbidden, status,
		"default-off config_edit must still 403 even with channels wired (no 503), with the console off")
}

// configEndpointStatus wires a real channel store/router (via initChannels) plus
// the UI options (via initUI) onto a test server and returns the status of a GET
// against a missing channel's config endpoint. The channels wiring guarantees
// the endpoint is not a 503, so the result isolates the config_edit gate: 403
// (toggle off) vs 404 (toggle on, channel absent).
func configEndpointStatus(t *testing.T, cfgDir string, enableUI bool) int {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "channels.db")
	logger := zap.NewNop()
	chanOpts, cleanup, err := initChannels(cfgDir, dbPath, "", "", nil, nil, nil, logger)
	t.Cleanup(cleanup)
	require.NoError(t, err)
	require.NotEmpty(t, chanOpts, "channels must wire so the config endpoint is not a 503")

	opts := append(chanOpts, initUI(enableUI, cfgDir, logger)...)
	srv := buildUITestServer(t, opts...)
	hs := httptest.NewServer(srv.Handler())
	t.Cleanup(hs.Close)

	resp, err := http.Get(hs.URL + "/api/v1/channels/group:ghost/config")
	require.NoError(t, err)
	t.Cleanup(func() { _ = resp.Body.Close() })
	return resp.StatusCode
}

// TestInitUI_DisabledEmitsNoWarn guards that the disabled (default) path is
// silent — the security WARN must fire only when the console is actually on,
// or operators learn to ignore it.
func TestInitUI_DisabledEmitsNoWarn(t *testing.T) {
	core, recorded := observer.New(zapcore.WarnLevel)

	_ = initUI(false, t.TempDir(), zap.New(core))

	assert.Empty(t, recorded.FilterMessageSnippet("web console ENABLED").All(),
		"the disabled path must not emit the console-enabled security WARN")
}

// TestInitUI_EnabledWiresOption verifies --enable-ui=true appends exactly one
// server option carrying the embedded assets, and emits the load-bearing
// security WARN so the unauthenticated-surface posture is impossible to miss.
func TestInitUI_EnabledWiresOption(t *testing.T) {
	core, recorded := observer.New(zapcore.WarnLevel)

	opts := initUI(true, t.TempDir(), zap.New(core))

	require.Len(t, opts, 2,
		"--enable-ui=true must wire WithUI(ui.Assets()) + WithUIConfig(...)")
	warns := recorded.FilterMessageSnippet("web console ENABLED").All()
	require.Len(t, warns, 1,
		"the enabled path must emit exactly one console-enabled security WARN")
	assert.Equal(t, zapcore.WarnLevel, warns[0].Level,
		"the unauthenticated-surface notice must be a WARN")
}

// TestInitUI_EnabledServesPlaceholder is the end-to-end proof that the option
// returned by initUI(true) actually serves the committed placeholder asset
// from the internal/ui embed package — the "go build with no JS toolchain"
// guarantee surfaced through a flag-on binary.
func TestInitUI_EnabledServesPlaceholder(t *testing.T) {
	srv := buildUITestServer(t, initUI(true, t.TempDir(), zap.NewNop())...)
	hs := httptest.NewServer(srv.Handler())
	t.Cleanup(hs.Close)

	resp, err := http.Get(hs.URL + "/ui/")
	require.NoError(t, err)
	t.Cleanup(func() { _ = resp.Body.Close() })

	assert.Equal(t, http.StatusOK, resp.StatusCode,
		"a flag-on binary must serve the embedded placeholder shell at /ui/")
}

// TestInitUI_EnabledServesConfig is the end-to-end proof that the enabled path
// wires the config endpoint (RFC 0048 PR 2): a flag-on binary serves
// /api/v1/ui/config off the loaded (here: absent → default) ui.yaml.
func TestInitUI_EnabledServesConfig(t *testing.T) {
	srv := buildUITestServer(t, initUI(true, t.TempDir(), zap.NewNop())...)
	hs := httptest.NewServer(srv.Handler())
	t.Cleanup(hs.Close)

	resp, err := http.Get(hs.URL + "/api/v1/ui/config")
	require.NoError(t, err)
	t.Cleanup(func() { _ = resp.Body.Close() })

	assert.Equal(t, http.StatusOK, resp.StatusCode,
		"a flag-on binary must serve /api/v1/ui/config")
}

// TestInitUI_MalformedConfigSoftDegrades pins the channels.yaml-consistent
// posture: a malformed config/ui.yaml does not abort console wiring — initUI
// logs a WARN and falls back to defaults so the console still boots.
func TestInitUI_MalformedConfigSoftDegrades(t *testing.T) {
	cfgDir := t.TempDir()
	require.NoError(t, os.WriteFile(
		filepath.Join(cfgDir, "ui.yaml"), []byte("panels: [broken\n"), 0o600))

	core, recorded := observer.New(zapcore.WarnLevel)
	opts := initUI(true, cfgDir, zap.New(core))

	require.Len(t, opts, 2, "a malformed ui.yaml must still wire the console (defaults)")
	assert.NotEmpty(t, recorded.FilterMessageSnippet("ui.yaml").All(),
		"a malformed ui.yaml must surface a WARN")
}

// buildUITestServer constructs a minimal Server with the given options for the
// orchestrator-side wiring tests.
func buildUITestServer(t *testing.T, opts ...server.ServerOption) *server.Server {
	t.Helper()
	dir := t.TempDir()
	logger := zap.NewNop()
	store := state.NewInMemoryStore(logger)
	reg := registry.NewInMemoryRegistry(logger)
	pl := planner.NewYAMLPlanner(logger)
	srv, err := server.New("127.0.0.1:0", dir, store, reg, pl, logger, opts...)
	require.NoError(t, err)
	return srv
}
