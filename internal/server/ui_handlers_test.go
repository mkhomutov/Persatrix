package server

import (
	"encoding/json"
	"net/http"
	"testing"
	"testing/fstest"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// uiTestMarker is the sentinel body the in-test embed FS serves at /ui/, so
// the assertions confirm the static handler reaches the asset tree rather than
// any other route. (The real placeholder body — "run `make ui`" — is asserted
// by the internal/ui package's own compile-time presence, not here.)
const uiTestMarker = "PERSATRIX-UI-TEST-MARKER"

// uiTestServer builds a Server with WithUI wired over an in-memory asset FS
// carrying a known marker at index.html (RFC 0048 Phase 1 PR 1). Mirrors the
// other *TestServer helpers in this package.
func uiTestServer(t *testing.T, opts ...ServerOption) *Server {
	t.Helper()
	dir := t.TempDir()
	logger := zap.NewNop()
	store := state.NewInMemoryStore(logger)
	reg := registry.NewInMemoryRegistry(logger)
	pl := planner.NewYAMLPlanner(logger)
	srv, err := New("127.0.0.1:0", dir, store, reg, pl, logger, opts...)
	require.NoError(t, err)
	return srv
}

// uiAssetFS is the in-memory asset tree the WithUI tests serve. A real bundle
// would carry hashed JS/CSS too; the marker on index.html is all the static
// handler needs to prove the wiring.
func uiAssetFS() fstest.MapFS {
	return fstest.MapFS{
		"index.html": &fstest.MapFile{
			Data: []byte("<!doctype html><title>" + uiTestMarker + "</title>"),
		},
	}
}

// TestWithUI_ServesShell is the load-bearing Red assertion for PR 1: with
// WithUI wired, GET /ui/ serves the embedded index.html (200 + marker body).
func TestWithUI_ServesShell(t *testing.T) {
	srv := uiTestServer(t, WithUI(uiAssetFS()))

	rec := doRequest(srv.Handler(), http.MethodGet, "/ui/", nil)

	require.Equal(t, http.StatusOK, rec.Code,
		"GET /ui/ must serve the embedded shell when WithUI is wired")
	assert.Contains(t, rec.Body.String(), uiTestMarker,
		"the served body must come from the injected asset FS (index.html)")
}

// TestWithUI_RedirectsBareUITrailingSlash verifies net/http's subtree-pattern
// redirect: GET /ui (no trailing slash) redirects to /ui/, so no explicit /ui
// route is needed. The exact 3xx code is net/http's choice (307 on this Go
// version); the contract this asserts is "bare /ui lands at the /ui/ subtree".
func TestWithUI_RedirectsBareUITrailingSlash(t *testing.T) {
	srv := uiTestServer(t, WithUI(uiAssetFS()))

	rec := doRequest(srv.Handler(), http.MethodGet, "/ui", nil)

	assert.GreaterOrEqual(t, rec.Code, 300, "GET /ui must redirect (3xx)")
	assert.Less(t, rec.Code, 400, "GET /ui must redirect (3xx)")
	assert.Equal(t, "/ui/", rec.Header().Get("Location"),
		"the redirect target must be the /ui/ subtree")
}

// TestWithUI_NoDirectoryListing hardens the static surface: a bare
// http.FileServer renders an auto-generated listing for any directory lacking
// an index.html, which on this deliberately-unauthenticated console surface
// (RFC 0048 §Security) would expose the bundle's internal file names. PR 3's
// Svelte/Vite output ships exactly such subdirectories (e.g. _app/, .vite/), so
// the listing must be a clean 404 while individual hashed assets stay reachable
// by their direct path. The scaffold today has no subdirs, so this test
// supplies one via the injected FS to lock the contract before PR 3 relies on
// it.
func TestWithUI_NoDirectoryListing(t *testing.T) {
	fsys := fstest.MapFS{
		"index.html": &fstest.MapFile{
			Data: []byte("<!doctype html><title>" + uiTestMarker + "</title>"),
		},
		"_app/immutable/chunk.js": &fstest.MapFile{Data: []byte("export const x = 1")},
	}
	srv := uiTestServer(t, WithUI(fsys))

	listing := doRequest(srv.Handler(), http.MethodGet, "/ui/_app/immutable/", nil)
	assert.Equal(t, http.StatusNotFound, listing.Code,
		"a directory without index.html must 404, not serve a file listing")

	asset := doRequest(srv.Handler(), http.MethodGet, "/ui/_app/immutable/chunk.js", nil)
	assert.Equal(t, http.StatusOK, asset.Code,
		"hardening must not break serving hashed assets by their direct path")
}

// TestWithoutUI_NotFound is the nil-safe gate: with no WithUI option the /ui/
// route is never registered, so it is a clean 404 and the rest of the surface
// is untouched.
func TestWithoutUI_NotFound(t *testing.T) {
	srv := uiTestServer(t) // no WithUI

	rec := doRequest(srv.Handler(), http.MethodGet, "/ui/", nil)

	assert.Equal(t, http.StatusNotFound, rec.Code,
		"without WithUI, /ui/ must be a clean 404 (route never registered)")
}

// TestWithoutUI_ExistingRoutesUnaffected guards that wiring (or not wiring) the
// UI scaffold never perturbs the existing surface — /healthz and the agents
// list keep responding exactly as before.
func TestWithoutUI_ExistingRoutesUnaffected(t *testing.T) {
	srv := uiTestServer(t) // no WithUI

	health := doRequest(srv.Handler(), http.MethodGet, "/healthz", nil)
	assert.Equal(t, http.StatusOK, health.Code, "/healthz must be unaffected by the UI scaffold")

	agents := doRequest(srv.Handler(), http.MethodGet, "/api/v1/agents", nil)
	assert.Equal(t, http.StatusOK, agents.Code, "GET /api/v1/agents must be unaffected by the UI scaffold")
}

// uiChannelStore builds a throwaway SQLite-backed channel store so a test can
// wire WithChannels and exercise the runtime-derived `available` flag.
func uiChannelStore(t *testing.T) channels.ChannelStore {
	t.Helper()
	store, err := channels.NewSQLiteStore(":memory:", channels.SQLiteOptions{
		MaxChannels: 50,
		Logger:      zap.NewNop(),
	})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	return store
}

// uiConfigResponseBody is the test-side mirror of the /api/v1/ui/config JSON
// (RFC 0048 §C). Kept here, not imported from the handler, so the test pins the
// wire shape independently of the server's internal struct.
type uiConfigResponseBody struct {
	Panels map[string]struct {
		Enabled   bool `json:"enabled"`
		Available bool `json:"available"`
	} `json:"panels"`
	Build struct {
		Version string `json:"version"`
	} `json:"build"`
}

// TestUIConfig_Shape pins the RFC 0048 §C config contract: the Slice-1 panel
// toggles default on for chat/channel_timeline and off for memory_strip/cost,
// and a non-empty build.version ships so the console can show what it is
// rendering. channels wired → channel_timeline is available.
func TestUIConfig_Shape(t *testing.T) {
	srv := uiTestServer(t, WithUI(uiAssetFS()), WithChannels(uiChannelStore(t), nil))

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/ui/config", nil)
	require.Equal(t, http.StatusOK, rec.Code, "GET /api/v1/ui/config must succeed when the console is wired")

	var body uiConfigResponseBody
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &body))

	require.Contains(t, body.Panels, "chat")
	require.Contains(t, body.Panels, "channel_timeline")
	require.Contains(t, body.Panels, "memory_strip")
	require.Contains(t, body.Panels, "cost")

	assert.True(t, body.Panels["chat"].Enabled, "chat ships enabled in Slice 1")
	assert.True(t, body.Panels["channel_timeline"].Enabled, "channel_timeline ships enabled in Slice 1")
	assert.False(t, body.Panels["memory_strip"].Enabled, "memory_strip ships off (Slice 2)")
	assert.False(t, body.Panels["cost"].Enabled, "cost ships off (Slice 4)")

	assert.True(t, body.Panels["chat"].Available, "chat is always wired")
	assert.True(t, body.Panels["channel_timeline"].Available, "channels wired → channel_timeline available")
	assert.False(t, body.Panels["memory_strip"].Available, "memory_strip has no backing subsystem yet (Slice 2)")

	assert.NotEmpty(t, body.Build.Version, "build.version must be reported so the console can display it")
}

