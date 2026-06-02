package main

import (
	"net/http"
	"net/http/httptest"
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

// TestInitUI_DisabledByDefault is the Red assertion for the off-by-default
// posture (RFC 0048 §Security): --enable-ui=false yields no server option, so
// the /ui/ route is never registered.
func TestInitUI_DisabledByDefault(t *testing.T) {
	opts := initUI(false, zap.NewNop())
	assert.Empty(t, opts,
		"--enable-ui=false (default) must wire no UI option so /ui/ is never registered")
}

// TestInitUI_DisabledEmitsNoWarn guards that the disabled (default) path is
// silent — the security WARN must fire only when the console is actually on,
// or operators learn to ignore it.
func TestInitUI_DisabledEmitsNoWarn(t *testing.T) {
	core, recorded := observer.New(zapcore.WarnLevel)

	_ = initUI(false, zap.New(core))

	assert.Empty(t, recorded.FilterMessageSnippet("web console ENABLED").All(),
		"the disabled path must not emit the console-enabled security WARN")
}

// TestInitUI_EnabledWiresOption verifies --enable-ui=true appends exactly one
// server option carrying the embedded assets, and emits the load-bearing
// security WARN so the unauthenticated-surface posture is impossible to miss.
func TestInitUI_EnabledWiresOption(t *testing.T) {
	core, recorded := observer.New(zapcore.WarnLevel)

	opts := initUI(true, zap.New(core))

	require.Len(t, opts, 1,
		"--enable-ui=true must wire exactly one server option (WithUI(ui.Assets()))")
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
	srv := buildUITestServer(t, initUI(true, zap.NewNop())...)
	hs := httptest.NewServer(srv.Handler())
	t.Cleanup(hs.Close)

	resp, err := http.Get(hs.URL + "/ui/")
	require.NoError(t, err)
	t.Cleanup(func() { _ = resp.Body.Close() })

	assert.Equal(t, http.StatusOK, resp.StatusCode,
		"a flag-on binary must serve the embedded placeholder shell at /ui/")
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