// TestUIConfig_AvailabilityTracksChannels is the unit-level proof of the
// runtime-derivation contract (RFC 0048 §C / PR-plan D4): `available` is
// computed by the server from whether the backing subsystem is wired, never
// read from YAML. With no WithChannels, channel_timeline is unavailable.
func TestUIConfig_AvailabilityTracksChannels(t *testing.T) {
	srv := uiTestServer(t, WithUI(uiAssetFS())) // no WithChannels

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/ui/config", nil)
	require.Equal(t, http.StatusOK, rec.Code)

	var body uiConfigResponseBody
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &body))

	assert.False(t, body.Panels["channel_timeline"].Available,
		"channels absent → channel_timeline.available must be false")
	assert.True(t, body.Panels["channel_timeline"].Enabled,
		"the enabled toggle is independent of availability — it stays on, the client hides the slot")
}

// TestUIContext_Local pins the RFC 0048 §F identity contract for today's
// no-auth localhost mode: the single-tenant degenerate case reports
// principal=tenant=local and authenticated=false. PR 4's chat panel derives
// user_id from this endpoint, never a hard-coded or free-text user.
func TestUIContext_Local(t *testing.T) {
	srv := uiTestServer(t, WithUI(uiAssetFS()))

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/ui/context", nil)
	require.Equal(t, http.StatusOK, rec.Code)

	var body struct {
		Principal     string `json:"principal"`
		Tenant        string `json:"tenant"`
		Authenticated bool   `json:"authenticated"`
	}
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &body))

	assert.Equal(t, "local", body.Principal, "no-auth localhost principal is `local`")
	assert.Equal(t, "local", body.Tenant, "single-tenant degenerate case tenant is `local`")
	assert.False(t, body.Authenticated, "no auth layer today → authenticated=false")
}

// TestUIBuildVersion_FallsBackToCompiledDefault pins the fix for the PR-497
// review finding: when PERSATRIX_SERVICE_VERSION is unset and the build carries
// no module version — the plain `go build`/Docker case, and `go test`, where
// build-info reports "(devel)" — build.version must report the compiled-in
// defaultServiceVersion, never the old "dev" sentinel. This mirrors the Python
// runtimes' _DEFAULT_SERVICE_VERSION so the console and the observability stack
// agree on the version an operator sees.
func TestUIBuildVersion_FallsBackToCompiledDefault(t *testing.T) {
	t.Setenv("PERSATRIX_SERVICE_VERSION", "") // force the non-env path

	got := uiBuildVersion()

	assert.Equal(t, defaultServiceVersion, got,
		"with no env var and no module version, the fallback must be the compiled-in default")
	assert.Regexp(t, `^\d+\.\d+\.\d+`, got,
		"build.version fallback must be a real semver, not the 'dev' sentinel")
}

// TestUIBuildVersion_EnvOverride guards the precedence: an explicit
// PERSATRIX_SERVICE_VERSION (what real deployments and the observability
// runtimes set) wins over the compiled-in default.
func TestUIBuildVersion_EnvOverride(t *testing.T) {
	t.Setenv("PERSATRIX_SERVICE_VERSION", "9.9.9-test")

	assert.Equal(t, "9.9.9-test", uiBuildVersion(),
		"an explicit env version must override the compiled-in default")
}

// TestUIEndpoints_404WhenDisabled is the nil-safe gate for PR 2: with no WithUI
// option (the --enable-ui=off default) neither /api/v1/ui/config nor
// /api/v1/ui/context is registered, so both are a clean 404 and the surface is
// unchanged.
func TestUIEndpoints_404WhenDisabled(t *testing.T) {
	srv := uiTestServer(t) // no WithUI

	cfg := doRequest(srv.Handler(), http.MethodGet, "/api/v1/ui/config", nil)
	assert.Equal(t, http.StatusNotFound, cfg.Code,
		"without WithUI, /api/v1/ui/config must be a clean 404")

	ctx := doRequest(srv.Handler(), http.MethodGet, "/api/v1/ui/context", nil)
	assert.Equal(t, http.StatusNotFound, ctx.Code,
		"without WithUI, /api/v1/ui/context must be a clean 404")
}
